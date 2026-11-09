"""Phase 4 fixture workloads (RC1-632): the loop end to end without Neon or a
model.

A fixture is a small latency model (fixtures/phase4/*.json): per shape, its
net latency with primary keys only, the index candidates that serve it and
its latency when served, how it scales with tier and concurrency, and the
naive baseline's latency. `FixtureRunner` turns a bundle into the pooled
cells a development experiment would produce, with closed-loop observation
counts (fast shapes earn tail support, slow ones do not) and timeouts above
the load model's per-query timeout. A scripted proposer then drives the real
loop, validator, verdicts and decision code, and the fixture names the
output it must produce.
"""

import json
import math
from pathlib import Path

from pocbench import config, loop, space
from pocbench.proposer import ScriptedProposer

FIXTURE_DIR = config.REPO_ROOT / "fixtures" / "phase4"
DEV_LEVELS = (8, 32)
CACHE_STATES = ("warm", "post-restart-with-eviction")
REPS = 2
WINDOW_S = 300
TIMEOUT_MS = 5000
TAIL = {50: 1.0, 95: 1.6, 99: 2.2}  # percentile / median


def load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def _shape_ms(sh: dict, bundle_ids: set[str], cu: float, level: int, base_cu: float) -> float:
    served = any(i in bundle_ids for i in sh.get("served_by", []))
    ms = sh["served_ms"] if served else sh["pk_ms"]
    ms *= (base_cu / cu) ** sh.get("tier_exponent", 0.5)
    ms *= sh.get("level_factor", {}).get(str(level), 1.0)
    return ms


def _stat(ms: float, q: int, obs_ok: int, spread: float) -> dict:
    v = ms * TAIL[q]
    beyond = int(obs_ok * (100 - q) / 100)
    w = min(0.5, 2 / math.sqrt(max(beyond, 1)))
    return {"ms": round(v, 3), "ci95_ms": [round(v * (1 - w), 3), round(v * (1 + w), 3)],
            "observations_beyond": beyond, "distinct_beyond": beyond,
            "supported": beyond >= 100, "per_rep_ms": [v, v * (1 + spread)],
            "spread": spread}


def cells_for(fx: dict, name: str, latency: dict[str, float], cu: float, level: int,
              cs: str) -> dict:
    """One pooled cell from per-shape median latencies (ms)."""
    shares = {sid: sh["share"] for sid, sh in fx["shapes"].items()}
    mean = sum(shares[s] * min(latency[s], TIMEOUT_MS) for s in shares) / sum(shares.values())
    total = level * REPS * WINDOW_S * 1000 / mean
    by_shape, ok_total = {}, 0
    for sid, ms in latency.items():
        obs = int(total * shares[sid])
        fail = 1.0 if ms * TAIL[95] > TIMEOUT_MS * 1.5 else (
            0.05 if ms * TAIL[99] > TIMEOUT_MS else 0.0)
        ok = int(obs * (1 - fail))
        ok_total += ok
        net = {"ok": ok}
        if ok:
            net |= {f"p{q}": _stat(ms, q, ok, fx.get("spread", 0.03)) for q in TAIL}
        by_shape[sid] = {"observations": obs, "failure_fraction": fail,
                         "over_error_ceiling": fail > 0.01, "net": net}
    qps = ok_total / (REPS * WINDOW_S)
    cost = cu * fx["cu_hour_usd"] * REPS * WINDOW_S / 3600
    spread = fx.get("spread", 0.03)
    return {"slice": "profiling", "config": name, "cu": cu, "level": level, "cache_state": cs,
            "repetitions": REPS, "by_shape": by_shape,
            "successful_qps": {"median": qps, "per_rep": [qps, qps * (1 + spread)]},
            "cost_per_successful_query_usd": cost / ok_total if ok_total else None}


class FixtureRunner:
    def __init__(self, fx: dict, cu_hours_per_cu: float = 1.6):
        self.fx = fx
        self.cu_hours_per_cu = cu_hours_per_cu
        self.ran: list[str] = []
        self.cells_by_name: dict[str, list[dict]] = {}

    def naive_cells(self) -> list[dict]:
        nv = self.fx["naive"]
        return [cells_for(self.fx, "naive", {s: nv[s] * self.fx["shapes"][s].get(
            "level_factor", {}).get(str(lvl), 1.0) for s in nv}, self.fx["base_cu"], lvl, cs)
            for lvl in DEV_LEVELS for cs in CACHE_STATES]

    def estimate_cu_hours(self, bundle: space.Bundle) -> float:
        return (bundle.cu + self.fx["base_cu"]) * self.cu_hours_per_cu / 2

    def run(self, bundle: space.Bundle) -> loop.ExperimentResult:
        self.ran.append(bundle.key)
        ids = set(bundle.index_ids)
        cells = [cells_for(self.fx, bundle.name,
                           {sid: _shape_ms(sh, ids, bundle.cu, lvl, self.fx["base_cu"])
                            for sid, sh in self.fx["shapes"].items()}, bundle.cu, lvl, cs)
                 for lvl in DEV_LEVELS for cs in CACHE_STATES]
        self.cells_by_name[bundle.name] = cells
        return loop.ExperimentResult(bundle, self.fx.get("slice", "profiling"), cells,
                                     self.naive_cells(), self.estimate_cu_hours(bundle),
                                     [f"fx-{bundle.key}"])


def support_scale(sp: dict) -> float:
    """Evaluation repetitions over development repetitions."""
    return sp["protocol"]["eval"]["repetitions"] / sp["protocol"]["dev"]["repetitions"]


def run_fixture(fx: dict, doc: dict, sp: dict, log_path: Path, budgets: loop.Budgets | None = None
                ) -> tuple[dict, dict, FixtureRunner]:
    """Drive the real loop over a fixture. Returns (loop result, decision,
    runner)."""
    lm = config.load_model().data
    budgets = budgets or loop.Budgets.from_space(sp)
    log = loop.LoopLog(log_path, {"criteria_sha256": "fixture", "config_space_sha256": "fixture",
                                  "fixture": fx["name"]})
    cfg = loop.LoopConfig(doc, sp, lm["errors"]["error_ceiling_fraction"],
                          {"fixture": fx["name"]}, budgets, support_scale=support_scale(sp))
    runner = FixtureRunner(fx)
    proposer = ScriptedProposer(fx["script"], fx.get("usd_per_call", 0.05))
    result = loop.run_loop(cfg, log, proposer, runner)
    decision = loop.decide(cfg, log, lambda n: runner.cells_by_name[n], runner.naive_cells(),
                           None)
    return result, decision, runner
