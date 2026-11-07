"""Harness end to end on a real Postgres (skipped unless POCBENCH_TEST_DSN is
set; CI starts a service container). A short-window copy of the load model
stands in for the pinned one, and the record is opened with that copy."""

import copy
import os

import psycopg
import pytest

from pocbench import config, criteria, harness, record, slices
from pocbench.loadgen import Instance

DSN = os.environ.get("POCBENCH_TEST_DSN")
pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")]


def _session(tmp_path, rule: dict | None = None) -> harness.Session:
    doc = criteria.load_verified()
    sha = harness.criteria_sha256(doc)
    lm = copy.deepcopy(config.load_model().data)
    lm["timing"].update(warmup_seconds=1, measure_seconds=3)
    lm["probe"]["interval_ms"] = 100
    space = config.config_space()
    method = harness.Method.from_criteria(doc, sha, lm)
    if rule is not None:
        method = harness.Method(**{**method.__dict__, "probe_rule": rule})
    rec = record.Record(tmp_path / "record.jsonl", criteria_sha256=sha, load_model=lm,
                        load_model_sha256="test-load-model", config_space_sha256=space.sha256,
                        cache_states=space.data["cache_states"])
    return harness.Session("development", method, lm, "test-load-model", space.data,
                           space.sha256, rec, tmp_path / "raw", 0.106)


def _slice():
    inst = (Instance("s01", "SELECT %s::int", (1,)), Instance("s02", "SELECT pg_sleep(0.002)", ()))
    return slices.SliceInstances("profiling", (1, 2), (1, 2), inst)


def test_a_scored_run_lands_in_the_record_and_pools(tmp_path):
    s = _session(tmp_path)
    target = harness.LocalTarget(harness.Config("default", 2), DSN)
    cell = harness.Cell(1, "default", 2, "warm")
    row = harness.run_cell(s, cell, target, _slice())
    assert row["status"] == record.SCORED and row["platform"] == "local-postgres"
    assert row["probe"]["valid"] and row["probe"]["samples"] >= 27
    assert row["run"]["by_shape"]["s01"]["ok"] > 0
    assert "server_start" in row["cache_evidence"]["before"]
    rows = record.verify(tmp_path / "record.jsonl")
    assert [r["status"] for r in rows[1:]] == [record.SCORED]
    (cell_out,) = harness.pool(rows, tmp_path / "raw", s.method)
    assert cell_out["by_shape"]["s01"]["net"]["p50"]["ms"] >= 0


def test_a_network_that_never_passes_stops_the_job(tmp_path):
    impossible = {"probe_p50_ms_at_most": 0.0, "probe_p99_minus_p50_ms_at_most": 0.0}
    s = _session(tmp_path, rule=impossible)
    target = harness.LocalTarget(harness.Config("default", 2), DSN)
    with pytest.raises(harness.NetworkRejected, match="fixed-location runner"):
        harness.run_cell(s, harness.Cell(1, "default", 2, "warm"), target, _slice())
    rows = record.verify(tmp_path / "record.jsonl")[1:]
    assert len(rows) == harness.MAX_ATTEMPTS
    assert all(r["status"] == record.REJECTED and "slow network" in r["reasons"][0]
               for r in rows)
    assert harness.pool(rows, tmp_path / "raw", s.method) == []


def test_post_restart_cells_are_skipped_where_the_platform_cannot_restart(tmp_path):
    s = _session(tmp_path)
    targets = {"default": harness.LocalTarget(harness.Config("default", 2), DSN)}
    out = harness.run_protocol(s, targets, _slice(), [1], 1,
                               ["warm", "post-restart-with-eviction"])
    assert out["cells_run"] == 1 and out["skipped"][0]["why"] == "platform cannot restart"


def test_preflight_accepts_a_local_network_and_rejects_on_the_rule():
    lm = copy.deepcopy(config.load_model().data)
    lm["probe"]["interval_ms"] = 50
    rule = criteria.load_verified()["method"]["latency_basis"]["run_valid_if"]
    assert harness.preflight(DSN, lm, rule, seconds=1)["valid"]
    strict = {"probe_p50_ms_at_most": 0.0, "probe_p99_minus_p50_ms_at_most": 0.0}
    with pytest.raises(harness.NetworkRejected):
        harness.require_network(harness.preflight(DSN, lm, strict, seconds=1))


def test_the_filler_grows_past_its_target(tmp_path):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {harness.FILLER_TABLE}")
    out = harness.ensure_filler(DSN, 1, chunk_rows=1000)
    assert out["bytes"] >= 1
    again = harness.ensure_filler(DSN, 1, chunk_rows=1000)
    assert again["bytes"] == out["bytes"]  # already big enough: no growth
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP TABLE {harness.FILLER_TABLE}")
