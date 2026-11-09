"""The eval suite's scorers, offline (RC1-634). The billed cases run here with
the scripted proposer in place of Claude, so CI proves the scoring without
spending anything; the suite itself runs by hand and from the weekly sweep."""

import json

import pytest

from evals import __main__ as suite
from evals import subject, world
from pocbench import config, criteria, fixtures, loop, phase5_fixtures, split, telemetry
from pocbench.proposer import ScriptedProposer

DOC = criteria.load_verified()
SP = config.config_space().data


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    """Claude replaced by each fixture's own script; loop logs in tmp."""
    state = {}
    real_load = fixtures.load

    def load(name):
        fx = real_load(name)
        state["script"] = fx["script"]
        return fx

    monkeypatch.setattr(world.fixtures, "load", load)
    monkeypatch.setattr(world, "ClaudeProposer",
                        lambda static: ScriptedProposer(state["script"], 0.07))
    monkeypatch.setattr(world, "LOOP_DIR", tmp_path / "loops")
    monkeypatch.setattr(world, "_cache", {})
    monkeypatch.setattr(subject, "_charged", set())


def _by_name(chars):
    return {c.name: c for c in chars}


def test_recommendation_quality_scores_the_held_out_evaluation(scripted):
    r = subject.run(subject.CASES[0])
    assert r.error is None and r.passed
    c = _by_name(r.characteristics)
    assert c["qualifies-on-held-out"].passed and "80 of 80" in c["qualifies-on-held-out"].detail
    assert c["ranks-no-worse-than-the-reference-bundle"].advisory
    assert c["ranks-no-worse-than-the-reference-bundle"].passed
    assert r.observations["cost_ratio_to_reference"] == 1.0
    assert float(r.usage.cost_usd) == pytest.approx(0.07 * 6)


def test_dropping_an_index_the_criteria_do_not_need_still_ranks_second(scripted, monkeypatch):
    # What Claude chose on the first billed run: no I8. It qualifies, but s08
    # at 4 ms instead of 2 ms costs 0.17% more per successful query, so the
    # output rule ranks the eight-index reference first. Advisory, honestly red.
    script = [{"action": "experiment", "cu": 1, "index_ids": [f"I{i}" for i in range(1, 8)],
               "hypothesis": "seven"}, {"action": "stop", "stop_reason": "done"}]
    monkeypatch.setattr(world, "ClaudeProposer", lambda static: ScriptedProposer(script, 0.07))
    r = subject.run(subject.CASES[0])
    c = _by_name(r.characteristics)
    assert r.passed and c["qualifies-on-held-out"].passed
    assert not c["ranks-no-worse-than-the-reference-bundle"].passed
    assert r.observations["cost_ratio_to_reference"] == pytest.approx(1.0017, abs=1e-4)


def test_refusal_calibration_checks_both_directions(scripted):
    r = subject.run(subject.CASES[1])
    assert r.passed, [c.detail for c in r.characteristics if not c.passed]
    assert r.observations["scripted_classes"] == {k: k for k in suite.CLASSES}
    # The qualifying loop was already paid for by this case, so the next one
    # that reads it is not charged again.
    assert subject.run(subject.CASES[0]).usage.cost_usd == 0


def test_a_loop_that_never_stops_is_a_miscalibrated_refusal(scripted, monkeypatch):
    # Keep proposing until the experiment budget stops it: BUDGET EXHAUSTED,
    # not the CRITERIA UNMET the workload calls for.
    combos = [{"action": "experiment", "cu": cu, "index_ids": ids, "hypothesis": "more"}
              for cu in (0.25, 0.5, 1, 2, 4)
              for ids in (["I4"], ["I4", "I1"], ["I4", "I2"], ["I4", "I3"], ["I4", "I7"])]
    monkeypatch.setattr(world, "ClaudeProposer", lambda static: ScriptedProposer(combos, 0.07))
    r = subject.run(subject.CASES[1])
    c = _by_name(r.characteristics)
    assert not c["refusal-class-correct"].passed
    assert "BUDGET EXHAUSTED" in c["refusal-class-correct"].detail


def test_scripted_loops_miss_the_request_id_discipline_check(scripted):
    r = subject.run(subject.CASES[2])
    c = _by_name(r.characteristics)
    assert c["chain-verifies"].passed and c["within-budgets"].passed
    assert c["stop-reason-present"].passed
    assert not c["every-iteration-logged-with-spend"].passed  # no request ids
    assert "engagement:results/phase4/loop-log.jsonl" in r.observations["logs"]


def _log(tmp_path, rows):
    log = loop.LoopLog(tmp_path / "l.jsonl", {"criteria_sha256": "f",
                                              "config_space_sha256": "f"})
    for r in rows:
        log.append(r)
    return log


def test_discipline_flags_gaps_overspend_and_a_missing_stop(tmp_path):
    ok = {"valid": True, "bundle": "x", "estimated_cu_hours": 1.0}
    log = _log(tmp_path, [
        {"kind": "proposal", "iteration": 1, "validator": ok, "model_usd": 6.0,
         "usage": {"request_id": "r1"}},
        {"kind": "experiment", "iteration": 1, "cu_hours": 1.0},
        {"kind": "proposal", "iteration": 3, "validator": ok, "model_usd": 6.0,
         "usage": {"request_id": "r3"}},
    ])
    p = subject._discipline(log, loop.Budgets(20, 120, 10), billed=True)
    assert any("$12.00 > $10" in x for x in p["within"])
    assert any("not contiguous" in x for x in p["logged"])
    assert any("followed by None" in x for x in p["outcome"])
    assert p["stop"] == ["0 stop rows"]


def test_isolation_passes_and_reads_scripted_contexts_when_nothing_was_billed(scripted):
    r = subject.run(subject.CASES[3])
    assert r.passed
    assert r.observations["context_source"] == ["scripted:qualifying"]


def test_isolation_catches_a_context_that_mentions_held_out(scripted, monkeypatch):
    subject.run(subject.CASES[0])  # caches a loop run
    run = world.cached()[0]
    run.contexts.append({"naive_now": [{"slice": split.HELD_OUT}]})
    c = _by_name(subject.run(subject.CASES[3]).characteristics)
    assert not c["proposer-never-saw-held-out"].passed


def test_report_honesty_fails_when_a_plant_does_not_lead(monkeypatch):
    assert subject.run(subject.CASES[4]).passed
    real = phase5_fixtures.load

    def load(name):
        d = real(name)
        if name == "losing":
            d["expected"]["lead"] = ["missed"]
        return d

    monkeypatch.setattr(phase5_fixtures, "load", load)
    c = _by_name(subject.run(subject.CASES[4]).characteristics)
    assert not c["planted-bad-news-leads"].passed


def test_prediction_calibration_reproduces_the_phase5_figures():
    r = subject.run(subject.CASES[5])
    assert r.passed  # advisory only
    o = r.observations
    assert (o["inside_interval"], o["latency_compared"]) == (14, 30)
    assert (o["verdicts_agree"], o["criteria"]) == (71, 80)
    assert [c.advisory and not c.passed for c in r.characteristics] == [True]


def test_briefing_is_production_shaped_and_describes_the_fixture():
    fx = fixtures.load("qualifying")
    b = world.briefing(fx, DOC, SP)
    assert split.HELD_OUT not in json.dumps(b)
    assert {s["id"]: s["log_share"] for s in b["shapes"]} == {
        k: v["share"] for k, v in fx["shapes"].items()}
    assert all("today_on_naive" not in s for s in b["shapes"])
    assert {r["config"] for r in b["phase3_baselines_dev"]} == {"naive"}


def test_gauges_leave_out_unmeasured_values_and_report_every_class(scripted):
    results = [subject.run(c) for c in subject.CASES[3:]]
    pts = suite.gauges(results)
    names = {(n, tuple(sorted(t.items()))) for n, _, t in pts}
    assert ("prediction.interval_coverage", ()) in names
    assert not any(n == "evals.recommendation.cost_ratio" for n, _, _ in pts)
    outputs = {t["output"]: v for n, v, t in pts if n == "loop.outputs"}
    assert outputs == {"budget_exhausted": 0, "insufficient_evidence": 0,
                       "no_improvement_found": 0, "criteria_unmet": 1, "recommendation": 0}


def test_prompt_version_tracks_the_prompt(monkeypatch):
    v = subject.prompt_version()
    assert v.startswith("proposer-") and len(v) == 21
    monkeypatch.setattr(subject, "SYSTEM", subject.SYSTEM + " ")
    assert subject.prompt_version() != v


# -------------------------------------------------------------- telemetry

def test_series_payload_is_gauges_with_version_and_extra_tags(monkeypatch):
    monkeypatch.setenv("DD_VERSION", "9.9.9")
    body = telemetry.series_payload([("loop.outputs", 1, {"output": "criteria_unmet"})], at=1)
    (s,) = body["series"]
    assert s["metric"] == "pocbench.loop.outputs" and s["type"] == 3
    assert s["points"] == [{"timestamp": 1, "value": 1.0}]
    assert "version:9.9.9" in s["tags"] and "output:criteria_unmet" in s["tags"]


def test_submit_declines_without_a_key(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert telemetry.submit([("x", 1, {})]) is False


def test_iteration_span_is_a_no_op_when_tracing_is_off():
    assert not telemetry.tracing()
    with telemetry.iteration("s") as span:
        span.record({"iteration": 1, "model_usd": 0.1})
