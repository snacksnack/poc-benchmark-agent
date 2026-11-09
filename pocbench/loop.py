"""The Phase 4 adaptive loop (RC1-632; ADR 0005, ADR 0006, ADR 0008, ADR 0011,
ADR 0022, ADR 0023).

Each iteration: the proposer reads the profile and every development result
so far, states a hypothesis and names one experiment (a tier and a set of
index candidates) or stops. Python validates the proposal (pocbench.space),
checks the budgets, runs the experiment through the development entry point
on the profiling slice, and logs hypothesis, experiment, observation and
budget to a hash-chained loop log.

What the model decides: which experiment to run next, and when it has seen
enough. What Python decides: whether a proposal is legal, whether a budget
allows it, which tested bundle is the recommendation (every predicted
criterion met, then the objective), and the refusal class. The model never
sees held-out data: the context builder accepts only profiling-slice
evidence and raises LeakError on anything else (R3).

The loop is resumable: its state is rebuilt from the log, so a job that hits
the runner's time limit stops between experiments and the next job continues
(R15).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pocbench import record, space, telemetry, verdicts
from pocbench.slices import LeakError

PROFILING = "profiling"
BUDGET_EXHAUSTED = "BUDGET EXHAUSTED"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"
NO_IMPROVEMENT = "NO IMPROVEMENT FOUND"
CRITERIA_UNMET = "CRITERIA UNMET"
REFUSAL_CLASSES = (INSUFFICIENT_EVIDENCE, BUDGET_EXHAUSTED, NO_IMPROVEMENT, CRITERIA_UNMET)
MAX_INVALID_IN_A_ROW = 3
REFUSAL_STATEMENT = ("no tested candidate within the permitted space and budget met the "
                     "success criteria")
PREDICTION = "prediction (profiling slice, development protocol)"


# ------------------------------------------------------------------ budgets

@dataclass(frozen=True)
class Budgets:
    max_experiments: int
    max_compute_cu_hours: float
    max_model_spend_usd: float

    @classmethod
    def from_space(cls, sp: dict) -> "Budgets":
        b = sp["budgets"]["phase4"]
        return cls(b["max_experiments"], float(b["max_compute_cu_hours"]),
                   float(b["max_model_spend_usd"]))


@dataclass
class Spend:
    experiments: int = 0
    compute_cu_hours: float = 0.0
    model_usd: float = 0.0


# ------------------------------------------------------- proposer, runner

@dataclass
class Proposal:
    hypothesis: str
    action: str  # "experiment" or "stop"
    why: str
    cu: float | None = None
    index_ids: list[str] = field(default_factory=list)
    expected: str = ""
    stop_reason: str = ""
    model_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    # Anything else the proposer returned, passed through to the validator
    # so an out-of-scope setting is rejected and logged, not dropped.
    extra: dict = field(default_factory=dict)

    def as_space_proposal(self) -> dict:
        return {"cu": self.cu, "index_ids": list(self.index_ids)} | self.extra


class Proposer(Protocol):
    def worst_case_usd(self) -> float: ...
    def propose(self, context: dict) -> Proposal: ...


@dataclass
class ExperimentResult:
    """What one development experiment hands back to the loop. `cells` are
    pooled cells (harness.pool) for the candidate; `naive_cells` the naive
    baseline pooled over every development repetition run so far."""

    bundle: space.Bundle
    slice: str
    cells: list[dict]
    naive_cells: list[dict]
    cu_hours: float
    run_ids: list[str]
    detail: dict = field(default_factory=dict)


class ExperimentAborted(RuntimeError):
    """An experiment that could not finish (e.g. the network gate rejected
    every attempt at a cell). Its compute still counts against the budget."""

    def __init__(self, reason: str, cu_hours: float):
        super().__init__(reason)
        self.cu_hours = cu_hours


class ExperimentRunner(Protocol):
    def estimate_cu_hours(self, bundle: space.Bundle) -> float: ...
    def run(self, bundle: space.Bundle) -> ExperimentResult: ...
    def naive_cells(self) -> list[dict]: ...


# ----------------------------------------------------------------- the log

class LoopLog:
    """Hash-chained JSON lines, same chain as the results record."""

    def __init__(self, path: Path, header: dict,
                 clock: Callable[[], str] = lambda: datetime.now(UTC).isoformat(
                     timespec="seconds")):
        self.path = path
        self.clock = clock  # fixtures pin it so their logs hash the same every run
        if path.exists():
            rows = record.verify(path)
            for k in ("criteria_sha256", "config_space_sha256"):
                if rows[0].get(k) != header.get(k):
                    raise record.RecordError(f"{path.name} was written under another {k}")
            self.rows = rows
            self._prev = rows[-1]["row_sha256"]
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.rows, self._prev = [], record.GENESIS
            self.append({"kind": "header", **header})

    def append(self, row: dict) -> dict:
        row = {"at": self.clock()} | row
        sealed = record._seal(row, self._prev)
        with self.path.open("a") as fh:
            fh.write(json.dumps(sealed, sort_keys=True, default=str) + "\n")
        self._prev = sealed["row_sha256"]
        self.rows.append(sealed)
        return sealed

    def of(self, kind: str) -> list[dict]:
        return [r for r in self.rows if r.get("kind") == kind]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def spend_from(log: LoopLog) -> Spend:
    s = Spend()
    for r in log.rows:
        s.model_usd += r.get("model_usd", 0.0)
        if r.get("kind") == "experiment":
            s.experiments += 1
            s.compute_cu_hours += r["cu_hours"]
        elif r.get("kind") == "aborted":
            s.compute_cu_hours += r["cu_hours"]
    return s


# ------------------------------------------------------------ the context

def require_profiling(result: ExperimentResult) -> None:
    """R3: nothing but profiling-slice evidence reaches the loop."""
    if result.slice != PROFILING:
        raise LeakError(f"experiment {result.bundle.key} returned {result.slice!r} evidence; "
                        "the loop reads the profiling slice only")
    for c in result.cells + result.naive_cells:
        if c.get("slice", PROFILING) != PROFILING:
            raise LeakError(f"a {c.get('slice')!r} cell reached the loop")


def _shape_line(cell: dict, sid: str) -> dict:
    s = cell["by_shape"].get(sid)
    if s is None:
        return {"obs": 0}
    net = s["net"]
    out = {"obs": s["observations"], "fail": round(s["failure_fraction"], 4)}
    for q in (50, 95, 99):
        st = net.get(f"p{q}")
        if st:
            out[f"p{q}"] = round(st["ms"], 1)
            out[f"p{q}_ok"] = st["supported"]
    return out


def summarize_cells(cells: list[dict]) -> list[dict]:
    """Compact per-cell view for the model: per shape latency and failures,
    throughput and cost. No raw rows."""
    out = []
    for c in sorted(cells, key=lambda c: (c["level"], c["cache_state"])):
        out.append({"level": c["level"], "cache_state": c["cache_state"],
                    "reps": c["repetitions"],
                    "qps": round(c["successful_qps"]["median"], 2),
                    "usd_per_ok_query": c["cost_per_successful_query_usd"],
                    "shapes": {sid: _shape_line(c, sid) for sid in sorted(c["by_shape"])}})
    return out


def unmet_summary(pred: dict) -> list[dict]:
    keep = ("id", "kind", "shape", "level", "cache_state", "verdict", "percentile",
            "value_ms", "bound_ms", "value", "bound", "support", "why")
    return [{k: v[k] for k in keep if k in v} for v in pred["verdicts"]
            if v["verdict"] != verdicts.MET]


def build_context(static: dict, log: LoopLog, budgets: Budgets, spend: Spend,
                  naive_cells: list[dict]) -> dict:
    iterations = []
    for r in log.rows:
        if r.get("kind") == "proposal":
            iterations.append({"iteration": r["iteration"], "hypothesis": r["hypothesis"],
                               "proposal": r["proposal"], "why": r["why"],
                               "validator": r["validator"]})
        elif r.get("kind") == "experiment":
            iterations.append({"iteration": r["iteration"], "observed": {
                "bundle": r["bundle"], "cu_hours": r["cu_hours"],
                "predicted_counts": r["prediction"]["counts"],
                "all_met": r["prediction"]["all_met"],
                "unmet": r["prediction"]["unmet"],
                "cells": r["summary"]}})
    return {"static": static,
            "naive_now": summarize_cells(naive_cells),
            "budgets": asdict(budgets),
            "spent": asdict(spend),
            "remaining": {"experiments": budgets.max_experiments - spend.experiments,
                          "compute_cu_hours": round(budgets.max_compute_cu_hours
                                                    - spend.compute_cu_hours, 2),
                          "model_usd": round(budgets.max_model_spend_usd - spend.model_usd, 4)},
            "history": iterations}


# ---------------------------------------------------------------- the loop

@dataclass
class LoopConfig:
    doc: dict  # verified criteria
    space: dict  # pinned config space data
    error_ceiling: float
    static_context: dict
    budgets: Budgets
    # Tail support projected from the development to the evaluation protocol
    # (eval repetitions / dev repetitions; verdicts.py, ADR 0022).
    support_scale: float = 1.0
    # Stop starting experiments when this returns False (job time left).
    may_start: Callable[[space.Bundle, float], bool] = lambda b, est: True


def _predict(cfg: LoopConfig, result: ExperimentResult) -> dict:
    pred = verdicts.evaluate(cfg.doc, result.cells + result.naive_cells, result.bundle.name,
                             error_ceiling_fraction=cfg.error_ceiling, basis=PREDICTION,
                             support_scale=cfg.support_scale)
    return pred | {"unmet": unmet_summary(pred)}


def terminal(log: LoopLog) -> dict | None:
    stops = log.of("stop")
    return stops[-1] if stops else None


def run_loop(cfg: LoopConfig, log: LoopLog, proposer: Proposer,
             runner: ExperimentRunner) -> dict:
    """Iterate until a stop (recorded in the log) or until `may_start` says
    this job has no time for the next experiment (returns paused)."""
    if (done := terminal(log)) is not None:
        return {"state": "stopped", "stop": done}
    invalid_in_a_row = 0
    while True:
        spend = spend_from(log)
        it = len(log.of("proposal")) + 1
        b = cfg.budgets
        stop = None
        if spend.experiments >= b.max_experiments:
            stop = ("experiments", f"experiment budget spent ({spend.experiments} of "
                                   f"{b.max_experiments})")
        elif spend.model_usd + proposer.worst_case_usd() > b.max_model_spend_usd:
            stop = ("model", f"model budget cannot cover another call (${spend.model_usd:.2f} "
                             f"of ${b.max_model_spend_usd:.2f} spent)")
        elif invalid_in_a_row >= MAX_INVALID_IN_A_ROW:
            stop = ("proposer", f"{invalid_in_a_row} invalid proposals in a row")
        if stop:
            return _stop(log, stop[0], stop[1], budget=stop[0] in ("experiments", "model"))

        # The traced span covers the decision (propose, validate, budget
        # checks), not the experiment, which runs for hours (ADR 0026).
        with telemetry.iteration(log.rows[0]["row_sha256"]) as span:
            context = build_context(cfg.static_context, log, b, spend, runner.naive_cells())
            p = proposer.propose(context)
            entry = {"kind": "proposal", "iteration": it, "hypothesis": p.hypothesis,
                     "action": p.action, "why": p.why, "expected": p.expected,
                     "proposal": p.as_space_proposal() if p.action == "experiment" else None,
                     "stop_reason": p.stop_reason, "model_usd": round(p.model_usd, 6),
                     "usage": p.usage}
            bundle = est = None
            if p.action == "stop":
                entry["validator"] = "not applicable"
            else:
                tested = {r["bundle"]["key"] for r in log.of("experiment")}
                try:
                    bundle = space.validate(p.as_space_proposal(), cfg.space, tested)
                except space.InvalidProposal as e:
                    entry["validator"] = {"valid": False, "reasons": e.reasons}
                else:
                    est = runner.estimate_cu_hours(bundle)
                    entry["validator"] = {"valid": True, "bundle": bundle.key,
                                          "estimated_cu_hours": round(est, 3)}
            log.append(entry)
            span.record(entry)
        if p.action == "stop":
            return _stop(log, "proposer", p.stop_reason or "proposer stopped", budget=False)
        if bundle is None:
            invalid_in_a_row += 1
            continue
        invalid_in_a_row = 0
        if spend.compute_cu_hours + est > b.max_compute_cu_hours:
            return _stop(log, "compute",
                         f"compute budget cannot cover {bundle.key} (estimated {est:.1f} "
                         f"CU-h; {spend.compute_cu_hours:.1f} of {b.max_compute_cu_hours:g} "
                         "spent)", budget=True)
        if not cfg.may_start(bundle, est):
            # Not a stop: the next job re-proposes from the same evidence.
            log.append({"kind": "pause", "iteration": it, "bundle": bundle.key,
                        "why": "not enough job time left for this experiment"})
            return {"state": "paused", "iteration": it}
        try:
            result = runner.run(bundle)
        except ExperimentAborted as e:
            log.append({"kind": "aborted", "iteration": it, "bundle": bundle.key,
                        "cu_hours": round(e.cu_hours, 4), "why": str(e)})
            raise
        require_profiling(result)
        pred = _predict(cfg, result)
        log.append({"kind": "experiment", "iteration": it,
                    "bundle": {"key": bundle.key, "name": bundle.name, "cu": bundle.cu,
                               "index_ids": list(bundle.index_ids), "sha256": bundle.sha256},
                    "cu_hours": round(result.cu_hours, 4), "run_ids": result.run_ids,
                    "summary": summarize_cells(result.cells),
                    "prediction": {k: pred[k] for k in ("basis", "counts", "all_met",
                                                        "unmet", "criteria_total")},
                    "detail": result.detail})


def _stop(log: LoopLog, by: str, reason: str, budget: bool) -> dict:
    row = log.append({"kind": "stop", "by": by, "reason": reason, "budget": budget})
    return {"state": "stopped", "stop": row}


# ------------------------------------------------------------ the output

def _objective_cost(cells: list[dict], name: str, level: int) -> float:
    vals = [c["cost_per_successful_query_usd"] for c in cells
            if c["config"] == name and c["level"] == level and c["cache_state"] == "warm"]
    return vals[0] if vals and vals[0] is not None else float("inf")


def _improvement(pred: dict, naive_pred: dict) -> bool:
    """A candidate improved on today if it meets more latency-ceiling and
    error-ceiling criteria than the naive baseline meets on the same cells."""
    kinds = ("latency_ceiling", "error_ceiling")

    def met(p: dict) -> int:
        return sum(v["verdict"] == verdicts.MET for v in p["verdicts"] if v["kind"] in kinds)
    return met(pred) > met(naive_pred)


def per_change(chosen: space.Bundle, tested: dict[str, dict], cells: list[dict], doc: dict,
               error_ceiling: float, support_scale: float = 1.0) -> list[dict]:
    """ADR 0006: an effect per change only where two tested bundles differ by
    exactly that change. Every other change is listed as not decomposed."""
    out = []
    for what in chosen.index_ids:
        without = space.Bundle(chosen.cu, tuple(i for i in chosen.index_ids if i != what))
        pair = tested.get(without.key)
        if pair is None:
            out.append({"change": f"add {what}", "isolated": False,
                        "statement": "no isolated run; this change's share of the bundle's "
                                     "effect is not claimed (ADR 0006)"})
            continue
        kw = {"error_ceiling_fraction": error_ceiling, "basis": PREDICTION,
              "support_scale": support_scale}
        a = verdicts.evaluate(doc, cells, without.name, **kw)
        b = verdicts.evaluate(doc, cells, chosen.name, **kw)
        flips = [{"id": x["id"], "without": x["verdict"], "with": y["verdict"]}
                 for x, y in zip(a["verdicts"], b["verdicts"], strict=True)
                 if x["verdict"] != y["verdict"]]
        out.append({"change": f"add {what}", "isolated": True, "against": without.key,
                    "criteria_changed": flips})
    tiers = [t for t in tested.values() if tuple(t["index_ids"]) == chosen.index_ids
             and t["cu"] != chosen.cu]
    out.append({"change": f"tier {chosen.cu:g} CU", "isolated": bool(tiers),
                "against": sorted(t["key"] for t in tiers),
                **({} if tiers else {"statement": "no run of the same indexes at another tier; "
                                                  "the tier's share is not claimed"})})
    return out


def decide(cfg: LoopConfig, log: LoopLog, cells_of: Callable[[str], list[dict]],
           naive_cells: list[dict], dev_record_sha256: str | None) -> dict:
    """The loop's one output, chosen in Python after the stop: a candidate
    document, or a refusal document (ADR 0011, ADR 0023). Every tested bundle
    is re-predicted against the final naive pool, so all comparisons use the
    same, largest baseline evidence."""
    stop = terminal(log)
    if stop is None:
        raise RuntimeError("the loop has not stopped; nothing to decide")
    exps = log.of("experiment")
    tested = {r["bundle"]["key"]: r["bundle"] for r in exps}
    cells = list(naive_cells)
    for r in exps:
        cells += cells_of(r["bundle"]["name"])
    preds = {}
    for key, bd in tested.items():
        p = verdicts.evaluate(cfg.doc, cells, bd["name"], error_ceiling_fraction=cfg.error_ceiling,
                              basis=PREDICTION, support_scale=cfg.support_scale)
        preds[key] = p | {"unmet": unmet_summary(p)}
    naive_pred = verdicts.evaluate(cfg.doc, cells, cfg.doc["goal"]["today"],
                                   error_ceiling_fraction=cfg.error_ceiling, basis=PREDICTION,
                                   support_scale=cfg.support_scale)
    level = cfg.doc["goal"]["objective"]["at_level"]

    def cost(key: str) -> float:
        return _objective_cost(cells, tested[key]["name"], level)

    def rank(key: str) -> tuple:
        bd = tested[key]
        return (cost(key), bd["cu"], len(bd["index_ids"]), key)

    evidence = [{"bundle": key, "cu": tested[key]["cu"], "index_ids": tested[key]["index_ids"],
                 "predicted_counts": preds[key]["counts"], "all_met": preds[key]["all_met"],
                 "usd_per_ok_query_at_objective": cost(key),
                 "unmet_shapes": verdicts.unmet_shapes(preds[key])}
                for key in sorted(tested, key=rank)]
    common = {"criteria_sha256": log.rows[0]["criteria_sha256"],
              "config_space_sha256": log.rows[0]["config_space_sha256"],
              "loop_log_sha256": log.sha256, "dev_record_sha256": dev_record_sha256,
              "stop": {"by": stop["by"], "reason": stop["reason"]},
              "spent": asdict(spend_from(log)), "budgets": asdict(cfg.budgets),
              "tested": evidence, "basis": PREDICTION}
    qualifying = [k for k in tested if preds[k]["all_met"]]
    margin = cfg.space["noise_gate"]["smallest_pass_effect"]
    if qualifying:
        key = min(qualifying, key=rank)
        bd = tested[key]
        bundle = space.Bundle(bd["cu"], tuple(bd["index_ids"]))
        per = [{k: v.get(k) for k in ("id", "kind", "shape", "level", "cache_state",
                                      "verdict", "percentile", "value_ms", "ci95_ms",
                                      "bound_ms", "value", "bound")}
               | {"confidence": verdicts.confidence(v, margin)}
               for v in preds[key]["verdicts"]]
        levels = [p["confidence"] for p in per]
        overall = ("low" if "low" in levels else "medium" if "medium" in levels else "high")
        return {"kind": "candidate", **common,
                "config": {"name": bundle.name, "cu": bundle.cu,
                           "ddl": list(space.ddl(bundle, cfg.space)),
                           "index_ids": list(bundle.index_ids)},
                "bundle_sha256": bundle.sha256,
                "chosen_by": (f"every predicted criterion met; lowest cost per successful "
                              f"query at {level} sessions, warm, among {len(qualifying)} "
                              "qualifying bundle(s)"),
                "predicted_effect": {"level": "bundle", "criteria": per,
                                     "confidence": overall,
                                     "confidence_rule": (
                                         "per criterion: high when the interval's conservative "
                                         f"end clears the bound by more than {margin:.0%} (the "
                                         "smallest pass effect), medium when met within it, low "
                                         "when not met; overall = the lowest")},
                "per_change": per_change(bundle, tested, cells, cfg.doc, cfg.error_ceiling,
                                         cfg.support_scale)}
    cls, best = refusal_class(stop, preds, naive_pred, rank)
    rid = "refusal-" + hashlib.sha256(json.dumps(common, sort_keys=True,
                                                 default=str).encode()).hexdigest()[:12]
    return {"kind": "refusal", **common, "refusal_id": rid, "class": cls,
            "statement": REFUSAL_STATEMENT,
            "shapes": verdicts.unmet_shapes(preds[best]) if best else [],
            "best_tested": None if best is None else {
                "bundle": best, "label": "best tested candidate; fails to qualify",
                "unmet": preds[best]["unmet"]}}


def refusal_class(stop: dict, preds: dict[str, dict], naive_pred: dict,
                  rank: Callable[[str], tuple]) -> tuple[str, str | None]:
    """ADR 0023. Precedence: a budget stop is BUDGET EXHAUSTED (the search was
    cut short, whatever it had found). Otherwise, with no experiment run, or a
    best candidate whose unmet criteria are all inconclusive, unsupported or
    missing (nothing outright failed), INSUFFICIENT EVIDENCE. Otherwise, if no
    tested candidate improved on today, NO IMPROVEMENT FOUND. Otherwise
    CRITERIA UNMET."""
    best = None
    if preds:
        # Most criteria met; ties go to the cheaper bundle.
        best = max(preds, key=lambda k: (preds[k]["counts"].get(verdicts.MET, 0), -rank(k)[0]))
    if stop["budget"]:
        return BUDGET_EXHAUSTED, best
    if best is None:
        return INSUFFICIENT_EVIDENCE, None
    unmet = [v["verdict"] for v in preds[best]["verdicts"] if v["verdict"] != verdicts.MET]
    if verdicts.FAILED not in unmet:
        return INSUFFICIENT_EVIDENCE, best
    if not any(_improvement(p, naive_pred) for p in preds.values()):
        return NO_IMPROVEMENT, best
    return CRITERIA_UNMET, best
