"""Load generator tests. The end-to-end test needs a real Postgres and is
skipped unless POCBENCH_TEST_DSN is set (CI starts a service container)."""

import csv
import dataclasses
import os
import time
from collections import Counter

import pytest

from pocbench import config, loadgen, spike

DSN = os.environ.get("POCBENCH_TEST_DSN")


def test_spike_instances_are_seeded_and_follow_the_declared_mix():
    a, b = spike.instances(seed=628), spike.instances(seed=628)
    assert a == b
    counts = Counter(i.shape for i in a)
    for shape, (weight, _sql) in spike.SHAPES.items():
        assert counts[shape] == round(len(a) * weight)


def test_run_spec_takes_every_number_from_the_load_model():
    lm = config.load_model()
    spec = loadgen.RunSpec.from_load_model(lm.data, lm.sha256, dsn="x", label="t", sessions=8,
                                           cache_state="warm")
    assert spec.measure_s == lm.data["timing"]["measure_seconds"]
    assert spec.timeout_ms == lm.data["timing"]["per_query_timeout_ms"]
    assert spec.load_model_sha256 == lm.sha256


def _spec(sessions: int, timeout_ms: int | None = None) -> loadgen.RunSpec:
    lm = config.load_model()
    spec = loadgen.RunSpec.from_load_model(lm.data, lm.sha256, dsn=DSN, label="it",
                                           sessions=sessions, cache_state="warm",
                                           warmup_s=1, measure_s=3)
    return spec if timeout_ms is None else dataclasses.replace(spec, timeout_ms=timeout_ms)


def _measured_rows(out_dir):
    rows = []
    for path in out_dir.glob("session-*.csv"):
        with path.open() as fh:
            rows += list(csv.DictReader(fh))
    return rows


@pytest.mark.postgres
@pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
def test_closed_loop_run_records_every_query_and_excludes_warmup(tmp_path):
    # 1 in 100 instances divides by zero: ~1% errors, under the abort fraction.
    instances = [loadgen.Instance("fast", "SELECT %s::int", (1,))] * 99 + [
        loadgen.Instance("bad", "SELECT 1/0", ())]
    summary = loadgen.run(_spec(sessions=4), instances, tmp_path)
    assert summary["aborted"] is False
    assert summary["exit_codes"] == [0] * 5  # 4 sessions + probe
    rows = _measured_rows(tmp_path)
    measured = [r for r in rows if r["phase"] == "measure"]
    assert {r["phase"] for r in rows} == {"warmup", "measure"}
    assert summary["overall"]["sample_count"] == len(measured)
    errors = [r for r in measured if r["outcome"] == "error"]
    assert errors and all(r["sqlstate"] == "22012" for r in errors)
    assert summary["overall"]["error_count"] == len(errors)
    assert summary["probe"]["ok_count"] >= 2
    assert "dsn" not in summary["spec"], "the DSN must never land in a summary"


@pytest.mark.postgres
@pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
def test_run_aborts_when_failures_pass_the_abort_fraction(tmp_path):
    instances = [loadgen.Instance("fast", "SELECT 1", ()),
                 loadgen.Instance("bad", "SELECT 1/0", ())]
    summary = loadgen.run(_spec(sessions=2), instances, tmp_path)
    assert summary["aborted"] is True
    assert summary["exit_codes"][:2] == [loadgen.EXIT_ABORTED] * 2


@pytest.mark.postgres
@pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
def test_statement_timeout_is_a_result_and_the_session_keeps_going(tmp_path):
    # 1 in 20 instances sleeps past a 100 ms statement_timeout.
    instances = [loadgen.Instance("fast", "SELECT 1", ())] * 19 + [
        loadgen.Instance("slow", "SELECT pg_sleep(0.3)", ())]
    summary = loadgen.run(_spec(sessions=2, timeout_ms=100), instances, tmp_path)
    assert summary["aborted"] is False
    slow = summary["by_shape"]["slow"]
    assert slow["timeout_count"] > 0 and slow["ok_count"] == 0
    timeouts = [r for r in _measured_rows(tmp_path) if r["outcome"] == "timeout"]
    assert all(r["sqlstate"] == "57014" for r in timeouts)
    # The session survived its timeouts and kept issuing queries after them.
    assert summary["by_shape"]["fast"]["ok_count"] > 10 * slow["timeout_count"]


@pytest.mark.postgres
@pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
def test_cache_evidence_is_recorded_before_and_after(tmp_path):
    summary = loadgen.run(_spec(sessions=1), [loadgen.Instance("q", "SELECT 1", ())], tmp_path)
    ev = summary["cache_evidence"]
    assert ev["procedure"] == "warm"
    assert ev["before"]["pg_stat_database"][0]["blks_hit"] is not None
    assert ev["after"]["pg_stat_database"][0]["blks_hit"] is not None
    # Stock Postgres has no Neon file cache: recorded as unavailable, not dropped.
    assert ev["after"]["neon_stat_file_cache"] == {"unavailable": "42P01"}


@pytest.mark.postgres
@pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
def test_clock_starts_after_every_process_connects(tmp_path):
    # Post-restart runs have no warmup: a client that connects late used to
    # lose the start of the measured window (RC1-632).
    spec = dataclasses.replace(_spec(sessions=8), warmup_s=0)
    summary = loadgen.run(spec, [loadgen.Instance("fast", "SELECT 1", ())], tmp_path)
    assert summary["connect_wait"]["connected"] == 9
    for path in [*tmp_path.glob("session-*.csv"), tmp_path / "probe.csv"]:
        with path.open() as fh:
            first = next(csv.DictReader(fh))
        assert float(first["start_ms"]) < 500, path.name


def test_unreachable_target_fails_fast_instead_of_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(loadgen, "snapshot", lambda dsn, queries: {})
    spec = dataclasses.replace(_spec(sessions=2), dsn="postgresql://x@127.0.0.1:1/x")
    t0 = time.time()
    summary = loadgen.run(spec, [loadgen.Instance("q", "SELECT 1", ())], tmp_path)
    assert summary["failed_to_connect"] is True
    assert summary["connect_wait"]["connected"] == 0
    assert time.time() - t0 < 30
