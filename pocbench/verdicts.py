"""Apply the frozen criteria to pooled cells (RC1-632; ADR 0019, ADR 0022).

The verdict rules are the ones written into criteria.json `method.verdicts`;
this module executes them, it does not choose them. Every criterion returns
one of: met, failed, inconclusive, unsupported, missing. Only `met` counts as
met ("inconclusive counts as not met").

Two callers. The Phase 4 loop applies the criteria to development cells on
the profiling slice: those verdicts are predictions, labeled so. Phase 5
applies them to held-out cells: those are the scored verdicts.

Interpretations the frozen text leaves open, fixed here and in ADR 0022:

- A missing cell (the configuration never ran at that level and state) is
  `missing`, not met.
- A shape with no successful observations has no latency: `unsupported`.
- No-regression walks p99, p95, p50 for the highest percentile supported on
  both sides. If none is, `unsupported`.
- The noise rule reads the baseline's per-repetition spread of the statistic
  being compared (the percentile for latency, successful qps for throughput
  and cost). Fewer than two repetitions means the spread is unmeasured:
  `inconclusive`.
- A baseline over the error ceiling on a shape has survivor-only
  percentiles (timeouts are not latencies). The verdict carries that as a
  note; the rule itself is unchanged.
- Predictions only: tail support is projected to the evaluation protocol.
  Development runs 2 repetitions, evaluation 5, and every repetition replays
  the same seeded query sequence, so observations beyond a percentile scale
  by 5/2 while distinct instances do not. A tail that is supported only by
  projection is marked `support: projected`. Held-out verdicts use the
  support actually measured (`support_scale` 1).
"""

from collections import Counter
from dataclasses import dataclass


class PlatformJoinError(ValueError):
    """Cells from two platforms reached one comparison (ADR 0007: the
    fallback reports each platform separately and never combines them)."""


def one_platform(cells: list[dict]) -> str | None:
    """The single platform these cells were measured on. Raises on a mix."""
    found = {c.get("platform") for c in cells}
    if len(found) > 1:
        raise PlatformJoinError(f"cells from {sorted(map(str, found))} cannot be compared or "
                                "combined; report each platform separately (ADR 0007)")
    return next(iter(found), None)


MET, FAILED, INCONCLUSIVE, UNSUPPORTED, MISSING = (
    "met", "failed", "inconclusive", "unsupported", "missing")
LADDER = (99, 95, 50)


def _cell(cells: list[dict], config: str, level: int, cs: str) -> dict | None:
    for c in cells:
        if c["config"] == config and c["level"] == level and c["cache_state"] == cs:
            return c
    return None


def _series(cell: dict, shape: str) -> dict | None:
    s = cell["by_shape"].get(shape)
    return None if s is None else s["net"]


def _ci(stat: dict) -> tuple[float, float]:
    lo, hi = stat["ci95_ms"]
    return float(lo), float(hi)


@dataclass(frozen=True)
class Support:
    """Tail floors from the frozen method, and the projection factor."""

    beyond: int
    distinct: int
    scale: float = 1.0

    def of(self, stat: dict) -> str | None:
        """'measured', 'projected', or None (unsupported)."""
        if stat["supported"]:
            return "measured"
        if (self.scale > 1 and stat["observations_beyond"] * self.scale >= self.beyond
                and stat["distinct_beyond"] >= self.distinct):
            return "projected"
        return None


MEASURED = Support(0, 0)


def latency_ceiling(c: dict, cand: dict, sup: Support = MEASURED) -> dict:
    q, ceiling = c["percentile"], c["ceiling_ms"]
    net = _series(cand, c["shape"])
    stat = None if net is None else net.get(f"p{q}")
    if stat is None:
        return {"verdict": UNSUPPORTED, "why": f"no successful {c['shape']} observations"}
    support = sup.of(stat)
    out = {"percentile": q, "value_ms": stat["ms"], "ci95_ms": stat["ci95_ms"],
           "bound_ms": ceiling, "support": support}
    if support is None:
        return out | {"verdict": UNSUPPORTED,
                      "why": f"p{q} tail not supported ({stat['observations_beyond']} obs, "
                             f"{stat['distinct_beyond']} distinct beyond)"}
    lo, hi = _ci(stat)
    if hi <= ceiling:
        return out | {"verdict": MET}
    if lo > ceiling:
        return out | {"verdict": FAILED}
    return out | {"verdict": INCONCLUSIVE, "why": "interval straddles the ceiling"}


def _noisy(spread: float | None, limit: float) -> str | None:
    if spread is None:
        return "baseline spread unmeasured (fewer than two repetitions)"
    if spread >= limit:
        return f"baseline spread {spread:.1%} >= {limit:.0%} (noise rule)"
    return None


def no_regression(c: dict, cand: dict, base: dict, noise_limit: float,
                  error_ceiling: float, sup: Support = MEASURED) -> dict:
    tol = c["tolerance"]
    cn, bn = _series(cand, c["shape"]), _series(base, c["shape"])
    notes = []
    bshape = base["by_shape"].get(c["shape"])
    if bshape and bshape["failure_fraction"] > error_ceiling:
        notes.append(f"baseline fails {bshape['failure_fraction']:.0%} of {c['shape']}: its "
                     "percentiles are survivors only")
    for q in LADDER:
        cs_ = None if cn is None else cn.get(f"p{q}")
        bs_ = None if bn is None else bn.get(f"p{q}")
        if cs_ and bs_ and sup.of(cs_) and sup.of(bs_):
            break
    else:
        return {"verdict": UNSUPPORTED, "why": "no percentile supported on both sides",
                "notes": notes}
    clo, chi = _ci(cs_)
    _, bhi = _ci(bs_)
    bound = (1 + tol) * bhi
    out = {"percentile": q, "value_ms": cs_["ms"], "ci95_ms": cs_["ci95_ms"],
           "baseline_ms": bs_["ms"], "baseline_ci95_ms": bs_["ci95_ms"],
           "bound_ms": round(bound, 3), "notes": notes,
           "support": "measured" if cs_["supported"] and bs_["supported"] else "projected"}
    noise = _noisy(bs_.get("spread"), noise_limit)
    if noise:
        return out | {"verdict": INCONCLUSIVE, "why": noise}
    if chi <= bound:
        return out | {"verdict": MET}
    if clo > bound:
        return out | {"verdict": FAILED}
    return out | {"verdict": INCONCLUSIVE, "why": "interval straddles the bound"}


def error_ceiling(c: dict, cand: dict) -> dict:
    s = cand["by_shape"].get(c["shape"])
    if s is None or not s["observations"]:
        return {"verdict": UNSUPPORTED, "why": f"no {c['shape']} observations"}
    frac = s["failure_fraction"]
    return {"value": frac, "bound": c["max_fraction"], "observations": s["observations"],
            "verdict": MET if frac <= c["max_fraction"] else FAILED}


def _qps_spread(cell: dict) -> float | None:
    reps = cell["successful_qps"]["per_rep"]
    med = cell["successful_qps"]["median"]
    if len(reps) < 2 or not med:
        return None
    return (max(reps) - min(reps)) / med


def throughput_floor(c: dict, cand: dict, base: dict, noise_limit: float) -> dict:
    v, b = cand["successful_qps"]["median"], base["successful_qps"]["median"]
    bound = c["at_least_ratio"] * b
    out = {"value": v, "baseline": b, "bound": bound}
    noise = _noisy(_qps_spread(base), noise_limit)
    if noise:
        return out | {"verdict": INCONCLUSIVE, "why": noise}
    return out | {"verdict": MET if v >= bound else FAILED}


def cost_ceiling(c: dict, cand: dict, base: dict, noise_limit: float) -> dict:
    v, b = cand["cost_per_successful_query_usd"], base["cost_per_successful_query_usd"]
    if v is None:
        return {"verdict": UNSUPPORTED, "why": "no successful queries"}
    if b is None:  # the baseline served nothing: any successful query is cheaper
        return {"value": v, "baseline": None, "verdict": MET}
    bound = c["at_most_ratio"] * b
    out = {"value": v, "baseline": b, "bound": bound}
    noise = _noisy(_qps_spread(base), noise_limit)
    if noise:
        return out | {"verdict": INCONCLUSIVE, "why": noise}
    return out | {"verdict": MET if v <= bound else FAILED}


def evaluate(doc: dict, cells: list[dict], candidate: str, *, error_ceiling_fraction: float,
             basis: str, support_scale: float = 1.0) -> dict:
    """Every frozen criterion for `candidate` against the goal's `today`
    baseline. `basis` labels what the verdicts are: "prediction (profiling
    slice)" in the loop, "held-out" in Phase 5. `support_scale` > 1 only for
    predictions (module docstring)."""
    one_platform(cells)
    against = doc["goal"]["today"]
    noise_limit = doc["method"]["noise_rule"]["baseline_spread_at_least"]
    us = doc["method"]["under_sampled"]
    sup = Support(us["tail_beyond"], us["tail_distinct_beyond"], support_scale)
    out = []
    for c in doc["criteria"]:
        cand = _cell(cells, candidate, c["level"], c["cache_state"])
        base = _cell(cells, against, c["level"], c["cache_state"])
        head = {k: c.get(k) for k in ("id", "kind", "shape", "level", "cache_state")}
        if cand is None or (base is None and c["kind"] in
                            ("no_regression", "throughput_floor", "cost_ceiling")):
            out.append(head | {"verdict": MISSING, "why": "cell not run"})
            continue
        kind = c["kind"]
        if kind == "latency_ceiling":
            v = latency_ceiling(c, cand, sup)
        elif kind == "no_regression":
            v = no_regression(c, cand, base, noise_limit, error_ceiling_fraction, sup)
        elif kind == "error_ceiling":
            v = error_ceiling(c, cand)
        elif kind == "throughput_floor":
            v = throughput_floor(c, cand, base, noise_limit)
        elif kind == "cost_ceiling":
            v = cost_ceiling(c, cand, base, noise_limit)
        else:
            raise ValueError(f"unknown criterion kind {kind!r}")
        out.append(head | v)
    counts = Counter(v["verdict"] for v in out)
    return {"candidate": candidate, "against": against, "basis": basis,
            "support_scale": support_scale,
            "criteria_total": len(out), "counts": dict(counts),
            "all_met": counts.get(MET, 0) == len(out), "verdicts": out}


def unmet_shapes(result: dict) -> list[str]:
    """Shapes named by any criterion that is not met, in id order."""
    return sorted({v["shape"] for v in result["verdicts"]
                   if v["verdict"] != MET and v.get("shape")})


def confidence(v: dict, margin: float) -> str:
    """How far a met verdict's interval clears its bound, in the noise gate's
    smallest pass effect: high when the conservative end of the interval
    clears the bound by more than `margin`, medium when met within it, low
    when not met."""
    if v["verdict"] != MET:
        return "low"
    if "ci95_ms" in v and v.get("bound_ms"):
        hi = float(v["ci95_ms"][1])
        return "high" if hi <= (1 - margin) * v["bound_ms"] else "medium"
    if v["kind"] == "error_ceiling":
        return "high" if v["value"] <= (1 - margin) * v["bound"] else "medium"
    if v["kind"] == "throughput_floor":
        return "high" if v["value"] >= (1 + margin) * v["bound"] else "medium"
    if v["kind"] == "cost_ceiling" and v.get("bound"):
        return "high" if v["value"] <= (1 - margin) * v["bound"] else "medium"
    return "medium"
