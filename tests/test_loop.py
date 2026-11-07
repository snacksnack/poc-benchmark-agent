"""Phase 4 loop tests (RC1-632): validator, verdicts, budgets, the planted
held-out read, the evaluation refusal before a freeze, and the two fixture
workloads end to end. No database, no model."""

import copy
import json

import pytest

from pocbench import (
    candidate,
    config,
    criteria,
    fixtures,
    harness,
    loop,
    phase4,
    space,
    verdicts,
)
from pocbench.proposer import SCHEMA, ScriptedProposer, usd
from pocbench.slices import LeakError

DOC = criteria.load_verified()
SP = config.config_space().data
LM = config.load_model().data
ERR = LM["errors"]["error_ceiling_fraction"]
ALL = ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8"]


# --------------------------------------------------------------- validator

def test_validator_accepts_a_permitted_bundle():
    b = space.validate({"cu": 1, "index_ids": ["I7", "I2"]}, SP)
    assert b == space.Bundle(1.0, ("I2", "I7")) and b.key == "1cu-I2+I7"
    assert space.ddl(b, SP)[0].startswith("CREATE INDEX ON events (account_id")


def test_validator_rejects_a_planted_illegal_tier():
    with pytest.raises(space.InvalidProposal, match="tier 3 CU is not in the permitted tiers"):
        space.validate({"cu": 3, "index_ids": ["I1"]}, SP)
    with pytest.raises(space.InvalidProposal, match="cu must be one of"):
        space.validate({"cu": "2", "index_ids": []}, SP)


@pytest.mark.parametrize("param", ["shared_buffers", "effective_cache_size", "work_mem",
                                   "maintenance_work_mem", "autoscaling_limit_max_cu"])
def test_validator_rejects_out_of_scope_parameters(param):
    with pytest.raises(space.InvalidProposal, match=f"{param} is out of scope"):
        space.validate({"cu": 2, "index_ids": [], param: "64MB"}, SP)


def test_validator_rejects_anything_else():
    with pytest.raises(space.InvalidProposal, match="not part of the permitted space"):
        space.validate({"cu": 2, "index_ids": [], "random_page_cost": 1.1}, SP)
    with pytest.raises(space.InvalidProposal, match="out of scope"):
        space.validate({"cu": 2, "index_ids": [], "ddl": "CREATE INDEX ON events (id)"}, SP)
    with pytest.raises(space.InvalidProposal, match="I9 is not an index candidate"):
        space.validate({"cu": 2, "index_ids": ["I9"]}, SP)
    with pytest.raises(space.InvalidProposal, match="duplicate"):
        space.validate({"cu": 2, "index_ids": ["I1", "I1"]}, SP)
    with pytest.raises(space.InvalidProposal, match="already tested"):
        space.validate({"cu": 2, "index_ids": ["I1"]}, SP, tested={"2cu-I1"})


def test_redundant_index_is_rejected():
    a = space.IndexCandidate("A", "", "events", "btree", ("contact_id",), False, 0, "")
    b = space.IndexCandidate("B", "", "events", "btree", ("contact_id", "occurred_at"), False, 0,
                             "")
    brin = space.IndexCandidate("C", "", "events", "brin", ("contact_id",), False, 0, "")
    assert space.redundant_pairs([a, b, brin]) == [("A", "B")]
    # The real candidates contain no redundant pair.
    assert space.redundant_pairs(list(space.parse_candidates(SP).values())) == []


# ---------------------------------------------------------------- verdicts

def _stat(ms, supported=True, w=0.05, spread=0.02):
    return {"ms": ms, "ci95_ms": [ms * (1 - w), ms * (1 + w)], "supported": supported,
            "observations_beyond": 500 if supported else 5,
            "distinct_beyond": 50 if supported else 1, "spread": spread}


def _cell(name, shape_ms, fail=0.0, qps=10.0, cost=1e-5, supported=True, spread=0.02):
    return {"config": name, "level": 8, "cache_state": "warm",
            "by_shape": {"s01": {"observations": 1000, "failure_fraction": fail,
                                 "net": {"ok": 1000, **{f"p{q}": _stat(shape_ms * k, supported,
                                                                       spread=spread)
                                                        for q, k in ((50, 1), (95, 2), (99, 3))}}}},
            "successful_qps": {"median": qps, "per_rep": [qps, qps * (1 + spread)]},
            "cost_per_successful_query_usd": cost}


def test_ceiling_verdicts_follow_the_interval():
    c = {"percentile": 99, "ceiling_ms": 100.0, "shape": "s01"}
    assert verdicts.latency_ceiling(c, _cell("x", 20))["verdict"] == verdicts.MET   # 60 ms
    assert verdicts.latency_ceiling(c, _cell("x", 50))["verdict"] == verdicts.FAILED  # 150
    assert verdicts.latency_ceiling(c, _cell("x", 33.4))["verdict"] == verdicts.INCONCLUSIVE
    assert verdicts.latency_ceiling(c, _cell("x", 1, supported=False))["verdict"] == \
        verdicts.UNSUPPORTED


def test_no_regression_and_the_noise_rule():
    c = {"shape": "s01", "tolerance": 0.10}
    base = _cell("naive", 100)
    assert verdicts.no_regression(c, _cell("x", 100), base, 0.2, ERR)["verdict"] == verdicts.MET
    assert verdicts.no_regression(c, _cell("x", 200), base, 0.2, ERR)["verdict"] == verdicts.FAILED
    noisy = _cell("naive", 100, spread=0.3)
    v = verdicts.no_regression(c, _cell("x", 50), noisy, 0.2, ERR)
    assert v["verdict"] == verdicts.INCONCLUSIVE and "noise rule" in v["why"]
    # Falls back to a lower percentile supported on both sides.
    thin = _cell("naive", 100)
    for q in ("p99", "p95"):
        thin["by_shape"]["s01"]["net"][q]["supported"] = False
    assert verdicts.no_regression(c, _cell("x", 50), thin, 0.2, ERR)["percentile"] == 50


def test_failing_baseline_is_noted_as_survivors_only():
    v = verdicts.no_regression({"shape": "s01", "tolerance": 0.1}, _cell("x", 10),
                               _cell("naive", 100, fail=0.4), 0.2, ERR)
    assert any("survivors only" in n for n in v["notes"])


def test_evaluate_covers_every_frozen_criterion_and_missing_cells_are_not_met():
    out = verdicts.evaluate(DOC, [], "x", error_ceiling_fraction=ERR, basis="test")
    assert out["criteria_total"] == 80 and out["counts"] == {verdicts.MISSING: 80}
    assert not out["all_met"]


# ------------------------------------------------------------ the fixtures

def test_qualifying_fixture_recommends_the_cheapest_qualifying_bundle(tmp_path):
    fx = fixtures.load("qualifying")
    result, decision, runner = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    assert result["state"] == "stopped"
    assert runner.ran == ["2cu-" + "+".join(ALL), "1cu-" + "+".join(ALL),
                          "0.5cu-" + "+".join(ALL)]
    assert decision["kind"] == "candidate"
    assert decision["config"]["name"] == space.Bundle(1.0, tuple(ALL)).name
    assert len(decision["config"]["ddl"]) == 8
    assert decision["predicted_effect"]["level"] == "bundle"
    assert len(decision["predicted_effect"]["criteria"]) == 80
    # No index was run in isolation: no per-change effect is claimed for any.
    idx = [c for c in decision["per_change"] if c["change"].startswith("add")]
    assert idx and not any(c["isolated"] for c in idx)
    # The tier change was isolated (same indexes at 2 and 0.5 CU).
    (tier,) = [c for c in decision["per_change"] if c["change"].startswith("tier")]
    assert tier["isolated"]
    # The 0.5 CU bundle failed the daily report.
    half = next(t for t in decision["tested"] if t["cu"] == 0.5)
    assert not half["all_met"] and "s07" in half["unmet_shapes"]


def test_qualifying_fixture_logs_planted_rejections_without_counting_them(tmp_path):
    fx = fixtures.load("qualifying")
    fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    log = loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "fixture",
                                                "config_space_sha256": "fixture"})
    rejected = [r for r in log.of("proposal") if r["validator"] != "not applicable"
                and not r["validator"]["valid"]]
    reasons = [x for r in rejected for x in r["validator"]["reasons"]]
    assert any("tier 3 CU" in x for x in reasons)
    assert any("work_mem is out of scope" in x for x in reasons)
    assert loop.spend_from(log).experiments == fx["expected"]["experiments"]
    assert loop.terminal(log)["by"] == "proposer"


def test_refusal_fixture_returns_its_class(tmp_path):
    fx = fixtures.load("refusal-criteria-unmet")
    _, decision, runner = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    assert len(runner.ran) == fx["expected"]["experiments"]
    assert decision["kind"] == "refusal"
    assert decision["class"] == fx["expected"]["class"] == loop.CRITERIA_UNMET
    assert set(fx["expected"]["shapes_include"]) <= set(decision["shapes"])
    assert decision["statement"] == loop.REFUSAL_STATEMENT
    assert decision["best_tested"]["label"] == "best tested candidate; fails to qualify"
    assert decision["refusal_id"].startswith("refusal-")


def test_budget_stop_is_budget_exhausted(tmp_path):
    fx = fixtures.load("refusal-criteria-unmet")
    budgets = loop.Budgets(max_experiments=1, max_compute_cu_hours=120, max_model_spend_usd=10)
    result, decision, runner = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl",
                                                    budgets)
    assert len(runner.ran) == 1 and result["stop"]["by"] == "experiments"
    assert decision["class"] == loop.BUDGET_EXHAUSTED


def test_model_and_compute_budgets_stop_the_loop(tmp_path):
    fx = fixtures.load("qualifying")
    fx["usd_per_call"] = 4.0
    _, decision, runner = fixtures.run_fixture(
        fx, DOC, SP, tmp_path / "a.jsonl", loop.Budgets(20, 120, 10))
    assert decision["stop"]["by"] == "model"  # 2 calls spent; a third would pass $10
    fx = fixtures.load("qualifying")
    _, decision, runner = fixtures.run_fixture(
        fx, DOC, SP, tmp_path / "b.jsonl", loop.Budgets(20, 2.0, 10))
    assert runner.ran == [] and decision["stop"]["by"] == "compute"
    assert decision["class"] == loop.BUDGET_EXHAUSTED


def test_unsupported_tails_refuse_as_insufficient_evidence(tmp_path):
    fx = fixtures.load("qualifying")
    fx["shapes"]["s07"]["share"] = 0.0002  # too rare to support a p95
    fx["script"] = [fx["script"][3], {"action": "stop", "stop_reason": "done"}]
    _, decision, _ = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    assert decision["kind"] == "refusal" and decision["class"] == loop.INSUFFICIENT_EVIDENCE


def test_no_improvement_over_today(tmp_path):
    fx = fixtures.load("refusal-criteria-unmet")
    fx["script"] = [{"action": "experiment", "cu": 2, "index_ids": [], "hypothesis": "pk only"},
                    {"action": "stop", "stop_reason": "worse than today"}]
    _, decision, _ = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    assert decision["class"] == loop.NO_IMPROVEMENT


def test_three_invalid_proposals_in_a_row_stop_the_loop(tmp_path):
    fx = fixtures.load("qualifying")
    fx["script"] = [{"action": "experiment", "cu": 3, "index_ids": []}] * 4
    result, decision, runner = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    assert runner.ran == [] and result["stop"]["by"] == "proposer"
    assert decision["class"] == loop.INSUFFICIENT_EVIDENCE


# ------------------------------------------------------ held-out isolation

def test_planted_held_out_evidence_never_reaches_the_loop(tmp_path):
    fx = copy.deepcopy(fixtures.load("qualifying"))
    fx["slice"] = "held_out"  # the runner hands back held-out evidence
    with pytest.raises(LeakError, match="profiling slice only"):
        fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    log = loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "fixture",
                                                "config_space_sha256": "fixture"})
    assert not log.of("experiment")  # nothing was observed or logged as evidence


def test_a_planted_held_out_cell_is_refused():
    r = loop.ExperimentResult(space.Bundle(1.0, ()), "profiling",
                              [{"slice": "held_out"}], [], 0.0, [])
    with pytest.raises(LeakError):
        loop.require_profiling(r)


def test_context_carries_profiling_evidence_and_budgets_only(tmp_path):
    fx = fixtures.load("qualifying")
    fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    # The proposer saw budgets, history and naive cells, all profiling.
    p = ScriptedProposer([])
    ctx_log = loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "fixture",
                                                    "config_space_sha256": "fixture"})
    ctx = loop.build_context({}, ctx_log, loop.Budgets.from_space(SP),
                             loop.spend_from(ctx_log),
                             fixtures.FixtureRunner(fx).naive_cells())
    assert "held_out" not in json.dumps(ctx)
    assert ctx["remaining"]["experiments"] == 20 - 3
    assert p.worst_case_usd() == 0.0


def test_evaluation_refuses_before_a_freeze(tmp_path):
    with pytest.raises(candidate.NotFrozenError, match="refuses to run"):
        harness.require_frozen(harness.criteria_sha256(DOC), path=tmp_path / "f.json",
                               pin_path=tmp_path / "f.sha256")


def test_a_decision_freezes_and_unlocks_evaluation(tmp_path):
    fx = fixtures.load("qualifying")
    _, decision, _ = fixtures.run_fixture(fx, DOC, SP, tmp_path / "log.jsonl")
    sha = harness.criteria_sha256(DOC)
    decision["criteria_sha256"] = sha  # fixtures run under a placeholder pin
    f, pin = tmp_path / "frozen.json", tmp_path / "frozen.sha256"
    candidate.freeze(decision, f, pin)
    frozen = harness.require_frozen(sha, path=f, pin_path=pin)
    assert frozen.kind == "candidate" and frozen.verified
    cfgs = harness.eval_configs(frozen, [harness.Config("default", 2), harness.Config("naive", 2)])
    assert cfgs[-1].name == decision["config"]["name"]


def test_the_loop_resumes_from_its_log(tmp_path):
    fx = fixtures.load("qualifying")
    lm_err = ERR
    log = loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "f",
                                                "config_space_sha256": "f"})
    starts = iter([True, False])
    cfg = loop.LoopConfig(DOC, SP, lm_err, {}, loop.Budgets.from_space(SP),
                          may_start=lambda b, est: next(starts))
    runner = fixtures.FixtureRunner(fx)
    out = loop.run_loop(cfg, log, ScriptedProposer(fx["script"]), runner)
    assert out["state"] == "paused" and len(runner.ran) == 1
    # Next job: same log, the proposer continues from the evidence.
    log2 = loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "f",
                                                 "config_space_sha256": "f"})
    cfg.may_start = lambda b, est: True
    out = loop.run_loop(cfg, log2, ScriptedProposer(fx["script"][3:]), runner)
    assert out["state"] == "stopped" and len(runner.ran) == 3
    assert loop.run_loop(cfg, log2, ScriptedProposer([]), runner)["state"] == "stopped"
    with pytest.raises(Exception, match="another criteria_sha256"):
        loop.LoopLog(tmp_path / "log.jsonl", {"criteria_sha256": "g",
                                              "config_space_sha256": "f"})


# ----------------------------------------------------------- the proposer

def test_proposal_schema_names_only_the_permitted_fields():
    assert SCHEMA["additionalProperties"] is False
    assert {"cu", "index_ids"} <= set(SCHEMA["properties"])
    assert not set(SCHEMA["properties"]) & set(space.OUT_OF_SCOPE)


def test_model_spend_from_usage():
    class U:
        input_tokens, output_tokens = 10_000, 5_000
        cache_creation_input_tokens, cache_read_input_tokens = 0, 20_000
    assert usd(U) == pytest.approx((10_000 * 4 + 5_000 * 20 + 20_000 * 0.2) / 1e6)


# ------------------------------------------------- projection, phase4 glue

def test_tail_support_is_projected_only_for_predictions():
    stat = {"supported": False, "observations_beyond": 60, "distinct_beyond": 40}
    floors = DOC["method"]["under_sampled"]
    held = verdicts.Support(floors["tail_beyond"], floors["tail_distinct_beyond"])
    pred = verdicts.Support(floors["tail_beyond"], floors["tail_distinct_beyond"], 2.5)
    assert held.of(stat) is None  # held-out verdicts use measured support
    assert pred.of(stat) == "projected"  # 60 x 2.5 = 150 >= 100
    # Distinct instances do not grow with repetitions (seeded replay).
    assert pred.of(stat | {"distinct_beyond": 5}) is None
    c = {"percentile": 99, "ceiling_ms": 100.0, "shape": "s01"}
    cell = _cell("x", 10, supported=False)
    for q in ("p50", "p95", "p99"):
        cell["by_shape"]["s01"]["net"][q] |= {"observations_beyond": 60, "distinct_beyond": 40}
    assert verdicts.latency_ceiling(c, cell, held)["verdict"] == verdicts.UNSUPPORTED
    v = verdicts.latency_ceiling(c, cell, pred)
    assert v["verdict"] == verdicts.MET and v["support"] == "projected"


def test_static_context_is_profiling_only_and_names_the_space():
    prof = phase4._profile()
    p3 = json.loads((config.REPO_ROOT / "results/phase3/baselines.json").read_text())
    static = phase4.static_context(DOC, SP, prof, p3)
    assert [i["id"] for i in static["index_candidates"]] == ALL
    assert [t["cu"] for t in static["tiers"]] == SP["tiers"]["compute_units"]
    assert len(static["latency_ceilings"]) == 16 and len(static["shapes"]) == 9
    assert "held_out" not in json.dumps(static["phase3_baselines_dev"])
    builds = phase4.build_seconds_by_id(prof, SP)
    assert builds["I1"] == 198.0 and builds["I4"] == 3.0


def test_next_task_is_the_loop_until_a_freeze(monkeypatch, tmp_path):
    monkeypatch.setattr(candidate, "FROZEN_PATH", tmp_path / "none.json")
    assert phase4.next_task() == "loop"


def test_phase4_loop_config_wires_may_start_and_projection(tmp_path):
    s, doc = harness.open_session("development", tmp_path / "raw", tmp_path / "r.jsonl")

    def gate(b, est):
        return False

    cfg = phase4.loop_config(s, doc, {}, gate)
    assert cfg.may_start is gate and cfg.support_scale == 2.5
    assert cfg.budgets == loop.Budgets(20, 120.0, 10.0)
