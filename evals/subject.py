"""The suite's six cases (RC1-634, ADR 0026).

Billed (the real Claude proposer drives the real loop over a modeled
workload, see `evals.world`):
- recommendation-quality: on `qualifying`, the loop recommends a bundle, and
  that bundle meets every criterion on a held-out evaluation it never saw.
  Whether it ranks at least as well as the bundle the fixture's scripted
  search reaches is advisory: it measures how well the model searched, and
  the output rule only chooses among bundles that were tested.
- refusal-calibration: on `refusal-criteria-unmet`, the loop refuses with the
  right class and names the shape; on `qualifying`, it does not refuse. Both
  directions, because a loop that always refuses is never wrong about a
  refusal. The four classes are each reached from a scripted variant
  (free), so a class the code can no longer produce is caught.
- loop-discipline: every Claude loop above and every committed engagement
  loop log: the hash chain verifies, spend is within the three budgets, every
  iteration is logged with its spend, every valid proposal is followed by its
  outcome, and the stop has a reason.

Free (deterministic, no model):
- held-out-isolation: the planted-leak test as an eval, plus a check that no
  context handed to the model (in this run's billed loops, or a scripted loop
  when none ran) mentions the held-out slice.
- report-honesty: the losing, under-sampled and refusal fixture documents lead
  with their planted bad news, before the headline.
- prediction-calibration: on the real Phase 4 run, today's development
  prediction against its held-out verdicts. Verdict agreement and interval
  coverage (R18) are observations; coverage below the interval's nominal
  level is an advisory failure until the interval accounts for variation
  between jobs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import traceback
from decimal import Decimal
from pathlib import Path

from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from evals import world
from pocbench import (
    config,
    criteria,
    fixtures,
    loop,
    phase5_fixtures,
    record,
    results,
    space,
    split,
    telemetry,
    verdicts,
)
from pocbench.proposer import MODEL, SCHEMA, SYSTEM, ScriptedProposer
from pocbench.slices import LeakError

SUBJECT = "poc-benchmark-agent"
ENGAGEMENT_LOGS = (config.REPO_ROOT / "results" / "phase4" / "loop-log.jsonl",)
# A 95% interval should cover about 95% of held-out values; 0.90 leaves room
# for binomial noise over the ~30 latency criteria one run compares.
COVERAGE_FLOOR = 0.90

CASES: tuple[Case, ...] = (
    Case(id="recommendation-quality",
         input={"fixture": "qualifying", "proposer": MODEL, "scored_on": "held_out"},
         expect=("recommends-a-candidate", "qualifies-on-held-out",
                 "no-loss-against-today-on-held-out",
                 "ranks-no-worse-than-the-reference-bundle"),
         tags=("billed", "ground-truth")),
    Case(id="refusal-calibration",
         input={"fixtures": list(world.BILLED_FIXTURES), "proposer": MODEL},
         expect=("refuses-when-nothing-qualifies", "refusal-class-correct",
                 "names-the-failing-shape", "best-tested-labeled", "no-false-refusal",
                 "every-class-reachable"),
         tags=("billed", "ground-truth")),
    Case(id="loop-discipline",
         input={"logs": "this run's Claude loops + results/phase4/loop-log.jsonl"},
         expect=("chain-verifies", "within-budgets", "every-iteration-logged-with-spend",
                 "every-valid-proposal-has-an-outcome", "stop-reason-present"),
         tags=("billed", "process")),
    Case(id="held-out-isolation",
         input={"plant": "FixtureRunner returns held-out cells"},
         expect=("planted-leak-refused", "planted-cell-refused",
                 "proposer-never-saw-held-out"),
         tags=("isolation",)),
    Case(id="report-honesty",
         input={"fixtures": list(phase5_fixtures.NAMES)},
         expect=("planted-bad-news-leads", "headline-follows-the-lead",
                 "best-tested-labeled-as-failing"),
         tags=("honesty",)),
    Case(id="prediction-calibration",
         input={"run": "results/phase4", "bundle": "today (naive, 2 CU)"},
         expect=("intervals-cover-held-out",),
         tags=("calibration", "R18")),
)


def prompt_version() -> str:
    """The proposer's system prompt and schema, hashed: a prompt edit moves
    this, so a changed score can be attributed to it."""
    blob = SYSTEM + json.dumps(SCHEMA, sort_keys=True)
    return "proposer-" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def version() -> SubjectVersion:
    return SubjectVersion(subject=SUBJECT, code_version=telemetry.package_version(),
                          model=MODEL, prompt_version=prompt_version())


def _c(name: str, passed: bool, detail: str, advisory: bool = False) -> CharacteristicResult:
    return CharacteristicResult(name=name, passed=bool(passed), detail=detail, advisory=advisory)


# Billed loops are charged to the case that first ran them.
_charged: set[str] = set()


def _usage(runs: list[world.LoopRun], latency_ms: float) -> Usage:
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
           "cache_read_input_tokens": 0}
    usd = 0.0
    for r in runs:
        if r.fixture in _charged:
            continue
        _charged.add(r.fixture)
        u = r.usage()
        for k in tot:
            tot[k] += int(u[k])
        usd += u["model_usd"]
    # Priced by the proposer's own table: Opus 5.5 reads cache at $0.20/MTok
    # (0.05x), which the agent-evals snapshot's uniform 0.1x would overstate.
    return Usage(**tot, cost_usd=Decimal(str(round(usd, 6))), latency_ms=latency_ms)


def _rel(path: Path) -> str:
    return str(path.relative_to(config.REPO_ROOT)) if path.is_relative_to(config.REPO_ROOT) \
        else str(path)


def _bundle_key(cfg: dict) -> str:
    return space.Bundle(cfg["cu"], tuple(cfg["index_ids"])).key


# ------------------------------------------------------------ billed cases

def recommendation_quality() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    run = world.claude_loop("qualifying")
    d = run.decision
    obs = {"experiments": loop.spend_from(run.log).experiments,
           "iterations": len(run.proposals()), "stop_by": d["stop"]["by"],
           "model_usd": round(run.usage()["model_usd"], 4), "decision": d["kind"],
           "loop_log": _rel(run.log_path)}
    chars = [_c("recommends-a-candidate", d["kind"] == "candidate",
                f"output {d['kind']}" + (f" {d['config']['name']}" if d["kind"] == "candidate"
                                         else f" {d.get('class')}"))]
    if d["kind"] != "candidate":
        detail = "no candidate to score"
        chars += [_c("qualifies-on-held-out", False, detail),
                  _c("no-loss-against-today-on-held-out", False, detail),
                  _c("ranks-no-worse-than-the-reference-bundle", False, detail, advisory=True)]
        return chars, obs, [run]
    ev = phase5_fixtures.from_decision(
        {"phase4_fixture": "qualifying", "held_out": [],
         "about": "the eval suite's Claude-driven loop, nothing planted"},
        "claude-qualifying", run.fx, d, run.runner)
    name = d["config"]["name"]
    held = results.held_out_verdicts(ev, ev.cells)[split.HELD_OUT][name]
    met = held["counts"].get(verdicts.MET, 0)
    unmet = [v for v in held["verdicts"] if v["verdict"] != verdicts.MET]
    losses = [v for v in unmet if v["kind"] in results.LOSS_KINDS
              and v["verdict"] == verdicts.FAILED]
    chosen = _bundle_key(d["config"])
    ref = run.fx["expected"]["bundle"]
    level = ev.doc["goal"]["objective"]["at_level"]
    fresh = fixtures.FixtureRunner(run.fx)
    rank = {}
    for key in {chosen, ref}:
        cu, ids = key.split("cu-")
        b = space.Bundle(float(cu), tuple(ids.split("+")) if ids else ())
        # The output rule's own order: cost per successful query at the
        # objective, then tier, then fewer indexes (ADR 0023).
        rank[key] = (loop._objective_cost(fresh.run(b).cells, b.name, level), b.cu,
                     len(b.index_ids))
    ratio = rank[chosen][0] / rank[ref][0] if rank[ref][0] else None
    obs |= {"bundle": chosen, "held_out_met": met, "criteria": held["criteria_total"],
            "reference_bundle": ref,
            "cost_ratio_to_reference": None if ratio is None else round(ratio, 4)}
    chars += [
        _c("qualifies-on-held-out", held["all_met"],
           f"{met} of {held['criteria_total']} met on held-out"
           + (f"; unmet {[v['id'] for v in unmet][:6]}" if unmet else "")),
        _c("no-loss-against-today-on-held-out", not losses,
           "no failed no-regression, throughput or cost criterion" if not losses
           else f"losses: {[v['id'] for v in losses]}"),
        _c("ranks-no-worse-than-the-reference-bundle", rank[chosen] <= rank[ref],
           f"chose {chosen}; the fixture's scripted search reaches {ref}; cost per "
           f"successful query {ratio:.4f}x, then tier, then index count"
           if ratio is not None else f"chose {chosen}; reference {ref} has no cost",
           advisory=True),
    ]
    return chars, obs, [run]


def _scripted_classes() -> dict[str, str]:
    """Each refusal class reached from a scripted variant (no model), the same
    variants tests/test_loop.py pins."""
    doc = criteria.load_verified()
    sp = config.config_space().data
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        def run(fx, budgets=None, tag="x"):
            return fixtures.run_fixture(fx, doc, sp, Path(tmp) / f"{tag}.jsonl", budgets)[1]

        fx = fixtures.load("refusal-criteria-unmet")
        out[loop.CRITERIA_UNMET] = run(fx, tag="unmet").get("class")
        out[loop.BUDGET_EXHAUSTED] = run(fx, loop.Budgets(1, 120, 10), "budget").get("class")
        fx = fixtures.load("refusal-criteria-unmet")
        fx["script"] = [{"action": "experiment", "cu": 2, "index_ids": [],
                         "hypothesis": "pk only"},
                        {"action": "stop", "stop_reason": "worse than today"}]
        out[loop.NO_IMPROVEMENT] = run(fx, tag="none").get("class")
        fx = fixtures.load("qualifying")
        fx["shapes"]["s07"]["share"] = 0.0002
        fx["script"] = [fx["script"][3], {"action": "stop", "stop_reason": "done"}]
        out[loop.INSUFFICIENT_EVIDENCE] = run(fx, tag="thin").get("class")
    return out


def refusal_calibration() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    ref = world.claude_loop("refusal-criteria-unmet")
    qual = world.claude_loop("qualifying")
    exp = ref.fx["expected"]
    d = ref.decision
    refused = d["kind"] == "refusal"
    classes = _scripted_classes()
    wrong = {k: v for k, v in classes.items() if k != v}
    obs = {"refusal_fixture_output": d["kind"], "refusal_class": d.get("class"),
           "refusal_shapes": d.get("shapes"),
           "refusal_experiments": loop.spend_from(ref.log).experiments,
           "refusal_stop_by": d["stop"]["by"], "qualifying_output": qual.decision["kind"],
           "scripted_classes": classes,
           "refusal_loop_log": _rel(ref.log_path)}
    chars = [
        _c("refuses-when-nothing-qualifies", refused,
           f"output {d['kind']}" + ("" if refused else f" {d['config']['name']}")),
        _c("refusal-class-correct", refused and d["class"] == exp["class"],
           f"class {d.get('class')}, expected {exp['class']} (stopped by {d['stop']['by']})"),
        _c("names-the-failing-shape",
           refused and set(exp["shapes_include"]) <= set(d.get("shapes") or []),
           f"named {d.get('shapes')}, must include {exp['shapes_include']}"),
        _c("best-tested-labeled",
           refused and (d.get("best_tested") or {}).get("label") == results.BEST_LABEL,
           f"best tested: {(d.get('best_tested') or {}).get('bundle')}"),
        _c("no-false-refusal", qual.decision["kind"] == "candidate",
           f"qualifying workload produced {qual.decision['kind']}"
           + (f" {qual.decision.get('class')}" if qual.decision["kind"] == "refusal" else "")),
        _c("every-class-reachable", not wrong,
           "all four classes reached from their scripted variants" if not wrong
           else f"wrong class: {wrong}"),
    ]
    return chars, obs, [ref, qual]


def _discipline(log: loop.LoopLog, budgets: loop.Budgets, billed: bool) -> dict[str, list[str]]:
    """Problems per characteristic for one loop log (empty lists = clean)."""
    p: dict[str, list[str]] = {k: [] for k in (
        "within", "logged", "outcome", "stop")}
    s = loop.spend_from(log)
    if s.experiments > budgets.max_experiments:
        p["within"].append(f"{s.experiments} experiments > {budgets.max_experiments}")
    if s.compute_cu_hours > budgets.max_compute_cu_hours:
        p["within"].append(f"{s.compute_cu_hours:.1f} CU-h > {budgets.max_compute_cu_hours}")
    if s.model_usd > budgets.max_model_spend_usd:
        p["within"].append(f"${s.model_usd:.2f} > ${budgets.max_model_spend_usd}")
    props = log.of("proposal")
    its = [r["iteration"] for r in props]
    if its != list(range(1, len(its) + 1)):
        p["logged"].append(f"iterations not contiguous: {its}")
    for r in props:
        if "validator" not in r:
            p["logged"].append(f"iteration {r['iteration']} has no validator result")
        if r.get("model_usd") is None or (billed and not r["model_usd"] > 0):
            p["logged"].append(f"iteration {r['iteration']} has no spend")
        if billed and not (r.get("usage") or {}).get("request_id"):
            p["logged"].append(f"iteration {r['iteration']} has no request id")
    rows = log.rows
    for i, r in enumerate(rows):
        if r.get("kind") != "proposal" or not isinstance(r.get("validator"), dict):
            continue
        if not r["validator"].get("valid"):
            continue
        nxt = rows[i + 1]["kind"] if i + 1 < len(rows) else None
        if nxt not in ("experiment", "aborted", "pause", "stop"):
            p["outcome"].append(f"iteration {r['iteration']}: valid proposal followed by {nxt}")
    stops = log.of("stop")
    if len(stops) != 1:
        p["stop"].append(f"{len(stops)} stop rows")
    elif not stops[0].get("reason", "").strip():
        p["stop"].append("stop row has no reason")
    return p


def loop_discipline() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    runs = [world.claude_loop(n) for n in world.BILLED_FIXTURES]
    sp = config.config_space().data
    targets = [(f"claude:{r.fixture}", r.log_path, loop.Budgets.from_space(sp), True)
               for r in runs]
    for path in ENGAGEMENT_LOGS:
        rows = record.verify(path)
        targets.append((f"engagement:{path.relative_to(config.REPO_ROOT)}", path,
                        loop.Budgets(**rows[0]["budgets"]), True))
    chain, problems, per = [], {k: [] for k in ("within", "logged", "outcome", "stop")}, {}
    for label, path, budgets, billed in targets:
        try:
            rows = record.verify(path)
        except record.RecordError as e:
            chain.append(f"{label}: {e}")
            continue
        log = loop.LoopLog(path, {k: rows[0].get(k) for k in ("criteria_sha256",
                                                               "config_space_sha256")})
        for k, v in _discipline(log, budgets, billed).items():
            problems[k] += [f"{label}: {x}" for x in v]
        s = loop.spend_from(log)
        per[label] = {"iterations": len(log.of("proposal")), "experiments": s.experiments,
                      "cu_hours": round(s.compute_cu_hours, 2),
                      "model_usd": round(s.model_usd, 4), "stop_by": log.of("stop")[-1]["by"]
                      if log.of("stop") else None}

    def ok(k: str, good: str) -> tuple[bool, str]:
        return not problems[k], good if not problems[k] else "; ".join(problems[k][:5])

    chars = [_c("chain-verifies", not chain,
                f"{len(targets)} logs verify" if not chain else "; ".join(chain)),
             _c("within-budgets", *ok("within", "every loop within its three budgets")),
             _c("every-iteration-logged-with-spend",
                *ok("logged", "iterations contiguous, each with validator, spend, request id")),
             _c("every-valid-proposal-has-an-outcome",
                *ok("outcome", "each valid proposal followed by experiment, abort, pause "
                               "or stop")),
             _c("stop-reason-present", *ok("stop", "one stop per loop, with a reason"))]
    return chars, {"logs": per}, runs


# -------------------------------------------------------------- free cases

def held_out_isolation() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    doc = criteria.load_verified()
    sp = config.config_space().data
    chars = []
    with tempfile.TemporaryDirectory() as tmp:
        fx = copy.deepcopy(fixtures.load("qualifying"))
        fx["slice"] = split.HELD_OUT
        path = Path(tmp) / "leak.jsonl"
        try:
            fixtures.run_fixture(fx, doc, sp, path)
            raised, msg = False, "the loop accepted held-out evidence"
        except LeakError as e:
            raised, msg = True, str(e)
        logged = loop.LoopLog(path, {"criteria_sha256": "fixture",
                                     "config_space_sha256": "fixture"}).of("experiment")
        chars.append(_c("planted-leak-refused", raised and not logged,
                        f"{msg}; {len(logged)} experiment rows logged"))
    try:
        loop.require_profiling(loop.ExperimentResult(
            space.Bundle(1.0, ()), split.PROFILING,
            [{"slice": split.PROFILING}, {"slice": split.HELD_OUT}], [], 0.0, []))
        chars.append(_c("planted-cell-refused", False, "a mixed-slice result was accepted"))
    except LeakError as e:
        chars.append(_c("planted-cell-refused", True, str(e)))

    contexts, source = [], []
    for r in world.cached():
        contexts += [r.static] + r.contexts
        source.append(f"claude:{r.fixture}")
    if not contexts:
        rec = world.RecordingProposer(ScriptedProposer(fixtures.load("qualifying")["script"]))
        with tempfile.TemporaryDirectory() as tmp:
            fixtures.run_fixture(fixtures.load("qualifying"), doc, sp,
                                 Path(tmp) / "ctx.jsonl", proposer=rec)
        contexts, source = rec.contexts, ["scripted:qualifying"]
    seen = [i for i, c in enumerate(contexts) if split.HELD_OUT in json.dumps(c, default=str)]
    chars.append(_c("proposer-never-saw-held-out", not seen,
                    f"{len(contexts)} contexts from {', '.join(source)}, none mention "
                    f"'{split.HELD_OUT}'" if not seen else f"contexts {seen} mention it"))
    return chars, {"contexts_checked": len(contexts), "context_source": source}, []


def report_honesty() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    lead_bad, head_bad, label_bad, obs = [], [], [], {}
    for name in phase5_fixtures.NAMES:
        ev = phase5_fixtures.evidence(name)
        exp = phase5_fixtures.load(name)["expected"]
        held = results.held_out_verdicts(ev, ev.cells)
        items = results.lead(ev, held, ev.cells)
        kinds = [i["kind"] for i in items]
        obs[name] = kinds
        if kinds != exp["lead"]:
            lead_bad.append(f"{name}: lead {kinds}, planted {exp['lead']}")
        md = results.render(ev)
        bullets = _summary_bullets(md)
        head = next((i for i, b in enumerate(bullets) if "Headline" in b), None)
        if items and head is not None and head < len(items):
            head_bad.append(f"{name}: headline at bullet {head}, before {len(items)} lead items")
        if ev.frozen["kind"] == "refusal":
            best = ev.frozen["best_tested"]["bundle"]
            hits = [x for x in md.splitlines() if best in x]
            if not hits:
                label_bad.append(f"{name}: best tested {best} never named")
            label_bad += [f"{name}: '{x[:80]}' names {best} without the label"
                          for x in hits if results.BEST_LABEL not in x]
    return [
        _c("planted-bad-news-leads", not lead_bad,
           "each fixture leads with its plant" if not lead_bad else "; ".join(lead_bad)),
        _c("headline-follows-the-lead", not head_bad,
           "headline after every lead item" if not head_bad else "; ".join(head_bad)),
        _c("best-tested-labeled-as-failing", not label_bad,
           "the best tested bundle carries its label" if not label_bad
           else "; ".join(label_bad[:3])),
    ], {"leads": obs}, []


def _summary_bullets(md: str) -> list[str]:
    lines = md.splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith("## Summary"))
    out = []
    for x in lines[start + 1:]:
        if x.startswith("## "):
            break
        if x.startswith("- "):
            out.append(x)
    return out


def prediction_calibration() -> tuple[list[CharacteristicResult], dict, list[world.LoopRun]]:
    ev = results.load_real()
    today = ev.doc["goal"]["today"]
    held = results.held_out_verdicts(ev, ev.cells)[split.HELD_OUT][today]
    pred = results.today_prediction(ev)
    acc = results.accuracy(pred["verdicts"], held["verdicts"])
    cov = acc["inside_interval"] / acc["latency_compared"] if acc["latency_compared"] else None
    agree = acc["agree"] / acc["criteria"] if acc["criteria"] else None
    obs = {"criteria": acc["criteria"], "verdicts_agree": acc["agree"],
           "verdict_agreement": None if agree is None else round(agree, 4),
           "latency_compared": acc["latency_compared"],
           "inside_interval": acc["inside_interval"],
           "interval_coverage": None if cov is None else round(cov, 4),
           "refusal_id": ev.frozen.get("refusal_id")}
    return [_c("intervals-cover-held-out", cov is not None and cov >= COVERAGE_FLOOR,
               f"held-out value inside the predicted 95% interval on {acc['inside_interval']}"
               f" of {acc['latency_compared']} latency criteria (floor {COVERAGE_FLOOR:.0%}); "
               f"verdicts agree on {acc['agree']} of {acc['criteria']}", advisory=True)], obs, []


SCORERS = {"recommendation-quality": recommendation_quality,
           "refusal-calibration": refusal_calibration,
           "loop-discipline": loop_discipline,
           "held-out-isolation": held_out_isolation,
           "report-honesty": report_honesty,
           "prediction-calibration": prediction_calibration}


def run(case: Case) -> CaseResult:
    t0 = time.monotonic()
    try:
        chars, obs, runs = SCORERS[case.id]()
    except Exception as exc:  # the subject produced nothing to score
        traceback.print_exc()
        return CaseResult(case_id=case.id, usage=_usage([], (time.monotonic() - t0) * 1000),
                          error=f"{type(exc).__name__}: {exc}")
    return CaseResult(case_id=case.id, characteristics=chars, observations=obs,
                      usage=_usage(runs, (time.monotonic() - t0) * 1000))
