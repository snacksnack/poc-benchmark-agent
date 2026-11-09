"""Phase 5 results document (RC1-633; ADR 0025).

Turns verified evidence into the customer-facing document: per-criterion
verdicts against the frozen criteria on the held-out slice, the headline with
its uncertainty, regressions by name, errors and timeouts, the profiled
versus held-out gain, prediction accuracy, and cost lines kept separate.

Three rules shape the code:

- **Evidence is verified before it is read.** The real run loads the criteria
  through `criteria.load_verified`, the frozen output through
  `candidate.load_frozen`, the evaluation record through `record.verify`, and
  re-derives the committed verdicts from the pooled cells (`load_real`).
- **One platform per figure.** Every figure is computed from one platform's
  cells (`verdicts.one_platform` raises on a mix). In the fallback each
  platform gets its own summary and section; nothing combines them.
- **Bad news leads.** A refusal, a loss against today, an under-sampled key
  tail, or any missed criterion is listed before the headline (`lead`).
"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pocbench import (
    candidate,
    config,
    criteria,
    harness,
    loop,
    phase3,
    phase4,
    record,
    split,
    verdicts,
)

PLATFORM_LABELS = {
    "neon": "Neon Postgres",
    "local-postgres": "local Postgres (fallback latency platform)",
    "fixture-model": "fixture model (synthetic latencies, not a measurement)",
}
SLICES = (split.HELD_OUT, split.PROFILING)
LOSS_KINDS = ("no_regression", "throughput_floor", "cost_ceiling")
BEST_LABEL = "best tested candidate; fails to qualify"
DOC_PATH = config.REPO_ROOT / "docs" / "results.md"
FIXTURE_DOC_DIR = config.REPO_ROOT / "docs" / "fixtures"


@dataclass
class Evidence:
    """Everything one results document is built from."""

    title: str
    doc: dict                                  # the verified criteria
    criteria_sha256: str
    frozen: dict                               # the frozen candidate or refusal
    frozen_sha256: str
    cells: list[dict]                          # evaluation cells, both slices
    dev_cells: dict[str, list[dict]]           # experiment name -> development cells
    dev_naive: list[dict]                      # today's baseline, development pool
    builds: dict[str, dict]                    # config name -> cu, index builds, sizes
    space: dict
    load_model: dict
    profile: dict
    execution: dict | None = None              # compute the run held, by job
    sources: list[str] = field(default_factory=list)
    fixture: str | None = None


# ------------------------------------------------------------- evidence

def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def attach_outcomes(cells: list[dict], rows: list[dict]) -> list[dict]:
    """Errors and timeouts per shape, summed from the hash-verified record
    rows of each cell. Cells pooled before the split existed lack them; a
    count that disagrees with the pooled failure fraction raises."""
    sums: dict[tuple, dict] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    for r in rows:
        if r.get("kind") != "run" or r["status"] != record.SCORED:
            continue
        key = (r["slice"], r["config"]["name"], r["level"], r["cache_state"])
        for sid, b in r["run"]["by_shape"].items():
            acc = sums[key][sid]
            acc[0] += b["errors"]
            acc[1] += b["timeouts"]
            acc[2] += b["observations"]
    out = []
    for c in cells:
        c = json.loads(json.dumps(c))
        got = sums[(c["slice"], c["config"], c["level"], c["cache_state"])]
        for sid, s in c["by_shape"].items():
            errors, timeouts, obs = got.get(sid, [0, 0, 0])
            if obs != s["observations"] or abs(
                    (errors + timeouts) / max(obs, 1) - s["failure_fraction"]) > 1e-5:
                raise ValueError(f"record and pooled cell disagree on {c['config']} "
                                 f"{c['level']}/{c['cache_state']} {sid}")
            if "timeouts" in s and (s["timeouts"], s["errors"]) != (timeouts, errors):
                raise ValueError(f"record and pooled cell disagree on {sid} timeouts")
            s["errors"], s["timeouts"] = errors, timeouts
        out.append(c)
    return out


def load_real(state: Path = phase4.STATE) -> Evidence:
    """The committed Phase 4 run, every input verified."""
    doc = criteria.load_verified()
    crit_sha = harness.criteria_sha256(doc)
    frozen = candidate.load_frozen(crit_sha)
    rows = record.verify(state / "eval-record.jsonl")
    pins = {r["frozen"]["sha256"] for r in rows if r.get("kind") == "run"}
    if pins != {frozen.sha256}:
        raise ValueError(f"evaluation rows pin {sorted(pins)}, not the frozen {frozen.sha256}")
    cells = attach_outcomes(json.loads((state / "eval-cells.json").read_text()), rows)
    committed = json.loads((state / "eval-verdicts.json").read_text())
    if committed["frozen"]["sha256"] != frozen.sha256:
        raise ValueError("eval-verdicts.json was computed under another frozen output")
    lm = config.load_model().data
    err = lm["errors"]["error_ceiling_fraction"]
    for sl, by_name in committed["verdicts"].items():
        for name, v in by_name.items():
            again = verdicts.evaluate(doc, [c for c in cells if c["slice"] == sl], name,
                                      error_ceiling_fraction=err, basis=v["basis"])
            if json.loads(json.dumps(again)) != v:
                raise ValueError(f"committed verdicts for {name} on {sl} do not re-derive")

    loop_rows = [json.loads(x) for x in (state / "loop-log.jsonl").read_text().splitlines()]
    order = [r["bundle"]["name"] for r in loop_rows if r.get("kind") == "experiment"]
    exps = {n: json.loads((state / "experiments" / f"{n}.json").read_text()) for n in order}
    jobs = [json.loads(x) for x in (state / "jobs.jsonl").read_text().splitlines()]
    evals = [j for j in jobs if j["task"] == "evaluate" and j.get("builds")]
    by_task: dict[str, float] = defaultdict(float)
    for j in jobs:
        by_task[j["task"]] += sum(j.get("cu_hours_held", {}).values())
    sp = config.config_space().data
    return Evidence(
        title="POC benchmark, Phase 4 run (RC1-632)",
        doc=doc, criteria_sha256=crit_sha, frozen=frozen.doc, frozen_sha256=frozen.sha256,
        cells=cells, dev_cells={n: e["cells"] for n, e in exps.items()},
        dev_naive=exps[order[-1]]["naive_cells_at_the_time"] if order else [],
        builds={k: v for k, v in evals[-1]["builds"].items() if not k.startswith("_")},
        space=sp, load_model=lm,
        profile=json.loads((config.REPO_ROOT / criteria.PINNED_INPUTS["profile"]).read_text()),
        execution={"cu_hours_by_task": dict(by_task), "jobs": len(jobs),
                   "model_usd": frozen.doc["spent"]["model_usd"],
                   "notes": ["About 2.5 CU-h more ran in a loop job whose state was lost "
                             "before the commit gate existed; it is not in jobs.jsonl "
                             "(docs/phase4-findings.md, section 4)."]},
        sources=[f"`results/phase4/eval-record.jsonl`: {len(rows) - 1} runs, hash chain "
                 f"verified, every row pins the frozen output",
                 f"`results/phase4/eval-cells.json` sha256 "
                 f"`{_sha_file(state / 'eval-cells.json')[:12]}`; committed verdicts re-derived "
                 "from it",
                 f"`results/phase4/loop-log.jsonl` sha256 `{frozen.doc['loop_log_sha256'][:12]}`",
                 f"`results/phase4/jobs.jsonl`: {len(jobs)} jobs"])


# ------------------------------------------------------------- analysis

def names(ev: Evidence) -> dict[str, str]:
    """Shape id -> readable name: the criteria's names for goal shapes, the
    profile's label otherwise."""
    out = {s["shape"]: s["label"] for s in ev.profile["shapes"]}
    out |= {c["shape"]: c["shape_name"] for c in ev.doc["criteria"] if c.get("shape_name")}
    return out


def platforms(cells: list[dict]) -> list[str]:
    return sorted({c.get("platform") or "unlabeled" for c in cells})


def scored_configs(ev: Evidence) -> list[str]:
    """What the held-out verdicts are about: the frozen candidate, or, after
    a refusal, the two baselines (as `phase4.eval_report`)."""
    if ev.frozen["kind"] == "candidate":
        return [ev.frozen["config"]["name"]]
    return ["default", ev.doc["goal"]["today"]]


def _err(ev: Evidence) -> float:
    return ev.load_model["errors"]["error_ceiling_fraction"]


def held_out_verdicts(ev: Evidence, cells: list[dict]) -> dict[str, dict[str, dict]]:
    """slice -> config -> verdicts, one platform's cells only."""
    verdicts.one_platform(cells)
    out = {}
    for sl in SLICES:
        sub = [c for c in cells if c["slice"] == sl]
        out[sl] = {n: verdicts.evaluate(ev.doc, sub, n, error_ceiling_fraction=_err(ev),
                                        basis=f"{sl} slice, evaluation protocol")
                   for n in scored_configs(ev)}
    return out


def _cell(cells: list[dict], cfg: str, level: int, cs: str, sl: str) -> dict | None:
    for c in cells:
        if (c["config"], c["level"], c["cache_state"], c["slice"]) == (cfg, level, cs, sl):
            return c
    return None


def _goal_criterion(ev: Evidence, shape: str, level: int, cs: str) -> dict | None:
    for c in ev.doc["criteria"]:
        if (c["kind"], c.get("shape"), c["level"], c["cache_state"]) == (
                "latency_ceiling", shape, level, cs):
            return c
    return None


def stat(cell: dict | None, shape: str, q: int) -> dict | None:
    if cell is None:
        return None
    s = cell["by_shape"].get(shape)
    return None if s is None else s["net"].get(f"p{q}")


def headline_rows(ev: Evidence, cells: list[dict], sl: str = split.HELD_OUT) -> list[dict]:
    """Per goal shape at the objective level, warm: today and (for a
    candidate) the recommendation, at the percentile the shape's criterion
    names, with interval, spread and tail support."""
    verdicts.one_platform(cells)
    level = ev.doc["goal"]["objective"]["at_level"]
    today = ev.doc["goal"]["today"]
    cand = ev.frozen["config"]["name"] if ev.frozen["kind"] == "candidate" else None
    out = []
    for shape in ev.doc["goal"]["shapes"]:
        crit = _goal_criterion(ev, shape, level, "warm")
        q = crit["percentile"] if crit else 99
        row = {"shape": shape, "percentile": q, "level": level,
               "today": stat(_cell(cells, today, level, "warm", sl), shape, q),
               "today_fail": _fail(_cell(cells, today, level, "warm", sl), shape)}
        if cand:
            row |= {"candidate": stat(_cell(cells, cand, level, "warm", sl), shape, q),
                    "candidate_fail": _fail(_cell(cells, cand, level, "warm", sl), shape)}
        out.append(row)
    return out


def _fail(cell: dict | None, shape: str) -> float | None:
    if cell is None or shape not in cell["by_shape"]:
        return None
    return cell["by_shape"][shape]["failure_fraction"]


def cost_ratio(ev: Evidence, cells: list[dict], sl: str = split.HELD_OUT) -> dict | None:
    """Candidate over today, cost per successful query at the objective."""
    verdicts.one_platform(cells)
    if ev.frozen["kind"] != "candidate":
        return None
    level = ev.doc["goal"]["objective"]["at_level"]
    c = _cell(cells, ev.frozen["config"]["name"], level, "warm", sl)
    t = _cell(cells, ev.doc["goal"]["today"], level, "warm", sl)
    if not c or not t:
        return None
    a, b = c["cost_per_successful_query_usd"], t["cost_per_successful_query_usd"]
    return {"candidate": a, "today": b, "ratio": a / b if a and b else None, "level": level}


def lead(ev: Evidence, held: dict[str, dict[str, dict]], cells: list[dict]) -> list[dict]:
    """The items that must precede any win, in order: the refusal, losses
    against today, under-sampled key tails, then every other missed
    criterion. Empty only when every held-out criterion is met."""
    nm = names(ev)
    items: list[dict] = []
    if ev.frozen["kind"] == "refusal":
        items.append({"kind": "refusal",
                      "text": f"No recommendation. Refusal `{ev.frozen['refusal_id']}`, class "
                              f"**{ev.frozen['class']}**: {ev.frozen['statement']}. Shapes "
                              f"named: {_shapes(ev.frozen['shapes'], nm)}."})
        best = ev.frozen.get("best_tested")
        if best:
            row = next(t for t in ev.frozen["tested"] if t["bundle"] == best["bundle"])
            met = row["predicted_counts"].get(verdicts.MET, 0)
            items.append({"kind": "best_tested",
                          "text": f"`{best['bundle']}` ({BEST_LABEL}): {met} of "
                                  f"{len(ev.doc['criteria'])} criteria met in "
                                  "development (predicted, profiling slice)."})
        return items
    name = ev.frozen["config"]["name"]
    vs = held[split.HELD_OUT][name]["verdicts"]
    goal = set(ev.doc["goal"]["shapes"])
    losses = [v for v in vs if v["kind"] in LOSS_KINDS and v["verdict"] == verdicts.FAILED]
    thin = [v for v in vs if v["kind"] == "latency_ceiling" and v["shape"] in goal
            and v["verdict"] == verdicts.UNSUPPORTED]
    for key, group in _by_shape(losses):
        items.append({"kind": "loss", "ids": [v["id"] for v in group], "shape": key,
                      "text": f"Loss against today, {_who(key, nm)}: "
                              + "; ".join(_loss_text(v, cells, ev) for v in group) + "."})
    for key, group in _by_shape(thin):
        items.append({"kind": "under_sampled", "ids": [v["id"] for v in group], "shape": key,
                      "text": f"Under-sampled key tail, {_who(key, nm)}: "
                              + "; ".join(f"{_at(v)} {v['why']}" for v in group)
                              + f". Floors: {_floors(ev)} beyond the percentile. Not scored; "
                              "counts as not met."})
    shown = {i for it in items for i in it["ids"]}
    missed = [v for v in vs if v["verdict"] != verdicts.MET and v["id"] not in shown]
    if missed or shown:
        met = sum(v["verdict"] == verdicts.MET for v in vs)
        by = Counter(v["verdict"] for v in vs if v["verdict"] != verdicts.MET)
        rest = ("; ".join(f"{v['id']} {_where(v, nm)} {v['verdict']}" for v in missed)
                if missed else "")
        items.append({"kind": "missed", "ids": [v["id"] for v in missed],
                      "text": f"The recommendation did not hold on held-out accounts: {met} of "
                              f"{len(vs)} criteria met ("
                              + ", ".join(f"{n} {k}" for k, n in sorted(by.items())) + ")."
                              + (f" Also not met: {rest}." if rest else
                                 " Every unmet criterion is listed above.")})
    return items


def _by_shape(vs: list[dict]) -> list[tuple[str, list[dict]]]:
    out: dict[str, list[dict]] = {}
    for v in vs:
        out.setdefault(v.get("shape") or v["kind"], []).append(v)
    return list(out.items())


def _who(key: str, nm: dict) -> str:
    return f"{key} ({nm[key]})" if key in nm else key.replace("_", " ")


def _at(v: dict) -> str:
    return f"{v['level']} sessions {v['cache_state']}:"


def _floors(ev: Evidence) -> str:
    us = ev.doc["method"]["under_sampled"]
    return f"{us['tail_beyond']} observations and {us['tail_distinct_beyond']} distinct instances"


def _where(v: dict, nm: dict) -> str:
    s = v.get("shape")
    head = f"{s} {nm.get(s, '')}".strip() if s else v["kind"].replace("_", " ")
    return f"{head} at {v['level']} sessions, {v['cache_state']}"


def _shapes(ids: list[str], nm: dict) -> str:
    return ", ".join(f"{s} ({nm.get(s, s)})" for s in ids) or "none"


def _loss_text(v: dict, cells: list[dict], ev: Evidence) -> str:
    if v["kind"] == "no_regression":
        cand = stat(_cell(cells, ev.frozen["config"]["name"], v["level"], v["cache_state"],
                          split.HELD_OUT), v["shape"], v["percentile"])
        base = stat(_cell(cells, ev.doc["goal"]["today"], v["level"], v["cache_state"],
                          split.HELD_OUT), v["shape"], v["percentile"])
        return (f"{_at(v)} p{v['percentile']} {_ms_ci(cand)} against today's "
                f"{_ms_ci(base)}, bound {fmt_ms(v['bound_ms'])} ms "
                f"(+{_tol(ev, v)}), spread {_pct(cand.get('spread'))} recommended / "
                f"{_pct(base.get('spread'))} today")
    what = "throughput" if v["kind"] == "throughput_floor" else "cost per successful query"
    return (f"{_at(v)} {what} {_num(v['value'])} against bound {_num(v['bound'])} "
            f"(today {_num(v['baseline'])})")


def _tol(ev: Evidence, v: dict) -> str:
    c = next(c for c in ev.doc["criteria"] if c["id"] == v["id"])
    return f"{c['tolerance']:.0%}"


def regressions(ev: Evidence, held: dict, cells: list[dict], cfg: str) -> list[dict]:
    """No-regression criteria not met for `cfg` on held-out, worst first."""
    out = []
    for v in held[split.HELD_OUT][cfg]["verdicts"]:
        if v["kind"] != "no_regression" or v["verdict"] == verdicts.MET:
            continue
        q = v.get("percentile")
        c = stat(_cell(cells, cfg, v["level"], v["cache_state"], split.HELD_OUT), v["shape"], q) \
            if q else None
        b = stat(_cell(cells, ev.doc["goal"]["today"], v["level"], v["cache_state"],
                       split.HELD_OUT), v["shape"], q) if q else None
        out.append(v | {"cand_stat": c, "base_stat": b,
                        "ratio": (c["ms"] / b["ms"]) if c and b and b["ms"] else None})
    rank = {verdicts.FAILED: 0, verdicts.INCONCLUSIVE: 1}
    return sorted(out, key=lambda r: (rank.get(r["verdict"], 2), -(r["ratio"] or 0)))


def accuracy(predicted: list[dict], held: list[dict]) -> dict:
    """Bundle-level prediction accuracy: verdict agreement per criterion, and
    for latency figures, whether the held-out value fell inside the predicted
    interval."""
    hv = {v["id"]: v for v in held}
    agree, inside, both, rows = 0, 0, 0, []
    for p in predicted:
        h = hv.get(p["id"])
        if h is None:
            continue
        same = p["verdict"] == h["verdict"]
        agree += same
        if p.get("ci95_ms") and h.get("value_ms") is not None and p.get("percentile") == h.get(
                "percentile"):
            both += 1
            inside += p["ci95_ms"][0] <= h["value_ms"] <= p["ci95_ms"][1]
        if not same:
            rows.append({"id": p["id"], "kind": p["kind"], "shape": p.get("shape"),
                         "level": p["level"], "cache_state": p["cache_state"],
                         "predicted": p["verdict"], "held_out": h["verdict"],
                         "predicted_value": _pv(p), "held_out_value": _pv(h)})
    return {"criteria": len(predicted), "agree": agree, "latency_compared": both,
            "inside_interval": inside, "disagreements": rows}


def _pv(v: dict) -> str:
    """A verdict's figure with its statistic, so differing percentiles show."""
    if v.get("value_ms") is not None:
        return f"p{v['percentile']} {fmt_ms(v['value_ms'])} ms"
    x = v.get("value")
    if x is None:
        return "n/a"
    if v["kind"] == "error_ceiling":
        return f"{x:.2%}"
    if v["kind"] == "cost_ceiling":
        return _usd_q(x)
    return f"{x:.1f} qps"


def today_prediction(ev: Evidence) -> dict | None:
    """Today's baseline predicted from the development pool (profiling slice,
    projected support, as the loop saw it) for comparison with its held-out
    verdicts: the one bundle measured both ways in every outcome."""
    if not ev.dev_naive:
        return None
    sp = ev.space["protocol"]
    return verdicts.evaluate(ev.doc, ev.dev_naive, ev.doc["goal"]["today"],
                             error_ceiling_fraction=_err(ev),
                             basis=loop.PREDICTION,
                             support_scale=sp["eval"]["repetitions"] / sp["dev"]["repetitions"])


def cost_lines(ev: Evidence) -> dict:
    """Index build, index storage and projected operating cost per
    configuration (`phase3.cost_lines`, the same four separate lines)."""
    return phase3.cost_lines({"configs": ev.builds}, ev.space)["configs"]


# ------------------------------------------------------------ formatting

def fmt_ms(x: float | None) -> str:
    if x is None:
        return "n/a"
    if x >= 100:
        return f"{x:,.0f}"
    return f"{x:.1f}" if x >= 10 else f"{x:.2f}"


def _ms_ci(s: dict | None) -> str:
    if s is None:
        return "no successful queries"
    lo, hi = s["ci95_ms"]
    out = f"{fmt_ms(s['ms'])} ms (95% CI {fmt_ms(lo)}-{fmt_ms(hi)})"
    if not s["supported"]:
        out += (f", under-sampled: {s['observations_beyond']} obs / {s['distinct_beyond']} "
                "distinct beyond")
    return out


def _pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{x:.{digits}%}"


def _num(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float) and abs(x) < 0.01:
        return f"${x:.3g}" if x else "0"
    return f"{x:,.2f}" if isinstance(x, float) else str(x)


def _usd_q(x: float | None) -> str:
    return "n/a" if x is None else f"${x * 1e6:,.2f} per million"


def _verdict_value(v: dict) -> str:
    if v["verdict"] == verdicts.MISSING:
        return "cell not run"
    if "value_ms" in v:
        lo, hi = v["ci95_ms"]
        return f"p{v['percentile']} {fmt_ms(v['value_ms'])} ({fmt_ms(lo)}-{fmt_ms(hi)})"
    if v["kind"] == "error_ceiling" and "value" in v:
        return f"{v['value']:.2%} of {v['observations']:,}"
    if v["kind"] == "cost_ceiling" and v.get("value") is not None:
        return _usd_q(v["value"])
    if v["kind"] == "throughput_floor":
        return f"{v['value']:.1f} qps"
    return "n/a"


def _verdict_bound(v: dict) -> str:
    if v["kind"] == "error_ceiling" and "bound" in v:
        return f"<= {v['bound']:.0%}"
    if v.get("bound_ms") is not None:
        return f"<= {fmt_ms(v['bound_ms'])} ms"
    if v["kind"] == "cost_ceiling" and v.get("bound") is not None:
        return f"<= {_usd_q(v['bound'])}"
    if v["kind"] == "throughput_floor" and v.get("bound") is not None:
        return f">= {v['bound']:.1f} qps"
    return ""


def _note(v: dict) -> str:
    bits = []
    if v.get("why"):
        bits.append(v["why"])
    if v.get("support") == "projected":
        bits.append("support projected")
    bits += v.get("notes", [])
    return "; ".join(bits).replace("|", "/")


# --------------------------------------------------------------- render

class Doc:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def table(self, caption: str, head: list[str], rows: list[list[str]]) -> None:
        self.add(caption, "", "| " + " | ".join(head) + " |",
                 "|" + "|".join(" --- " for _ in head) + "|")
        self.add(*("| " + " | ".join(str(x) for x in r) + " |" for r in rows))
        self.add("")


def _cap(platform: str, *bits: str) -> str:
    return f"*Platform: {PLATFORM_LABELS.get(platform, platform)}*" + "".join(
        f" · {b}" for b in bits)


def _headline(ev: Evidence, cells: list[dict], platform: str, held: dict) -> list[str]:
    nm = names(ev)
    ph = ev.profile["headline"]
    level = ev.doc["goal"]["objective"]["at_level"]
    plat = PLATFORM_LABELS.get(platform, platform)
    rows = headline_rows(ev, cells)
    reps = ev.space["protocol"]["eval"]["repetitions"]
    window = ev.load_model["timing"]["measure_seconds"]
    out = []
    if ev.frozen["kind"] == "candidate":
        cfg = ev.frozen["config"]
        cr = cost_ratio(ev, cells)
        moves = "; ".join(
            f"{r['shape']} {nm[r['shape']]} p{r['percentile']} from {_ms_ci(r['today'])}"
            + _survivors(ev, r["today_fail"]) + f" to {_ms_ci(r['candidate'])}"
            + _survivors(ev, r["candidate_fail"]) for r in rows)
        cost = (f"at {_pct(cr['ratio'])} of today's cost per successful query "
                f"({_usd_q(cr['candidate'])} against {_usd_q(cr['today'])}, {reps} x {window} s "
                "measured windows)") if cr and cr["ratio"] else "cost ratio not measurable"
        out.append(
            f"Headline ({plat}, held-out accounts, {level} concurrent sessions, warm): profiled "
            f"{ph['log_rows_profiled']:,} query-log rows and {ph['shapes']} query shapes; the "
            f"recommended configuration ({cfg['cu']:g} CU with {len(cfg['index_ids'])} indexes) "
            f"moved {moves}, {cost}.")
    else:
        today = ev.doc["goal"]["today"]
        b = ev.doc["baselines"]
        moves = "; ".join(
            f"{r['shape']} {nm[r['shape']]} p{r['percentile']} {_ms_ci(r['today'])}, "
            f"{_pct(r['today_fail'], 1)} errors and timeouts"
            + (" (latency of the survivors only)" if (r["today_fail"] or 0) > _err(ev) else "")
            for r in rows)
        counts = "; ".join(
            f"{n}: {v['counts'].get(verdicts.MET, 0)} of {v['criteria_total']} criteria met"
            for n, v in held[split.HELD_OUT].items())
        out.append(
            f"Today on held-out accounts ({plat}, {level} concurrent sessions, warm, "
            f"{today} baseline at {b[today]['cu']:g} CU): profiled {ph['log_rows_profiled']:,} "
            f"query-log rows and {ph['shapes']} query shapes; {moves}. Held-out baselines: "
            f"{counts}.")
    return out


def _survivors(ev: Evidence, fail: float | None) -> str:
    if fail is None or fail <= _err(ev):
        return ""
    return f" ({fail:.1%} errors and timeouts; latency of the survivors only)"


def _gain_line(ev: Evidence, cells: list[dict]) -> str | None:
    if ev.frozen["kind"] != "candidate":
        return None
    nm = names(ev)
    parts = []
    for ho, pr in zip(headline_rows(ev, cells, split.HELD_OUT),
                      headline_rows(ev, cells, split.PROFILING), strict=True):
        g = [_gain(r) for r in (ho, pr)]
        if None in g:
            parts.append(f"{ho['shape']} {nm[ho['shape']]}: not measurable on both slices")
        else:
            parts.append(f"{ho['shape']} {nm[ho['shape']]} {_pct(g[0])} held-out vs "
                         f"{_pct(g[1])} profiled")
    return "Gain against today, held-out vs profiled (latency cut at the criterion's " \
           "percentile): " + "; ".join(parts) + "."


def _gain(r: dict) -> float | None:
    a, b = r.get("candidate"), r.get("today")
    if not a or not b or not b["ms"]:
        return None
    return 1 - a["ms"] / b["ms"]


def _regression_line(ev: Evidence, held: dict, cells: list[dict]) -> str:
    nm = names(ev)
    cfgs = scored_configs(ev) if ev.frozen["kind"] == "candidate" else ["default"]
    parts = []
    for cfg in cfgs:
        regs = [r for r in regressions(ev, held, cells, cfg) if r["verdict"] == verdicts.FAILED]
        who = "the recommendation" if ev.frozen["kind"] == "candidate" else f"{cfg}"
        if regs:
            parts.append(f"{who} regresses against today on " + ", ".join(
                f"{r['shape']} {nm.get(r['shape'], '')} ({r['level']}, {r['cache_state']})"
                for r in regs))
        else:
            parts.append(f"no failed no-regression criterion for {who}")
    return "Regressions (held-out): " + "; ".join(parts) + "."


def _summary(d: Doc, ev: Evidence, cells: list[dict], platform: str, held: dict) -> None:
    d.add(f"## Summary: {PLATFORM_LABELS.get(platform, platform)}", "")
    items = lead(ev, held, cells)
    for i in items:
        d.add(f"- {i['text']}")
    for line in _headline(ev, cells, platform, held):
        d.add(f"- {line}")
    gain = _gain_line(ev, cells)
    if gain:
        d.add(f"- {gain}")
    d.add(f"- {_regression_line(ev, held, cells)}", "")


def _verdict_tables(d: Doc, ev: Evidence, held: dict, platform: str) -> None:
    nm = names(ev)
    d.add("### Per-criterion verdicts", "",
          f"Every criterion in `criteria/criteria.json` (sha256 `{ev.criteria_sha256[:12]}`, "
          "verified), at the level and cache state it names, scored on the held-out slice. "
          "Only `met` counts as met.", "")
    for cfg, res in held[split.HELD_OUT].items():
        label = (f"recommended configuration `{cfg}`" if ev.frozen["kind"] == "candidate"
                 else f"held-out baseline `{cfg}`")
        c = res["counts"]
        d.table(_cap(platform, "held-out slice", label, f"against `{res['against']}`",
                     ", ".join(f"{k} {n}" for k, n in sorted(c.items()))),
                ["id", "criterion", "shape", "level", "cache", "value (95% CI)", "bound",
                 "verdict", "note"],
                [[v["id"], v["kind"].replace("_", " "),
                  f"{v.get('shape') or ''} {nm.get(v.get('shape'), '')}".strip(), v["level"],
                  v["cache_state"], _verdict_value(v), _verdict_bound(v),
                  f"**{v['verdict']}**" if v["verdict"] != verdicts.MET else v["verdict"],
                  _note(v)] for v in res["verdicts"]])


def _regression_table(d: Doc, ev: Evidence, held: dict, cells: list[dict], platform: str) -> None:
    nm = names(ev)
    d.add("### Regressions", "",
          "No-regression criteria not met, by name, with each side's per-repetition spread "
          "((max - min) / median). Failed first, then not ruled out.", "")
    cfgs = scored_configs(ev) if ev.frozen["kind"] == "candidate" else ["default"]
    for cfg in cfgs:
        rows = regressions(ev, held, cells, cfg)
        if not rows:
            d.add(f"{_cap(platform, 'held-out slice', f'`{cfg}`')}: none; every no-regression "
                  "criterion met.", "")
            continue
        d.table(_cap(platform, "held-out slice", f"`{cfg}` against today"),
                ["id", "shape", "level", "cache", "verdict", "percentile", "value", "today",
                 "ratio", "spread (value / today)", "why"],
                [[r["id"], f"{r['shape']} {nm.get(r['shape'], '')}", r["level"],
                  r["cache_state"], r["verdict"],
                  f"p{r['percentile']}" if r.get("percentile") else "none supported",
                  _ms_ci(r["cand_stat"]) if r["cand_stat"] else "n/a",
                  _ms_ci(r["base_stat"]) if r["base_stat"] else "n/a",
                  f"{r['ratio']:.2f}x" if r["ratio"] else "n/a",
                  f"{_pct((r['cand_stat'] or {}).get('spread'))} / "
                  f"{_pct((r['base_stat'] or {}).get('spread'))}",
                  _note(r)] for r in rows])


def _errors_table(d: Doc, ev: Evidence, cells: list[dict], platform: str) -> None:
    nm = names(ev)
    cfgs = sorted({c["config"] for c in cells}, key=lambda n: (n != ev.doc["goal"]["today"], n))
    levels = [lvl for lvl in ev.doc["method"]["levels"] if lvl > 1]
    d.add("### Errors and timeouts", "",
          "Per shape, timeouts (the 5 s statement timeout, or the client's) and errors, as a "
          "share of observations, pooled over the repetitions. The error ceiling is "
          f"{_err(ev):.0%} of errors and timeouts together.", "")
    rows = []
    for lvl in levels:
        for cs in ev.doc["method"]["cache_states"]:
            for sid in sorted({s for c in cells for s in c["by_shape"]}):
                r = [sid + " " + nm.get(sid, ""), lvl, cs]
                for cfg in cfgs:
                    cell = _cell(cells, cfg, lvl, cs, split.HELD_OUT)
                    b = cell["by_shape"].get(sid) if cell else None
                    if not b or not b["observations"]:
                        r.append("n/a")
                        continue
                    n = b["observations"]
                    r.append(f"{b['timeouts'] / n:.1%} / {b['errors'] / n:.1%} of {n:,}")
                rows.append(r)
    d.table(_cap(platform, "held-out slice", "timeouts / errors of observations"),
            ["shape", "level", "cache"] + [f"`{c}`" for c in cfgs], rows)


def _slices_table(d: Doc, ev: Evidence, held: dict, cells: list[dict], platform: str) -> None:
    nm = names(ev)
    d.add("### Profiled versus held-out", "",
          "The same evaluation protocol on both slices: the profiling slice the loop learned "
          "on, and the held-out accounts it never saw. A large gap means the profile did not "
          "represent the held-out workload.", "")
    rows = []
    for cfg in held[split.HELD_OUT]:
        r = [f"`{cfg}`"]
        for sl in SLICES:
            c = held[sl][cfg]["counts"]
            r.append(f"{c.get('met', 0)} met / {c.get('failed', 0)} failed / "
                     f"{sum(c.values()) - c.get('met', 0) - c.get('failed', 0)} other")
        rows.append(r)
    d.table(_cap(platform, "verdict counts by slice"), ["config", "held-out", "profiling"], rows)
    level = ev.doc["goal"]["objective"]["at_level"]
    ho, pr = headline_rows(ev, cells, split.HELD_OUT), headline_rows(ev, cells, split.PROFILING)
    cand = ev.frozen["kind"] == "candidate"
    head = ["shape", "today held-out", "today profiled"]
    if cand:
        head += ["recommended held-out", "recommended profiled", "gain held-out",
                 "gain profiled"]
    rows = []
    for a, b in zip(ho, pr, strict=True):
        r = [f"{a['shape']} {nm[a['shape']]} p{a['percentile']}", _ms_ci(a["today"]),
             _ms_ci(b["today"])]
        if cand:
            r += [_ms_ci(a["candidate"]), _ms_ci(b["candidate"]), _pct(_gain(a)), _pct(_gain(b))]
        rows.append(r)
    d.table(_cap(platform, f"goal shapes at {level} sessions, warm"), head, rows)


def _accuracy(d: Doc, ev: Evidence, held: dict, platform: str) -> None:
    nm = names(ev)
    d.add("### Prediction accuracy", "",
          "Predictions are the loop's development verdicts (profiling slice, 2 repetitions, "
          "tail support projected to 5); the held-out verdicts are measured. Accuracy is "
          "stated for whole bundles only.", "")
    today = ev.doc["goal"]["today"]
    if ev.frozen["kind"] == "candidate":
        name = ev.frozen["config"]["name"]
        acc = accuracy(ev.frozen["predicted_effect"]["criteria"],
                       held[split.HELD_OUT][name]["verdicts"])
        d.add(f"{_cap(platform, 'recommended bundle')}: predicted and held-out verdicts agree on "
              f"{acc['agree']} of {acc['criteria']} criteria; the held-out latency fell inside "
              f"the predicted 95% interval on {acc['inside_interval']} of "
              f"{acc['latency_compared']} latency criteria. Predicted confidence: "
              f"{ev.frozen['predicted_effect']['confidence']}.", "")
        _disagree(d, acc, nm, platform, "recommended bundle")
        d.add("Per-change effects (ADR 0006): claimed only where two tested bundles differ by "
              "exactly that change; the held-out evaluation measured the bundle only.", "")
        d.table(_cap(platform, "development evidence (prediction)"),
                ["change", "isolated run", "what it shows"],
                [[p["change"], f"yes, against `{', '.join(p['against'])}`"
                  if p["isolated"] and isinstance(p.get("against"), list)
                  else (f"yes, against `{p['against']}`" if p["isolated"] else "no"),
                  (", ".join(f"{f['id']} {f['without']} -> {f['with']}"
                             for f in p.get("criteria_changed", [])) or "no criterion changed")
                  if p["isolated"] else p.get("statement", "")]
                 for p in ev.frozen["per_change"]])
    else:
        d.add(f"{_cap(platform)}: the tested candidates were not run on the held-out slice "
              "(after a refusal the evaluation measures the baselines only, ADR 0011), so "
              "their predictions cannot be scored here. Per-change effects are not claimed.",
              "")
    tp = today_prediction(ev)
    if tp is not None:
        acc = accuracy(tp["verdicts"], held[split.HELD_OUT].get(today, {}).get("verdicts") or
                       verdicts.evaluate(ev.doc, [c for c in _held_cells(ev, platform)],
                                         today, error_ceiling_fraction=_err(ev),
                                         basis="held_out")["verdicts"])
        d.add(f"{_cap(platform, f'today (`{today}`)')}: the development pool predicted today's "
              f"held-out verdicts on {acc['agree']} of {acc['criteria']} criteria; the held-out "
              f"latency fell inside the predicted interval on {acc['inside_interval']} of "
              f"{acc['latency_compared']}.", "")
        _disagree(d, acc, nm, platform, f"today (`{today}`)")


def _held_cells(ev: Evidence, platform: str) -> list[dict]:
    return [c for c in ev.cells if (c.get("platform") or "unlabeled") == platform
            and c["slice"] == split.HELD_OUT]


def _disagree(d: Doc, acc: dict, nm: dict, platform: str, who: str) -> None:
    if not acc["disagreements"]:
        return
    d.table(_cap(platform, f"{who}: predicted vs held-out, where they differ"),
            ["id", "criterion", "shape", "level", "cache", "predicted", "held-out",
             "predicted value", "held-out value"],
            [[r["id"], r["kind"].replace("_", " "),
              f"{r['shape'] or ''} {nm.get(r['shape'], '')}".strip(), r["level"],
              r["cache_state"], r["predicted"], r["held_out"], r["predicted_value"],
              r["held_out_value"]] for r in acc["disagreements"]])


def _cost(d: Doc, ev: Evidence, cells: list[dict], platform: str) -> None:
    if all(c.get("cost_per_successful_query_usd") is None for c in cells):
        d.add("### Cost", "", f"{_cap(platform)}: no cost measured on this platform "
              "(ADR 0007: cost comes from Neon).", "")
        return
    price = ev.space["cost"]["cu_hour_usd"]
    reps = ev.space["protocol"]["eval"]["repetitions"]
    window = ev.load_model["timing"]["measure_seconds"]
    d.add("### Cost", "",
          f"Four separate lines, never summed: cost per successful query over the measured "
          f"window, index build (one-time), index storage (monthly), and projected operating "
          f"cost. Price: ${price}/CU-hour, ${ev.space['cost']['storage_gb_month_usd']}/GB-month "
          "(Neon Launch).", "")
    today = ev.doc["goal"]["today"]
    rows = []
    for c in sorted((c for c in cells if c["slice"] == split.HELD_OUT),
                    key=lambda c: (c["config"], c["level"], c["cache_state"])):
        t = _cell(cells, today, c["level"], c["cache_state"], split.HELD_OUT)
        a, b = c["cost_per_successful_query_usd"], t and t["cost_per_successful_query_usd"]
        rows.append([f"`{c['config']}`", f"{c['cu']:g}", c["level"], c["cache_state"],
                     _usd_q(a), f"{a / b:.0%}" if a and b else "n/a",
                     f"{c['successful_qps']['median']:.1f}"])
    d.table(_cap(platform, "held-out slice",
                 f"cost per successful query = CU x ${price}/CU-h x measured seconds / 3600 / "
                 f"successful queries, pooled over {reps} repetitions of a {window} s measured "
                 "window at fixed size, scale-to-zero off"),
            ["config", "CU", "level", "cache", "cost per successful query", "vs today",
             "successful qps (median)"], rows)
    lines = cost_lines(ev)
    d.table(_cap(platform, "one-time and monthly lines per configuration"),
            ["config", "index build (one-time)", "index storage (monthly)",
             "projected operating (monthly)", "projection basis"],
            [[f"`{n}`", f"${v['index_build_usd']:.4f} ({v['index_build_seconds']:.0f} s at "
              f"{ev.space['capture']['build_cu']:g} CU)",
              f"${v['index_storage_usd_per_month']:.2f}",
              f"${v['projected_operating_usd_per_month']:,.2f}", v["projected_basis"]]
             for n, v in sorted(lines.items())])
    d.add("Projected operating cost assumes the configuration runs at a fixed size around the "
          "clock (730 hours a month, no autoscaling, no scale-to-zero) and stores the dataset "
          "with its indexes at the storage price; it excludes egress, extra branches, instant "
          "restore history and the prospect's real traffic shape. It is a projection, not a "
          "measurement.", "")
    ex = ev.execution
    if ex is None:
        d.add(f"{_cap(platform)}: execution cost: none. A fixture document runs no compute.", "")
        return
    total = sum(ex["cu_hours_by_task"].values())
    d.table(_cap(platform, f"benchmark execution, {ex['jobs']} GitHub jobs"),
            ["task", "CU-hours held", "cost"],
            [[k, f"{v:.1f}", f"${v * price:.2f}"]
             for k, v in sorted(ex["cu_hours_by_task"].items())]
            + [["all recorded jobs", f"{total:.1f}", f"${total * price:.2f}"],
               ["model spend (loop)", "", f"${ex['model_usd']:.2f}"]])
    for n in ex.get("notes", []):
        d.add(n)
    d.add("")


def _refusal(d: Doc, ev: Evidence) -> None:
    f = ev.frozen
    nm = names(ev)
    dev_platforms = platforms([c for cs in ev.dev_cells.values() for c in cs]) or ["unlabeled"]
    plat = dev_platforms[0]
    best = (f.get("best_tested") or {}).get("bundle")
    d.add(f"## Refusal (development evidence on {PLATFORM_LABELS.get(plat, plat)})", "",
          f"- Class: **{f['class']}** (ADR 0023 precedence). Refusal id `{f['refusal_id']}`.",
          f"- Statement: {f['statement']}.",
          f"- Stopped by {f['stop']['by']}: {f['stop']['reason']}",
          f"- Shapes named by the unmet criteria of the best tested bundle: "
          f"{_shapes(f['shapes'], nm)}.",
          f"- Spend: {f['spent']['experiments']} of {f['budgets']['max_experiments']} "
          f"experiments, {f['spent']['compute_cu_hours']:.1f} of "
          f"{f['budgets']['max_compute_cu_hours']:g} CU-h, ${f['spent']['model_usd']:.2f} of "
          f"${f['budgets']['max_model_spend_usd']:g} model.", "")
    d.table(_cap(plat, "development evidence (predictions, profiling slice)",
                 "re-predicted against the final naive pool"),
            ["tested bundle", "met", "failed", "inconclusive", "unsupported",
             "cost per successful query at the objective", "unmet shapes"],
            [[f"`{t['bundle']}`" + (f" ({BEST_LABEL})" if t["bundle"] == best else ""),
              t["predicted_counts"].get("met", 0), t["predicted_counts"].get("failed", 0),
              t["predicted_counts"].get("inconclusive", 0),
              t["predicted_counts"].get("unsupported", 0),
              _usd_q(t["usd_per_ok_query_at_objective"]), ", ".join(t["unmet_shapes"])]
             for t in f["tested"]])
    level = ev.doc["goal"]["objective"]["at_level"]
    by_bundle = {cs[0]["config"]: cs for cs in ev.dev_cells.values() if cs}
    for t in f["tested"]:
        cs = next((v for k, v in by_bundle.items() if k.endswith(t["bundle"])), None)
        if not cs:
            continue
        rows = []
        for sid in sorted({s for c in cs for s in c["by_shape"]}):
            r = [f"{sid} {nm.get(sid, '')}"]
            for state in ev.doc["method"]["cache_states"]:
                c = _cell(cs, cs[0]["config"], level, state, split.PROFILING)
                b = c["by_shape"].get(sid) if c else None
                p99 = b and b["net"].get("p99")
                r += [_pct(b["failure_fraction"], 1) if b else "n/a",
                      fmt_ms(p99["ms"]) + ("" if p99["supported"] else " (under-sampled)")
                      if p99 else "no successful queries"]
            rows.append(r)
        tag = f"`{t['bundle']}`" + (f" ({BEST_LABEL})" if t["bundle"] == best else "")
        d.table(_cap(plat, f"development cells for {tag}", f"{level} sessions"),
                ["shape", "errors+timeouts, warm", "p99 ms, warm",
                 "errors+timeouts, post-restart", "p99 ms, post-restart"], rows)


def render(ev: Evidence) -> str:
    d = Doc()
    gen = (f"`pocbench results-fixture {ev.fixture}`" if ev.fixture
           else "`pocbench results-doc`")
    d.add(f"# Results: {ev.title}", "",
          f"Phase 5 results document (RC1-633), generated by {gen} from verified evidence. "
          "Do not edit by hand.", "")
    if ev.fixture:
        d.add(f"> **Fixture document.** Built from the `{ev.fixture}` fixture: the real loop, "
              "validator, verdicts and decision code over a synthetic latency model. No figure "
              "below is a measurement.", "")
    plats = platforms(ev.cells)
    if len(plats) > 1:
        d.add(f"Results on {len(plats)} platforms, reported separately and never combined "
              "(ADR 0007): " + ", ".join(PLATFORM_LABELS.get(p, p) for p in plats) + ".", "")
    by_plat = {p: [c for c in ev.cells if (c.get("platform") or "unlabeled") == p]
               for p in plats}
    held = {p: held_out_verdicts(ev, cs) for p, cs in by_plat.items()}
    for p in plats:
        _summary(d, ev, by_plat[p], p, held[p])
    if ev.frozen["kind"] == "refusal":
        _refusal(d, ev)
    for p in plats:
        d.add(f"## {PLATFORM_LABELS.get(p, p)}: held-out results", "")
        _verdict_tables(d, ev, held[p], p)
        _regression_table(d, ev, held[p], by_plat[p], p)
        _errors_table(d, ev, by_plat[p], p)
        _slices_table(d, ev, held[p], by_plat[p], p)
        _accuracy(d, ev, held[p], p)
        _cost(d, ev, by_plat[p], p)
    d.add("## Sources and pins", "",
          f"- Criteria: `criteria/criteria.json` sha256 `{ev.criteria_sha256}` (verified).",
          f"- Frozen output: {ev.frozen['kind']} sha256 `{ev.frozen_sha256}`"
          + (" (verified)." if not ev.fixture else " (fixture decision, hashed in memory)."),
          f"- Config space sha256 `{ev.frozen['config_space_sha256'][:12]}`, development "
          f"record sha256 `{str(ev.frozen.get('dev_record_sha256'))[:12]}`.")
    for s in ev.sources:
        d.add(f"- {s}")
    d.add("")
    return "\n".join(d.lines)
