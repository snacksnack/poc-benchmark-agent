"""Harness unit tests: the network gate, schedule, net latency, pooling,
the entry points' refusals and the planted-leak test (R3). No database."""

import csv
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from pocbench import candidate, config, criteria, harness, phase3_report, record, slices
from pocbench.loadgen import FIELDS, Instance

DOC = criteria.load_verified()
SHA = harness.criteria_sha256(DOC)
LM = config.load_model()
RULE = DOC["method"]["latency_basis"]["run_valid_if"]


# ------------------------------------------------------------ network gate

def test_method_comes_from_the_frozen_criteria():
    m = harness.Method.from_criteria(DOC, SHA, LM.data)
    assert m.probe_rule == {"probe_p50_ms_at_most": 100.0,
                            "probe_p99_minus_p50_ms_at_most": 15.0}
    assert m.order_seed == criteria.ORDER_SEED
    assert (m.tail_beyond, m.tail_distinct_beyond) == (100, 10)
    assert SHA == criteria.sha256_file(criteria.CRITERIA_PATH)


def test_stable_network_is_accepted():
    v = harness.probe_verdict([4.0] * 290 + [9.0] * 10, 0, 300, RULE)
    assert v["valid"] and v["p50_ms"] == 4.0


def test_slow_network_is_rejected():
    v = harness.probe_verdict([120.0] * 300, 0, 300, RULE)
    assert not v["valid"]
    assert any(r.startswith("slow network") for r in v["reasons"])


def test_jittery_network_is_rejected():
    # p50 40 ms is fine; a p99 of 66 ms is 26 ms of jitter, over 15.
    v = harness.probe_verdict([40.0] * 290 + [66.0] * 10, 0, 300, RULE)
    assert not v["valid"]
    assert any(r.startswith("jittery network") for r in v["reasons"])


def test_phase1_capture_network_would_have_passed():
    # results/phase1: probe p50 65.8 ms, p99 71.7 ms on a far runner.
    v = harness.probe_verdict([65.8] * 1180 + [71.7] * 18, 0, 1200, RULE)
    assert v["valid"]


def test_an_unmeasured_network_is_not_a_stable_one():
    assert not harness.probe_verdict([4.0] * 100, 0, 300, RULE)["valid"]
    assert not harness.probe_verdict([4.0] * 300, 2, 300, RULE)["valid"]
    assert not harness.probe_verdict([], 0, 300, RULE)["valid"]


def test_preflight_takes_as_many_round_trips_as_a_run_probe():
    # 300: one spike in 60 samples is the p99; in 300 it is not.
    assert harness.preflight_rounds(LM.data) == 300
    one_spike = [69.2] * 299 + [139.3]  # the 2026-11-07 preflight's shape
    assert harness.probe_verdict(one_spike, 0, 300, RULE)["valid"]
    assert not harness.probe_verdict([69.2] * 59 + [139.3], 0, 60, RULE)["valid"]


def test_require_network_stops_the_job():
    with pytest.raises(harness.NetworkRejected, match="slow network"):
        harness.require_network(harness.probe_verdict([150.0] * 60, 0, 60, RULE))


# ---------------------------------------------------------------- schedule

def test_every_cell_runs_once_per_repetition_and_baselines_interleave():
    cells = harness.schedule(["default", "naive"], [1, 8, 32],
                             ["warm", "post-restart-with-eviction"], 2, 630, "development")
    assert len(cells) == 24
    for r in (1, 2):
        block = [c for c in cells if c.repetition == r]
        assert Counter((c.config, c.level, c.cache_state) for c in block).most_common(1)[0][1] == 1
        assert {c.config for c in block[:6]} == {"default", "naive"}  # not one config's block
    assert cells == harness.schedule(["default", "naive"], [1, 8, 32],
                                     ["warm", "post-restart-with-eviction"], 2, 630,
                                     "development")
    assert [c.config for c in cells[:12]] != [c.config for c in cells[12:]]


# -------------------------------------------------------------- run summary

def _rows(spec):
    """spec: list of (shape, instance, latency, outcome)."""
    return [{"phase": "measure", "shape": s, "instance": str(i), "start_ms": str(n * 10),
             "latency_ms": str(lat), "outcome": o, "sqlstate": ""}
            for n, (s, i, lat, o) in enumerate(spec)]


def test_net_latency_subtracts_the_probe_median_and_counts_failures():
    rows = _rows([("a", 1, 50.0, "ok"), ("a", 2, 3.0, "ok"), ("a", 3, 5000, "timeout"),
                  ("b", 4, 10.0, "error")])
    run = harness.summarize_run(rows, probe_p50=4.0, measure_s=300)
    a = run["by_shape"]["a"]
    assert a["net"]["max_ms"] == 46.0 and a["client"]["max_ms"] == 50.0
    assert a["net"]["p50_ms"] == 0.0 and run["net_clamped"] == 1
    assert (a["observations"], a["ok"], a["timeouts"]) == (3, 2, 1)
    assert run["by_shape"]["b"]["errors"] == 1
    assert run["overall"]["failure_fraction"] == 0.5
    assert run["successful_qps"] == round(2 / 300, 4)


def test_an_aborted_run_measures_only_its_window():
    rows = _rows([("a", 1, 100.0, "ok")] * 3)  # starts 0, 10, 20 ms
    assert harness.measured_seconds(rows, 300, aborted=False) == 300
    assert harness.measured_seconds(rows, 300, aborted=True) == pytest.approx(0.12)


def test_tail_support_counts_distinct_instances_beyond():
    pairs = sorted([(float(x), str(x % 5)) for x in range(1000)])
    t = harness.tail_support(pairs, 50, beyond=100, distinct=10)
    assert t["observations_beyond"] == 500 and t["distinct_beyond"] == 5
    assert not t["supported"]  # 500 beyond, but only 5 distinct instances


def test_file_cache_size_parses_neon_settings():
    assert harness.parse_size("6553 MB") == 6553 * 1024 ** 2
    assert harness.parse_size("6553MB MB") == 6553 * 1024 ** 2
    assert harness.parse_size("16384 8kB") == 16384 * 8192


def test_cache_ratios_from_counters():
    before = {"pg_stat_database": [{"blks_hit": 0, "blks_read": 0}],
              "neon_stat_file_cache": [{"file_cache_hits": 10, "file_cache_misses": 10,
                                        "file_cache_used": 5}]}
    after = {"pg_stat_database": [{"blks_hit": 90, "blks_read": 10}],
             "neon_stat_file_cache": [{"file_cache_hits": 40, "file_cache_misses": 20,
                                       "file_cache_used": 9}]}
    r = harness.cache_ratios(before, after)
    assert r["shared_buffers_hit_ratio"] == 0.9 and r["file_cache_hit_ratio"] == 0.75
    assert harness.cache_ratios({"neon_stat_file_cache": {"unavailable": "42P01"}},
                                {"neon_stat_file_cache": {"unavailable": "42P01"}}) == {}


def test_server_checks_void_a_run_whose_compute_restarted():
    ev = lambda t, r: {"server_start": [{"started_at": t}],  # noqa: E731
                       "pg_stat_database": [{"stats_reset": r}]}
    ok = {"cache_evidence": {"before": ev("t0", None), "after": ev("t0", None)}}
    assert harness.server_checks(ok) == []
    bad = {"cache_evidence": {"before": ev("t0", None), "after": ev("t1", "x")}}
    assert len(harness.server_checks(bad)) == 2


# ----------------------------------------------------------------- pooling

def _write_run(root: Path, run_id: str, latencies: list[float]) -> dict:
    d = root / run_id
    d.mkdir(parents=True)
    with (d / "session-000.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for n, lat in enumerate(latencies):
            w.writerow(["measure", "s01", n % 50, n, lat, "ok", ""])
    return {"dir": run_id, "files": harness.raw_hashes(d)}


def _pooled_row(raw, status=record.SCORED, probe=5.0):
    return {"kind": "run", "status": status, "slice": "profiling",
            "config": {"name": "naive", "cu": 2,
                                                        "shared_compute": False},
            "level": 8, "cache_state": "warm", "diagnostic_only": False, "platform": "neon",
            "run_id": raw["dir"], "aborted": False, "raw": raw, "probe": {"p50_ms": probe},
            "run": {"overall": {"ok": 1000}, "successful_qps": 3.3}, "cost_usd": 0.01,
            "cache_evidence": {"ratios": {}}, "startup": None, "eviction": None}


def test_pooling_uses_scored_runs_only_and_reports_spread(tmp_path):
    m = harness.Method.from_criteria(DOC, SHA, LM.data)
    a = _write_run(tmp_path, "a", [float(x) for x in range(5, 1005)])
    b = _write_run(tmp_path, "b", [float(x) * 1.1 for x in range(5, 1005)])
    bad = _write_run(tmp_path, "bad", [9999.0] * 1000)
    rows = [_pooled_row(a), _pooled_row(b), _pooled_row(bad, status=record.REJECTED)]
    (cell,) = harness.pool(rows, tmp_path, m)
    assert cell["repetitions"] == 2 and cell["run_ids"] == ["a", "b"]
    p99 = cell["by_shape"]["s01"]["net"]["p99"]
    assert p99["ms"] < 9000  # the rejected run never entered
    assert p99["ci95_ms"][0] <= p99["ms"] <= p99["ci95_ms"][1]
    assert p99["spread"] == pytest.approx(0.1, abs=0.01)
    assert cell["cost_per_successful_query_usd"] == pytest.approx(0.02 / 2000)


def test_pooling_keeps_timeouts_and_errors_apart(tmp_path):
    m = harness.Method.from_criteria(DOC, SHA, LM.data)
    d = tmp_path / "t"
    d.mkdir()
    outcomes = ["ok"] * 90 + ["timeout"] * 6 + ["client_timeout"] * 1 + ["error"] * 3
    with (d / "session-000.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for n, o in enumerate(outcomes):
            w.writerow(["measure", "s01", n, n, 10.0, o, "57014" if o == "timeout" else ""])
    (cell,) = harness.pool([_pooled_row({"dir": "t", "files": harness.raw_hashes(d)})],
                           tmp_path, m)
    s = cell["by_shape"]["s01"]
    assert (s["timeouts"], s["errors"], s["observations"]) == (7, 3, 100)
    assert s["failure_fraction"] == pytest.approx(0.10)


def test_pooling_refuses_raw_logs_that_do_not_match_the_record(tmp_path):
    m = harness.Method.from_criteria(DOC, SHA, LM.data)
    a = _write_run(tmp_path, "a", [10.0] * 100)
    (tmp_path / "a" / "session-000.csv").write_text(",".join(FIELDS) + "\n")
    with pytest.raises(ValueError, match="do not match"):
        harness.pool([_pooled_row(a)], tmp_path, m)


# ------------------------------------------------- entry points, leak test

def _rebuilt(planted: bool = False) -> slices.Rebuilt:
    """Four log lines: accounts 1 and 2 profiling, 3 held out."""
    inst = Instance("s01", "SELECT %s", (1,))
    slice_of = {1: "profiling", 2: "profiling", 3: "held_out", 4: "held_out"}
    prof = slices.SliceInstances("profiling", (1, 2), (1, 2), (inst, inst))
    if planted:
        prof = slices.SliceInstances("profiling", (1, 2, 3), (1, 2, 3), (inst, inst, inst))
    held = slices.SliceInstances("held_out", (3, 4), (3, None), (inst, inst))
    manifest = {"slices": {"profiling": {"accounts": [1, 2]}, "held_out": {"accounts": [3]}}}
    return slices.Rebuilt(manifest, slice_of, {"profiling": prof, "held_out": held})


class _Untouchable:
    """A target that fails the test if the harness touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"the harness touched the target ({name}) before refusing")


def _session(tmp_path) -> harness.Session:
    s, _ = harness.open_session("development", tmp_path / "raw", tmp_path / "r.jsonl")
    return s


def test_planted_leak_is_refused_before_any_run(tmp_path):
    with pytest.raises(slices.LeakError, match="held-out"):
        harness.development(_session(tmp_path), {"naive": _Untouchable()},
                            _rebuilt(planted=True))
    assert len(record.verify(tmp_path / "r.jsonl")) == 1  # header only


def test_guard_catches_a_leak_through_either_source():
    rb = _rebuilt()
    inst = Instance("s01", "SELECT 1", ())
    # The assignment says held out, the account looks fine (a no-tenant query).
    by_assignment = slices.SliceInstances("profiling", (4,), (None,), (inst,))
    # The assignment was tampered to say profiling; the account is held out.
    rb.slice_of[3] = "profiling"
    by_account = slices.SliceInstances("profiling", (3,), (3,), (inst,))
    for si in (by_assignment, by_account):
        with pytest.raises(slices.LeakError):
            slices.guard_profiling(si, rb)


def test_held_out_needs_a_verified_frozen_record():
    rb = _rebuilt()
    with pytest.raises(candidate.NotFrozenError):
        rb.held_out(None)
    forged = candidate.Frozen("candidate", "0" * 64, {})
    with pytest.raises(candidate.NotFrozenError):
        rb.held_out(forged)


def test_evaluation_refuses_without_a_frozen_record(tmp_path):
    with pytest.raises(candidate.NotFrozenError, match="refuses to run"):
        harness.require_frozen(SHA, path=tmp_path / "frozen.json",
                               pin_path=tmp_path / "frozen.sha256")


def test_evaluation_refuses_an_edited_or_foreign_freeze(tmp_path):
    p, pin = tmp_path / "frozen.json", tmp_path / "frozen.sha256"
    candidate.freeze({"kind": "refusal", "criteria_sha256": "d" * 64}, p, pin)
    with pytest.raises(candidate.NotFrozenError, match="other criteria"):
        harness.require_frozen(SHA, path=p, pin_path=pin)
    p.write_text(p.read_text().replace("refusal", "candidate"))
    with pytest.raises(candidate.NotFrozenError, match="pinned"):
        harness.require_frozen(SHA, path=p, pin_path=pin)
    with pytest.raises(candidate.NotFrozenError, match="already frozen"):
        candidate.freeze({"kind": "refusal"}, p, pin)


def test_evaluation_with_a_refusal_runs_baselines_on_both_slices(tmp_path, monkeypatch):
    p, pin = tmp_path / "frozen.json", tmp_path / "frozen.sha256"
    candidate.freeze({"kind": "refusal", "criteria_sha256": SHA, "refusal_id": "refusal-x"},
                     p, pin)
    frozen = harness.require_frozen(SHA, path=p, pin_path=pin)
    ran = []

    def fake_cell(s, cell, target, si):
        ran.append((si.name, cell.config, cell.level, cell.cache_state, cell.repetition))

    monkeypatch.setattr(harness, "run_cell", fake_cell)
    s, _ = harness.open_session("evaluation", tmp_path / "raw", tmp_path / "r.jsonl")
    base = [harness.Config("default", 2), harness.Config("naive", 2, ("CREATE INDEX x",))]
    targets = {c.name: type("T", (), {"can_restart": True})()
               for c in harness.eval_configs(frozen, base)}
    out = harness.evaluation(s, frozen, targets, _rebuilt(), set())
    # 2 slices x 2 baselines x 3 levels x 2 cache states x 5 repetitions
    assert out["cells_planned"] == len(ran) == 120 and out["cells_remaining"] == 0
    assert {r[0] for r in ran} == {"held_out", "profiling"}
    assert {r[1] for r in ran} == {"default", "naive"}
    assert s.frozen == {"kind": "refusal", "sha256": frozen.sha256, "id": "refusal-x"}


def test_evaluation_resumes_and_pauses(tmp_path, monkeypatch):
    p, pin = tmp_path / "frozen.json", tmp_path / "frozen.sha256"
    candidate.freeze({"kind": "candidate", "criteria_sha256": SHA,
                      "config": {"name": "x-1cu-I2", "cu": 1, "ddl": ["CREATE INDEX y"]}}, p, pin)
    frozen = harness.require_frozen(SHA, path=p, pin_path=pin)
    ran = []
    monkeypatch.setattr(harness, "run_cell", lambda s, c, t, si: ran.append(c))
    s, _ = harness.open_session("evaluation", tmp_path / "raw", tmp_path / "r.jsonl")
    base = [harness.Config("default", 2), harness.Config("naive", 2)]
    cfgs = harness.eval_configs(frozen, base)
    assert [c.name for c in cfgs] == ["default", "naive", "x-1cu-I2"]
    targets = {c.name: type("T", (), {"can_restart": True})() for c in cfgs}
    plan = harness.eval_schedule(sorted(targets), s.method, ["held_out", "profiling"])
    done = set(plan[:100])
    budget = iter(range(30))
    out = harness.evaluation(s, frozen, targets, _rebuilt(), done,
                             may_start=lambda c: next(budget, None) is not None)
    assert len(plan) == 180 and len(ran) == 30 and out["paused"]
    assert out["cells_remaining"] == 50
    assert ran[0] == plan[100].cell


def test_development_reads_criteria_only_through_load_verified(tmp_path, monkeypatch):
    def refuse(_root=None):
        raise criteria.CriteriaPinError("planted pin failure")

    monkeypatch.setattr(criteria, "load_verified", refuse)
    with pytest.raises(criteria.CriteriaPinError, match="planted"):
        harness.open_session("development", tmp_path / "raw", tmp_path / "r.jsonl")


def test_no_harness_module_opens_the_criteria_file_directly():
    root = Path(harness.__file__).parent
    for name in ("harness", "phase3", "phase3_report", "slices", "record", "candidate"):
        src = (root / f"{name}.py").read_text()
        assert not re.search(r"criteria\.json|CRITERIA_PATH|criteria\.verify\(", src), name


def test_baselines_come_from_the_criteria_and_the_pinned_profile():
    prof = json.loads((config.REPO_ROOT / "results/phase1/profile.json").read_text())
    default, naive = harness.baselines(DOC, prof)
    assert (default.name, default.cu, default.ddl) == ("default", 2, ())
    assert naive.cu == 2 and len(naive.ddl) == 10
    assert default.sha256 != naive.sha256


def test_the_committed_log_verifies_against_the_manifest():
    manifest = json.loads((config.REPO_ROOT / slices.MANIFEST).read_text())
    assert len(slices.read_log(config.REPO_ROOT, manifest)) == 200_000
    with pytest.raises(slices.SliceMismatchError):
        slices.read_log(config.REPO_ROOT, manifest | {"log": manifest["log"] | {
            "sha256": "0" * 64}})


def test_report_renders_from_a_verified_record(tmp_path):
    s, _ = harness.open_session("development", tmp_path / "raw", tmp_path / "record.jsonl")
    t = s.lm["timing"]
    spec = {"warmup_s": t["warmup_seconds"], "measure_s": t["measure_seconds"],
            "timeout_ms": t["per_query_timeout_ms"], "grace_ms": t["client_grace_ms"],
            "load_model_sha256": s.lm_sha256}
    for run_id, status in (("a", record.SCORED), ("b", record.SCORED), ("c", record.REJECTED)):
        raw = _write_run(tmp_path / "raw", run_id, [float(x) for x in range(5, 1005)])
        row = _pooled_row(raw, status=status) | {
            "entry_point": "development", "slice": "profiling", **s.record.pins, "spec": spec,
            "reasons": [] if status == record.SCORED else ["jittery network: probe p99 - p50"],
            "probe": {"p50_ms": 5.0, "jitter_ms": 1.0, "valid": status == record.SCORED}}
        row.pop("kind")
        s.record.append(row)
    text = phase3_report.render(tmp_path)
    assert "2 scored runs, 1 rejected" in text and "| naive | 8 | warm | s01 |" in text
    assert "jittery network" in text
