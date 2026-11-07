import csv
from datetime import datetime, timedelta

import pytest

from pocbench import config, profile


@pytest.mark.parametrize(("q", "beyond", "floor"),
                         [(95, 100, 2000), (99, 100, 10000), (95, 10, 200), (99, 10, 1000)])
def test_tail_floor_puts_the_required_count_beyond_the_percentile(q, beyond, floor):
    assert profile.tail_floor(q, beyond) == floor


def test_load_model_tail_rule_matches_the_p99_sample_floor():
    r = config.load_model().data["report"]
    assert profile.tail_floor(99, r["tail_beyond"]) == r["min_samples_for_p99"]


def test_validator_rejects_a_p99_floor_out_of_step_with_the_tail_rule():
    lm = config.load_model().data
    lm["report"]["tail_beyond"] = 50
    with pytest.raises(ValueError, match="tail_beyond"):
        config.validate_load_model(lm)


T0 = datetime(2026, 11, 7, 12, 0)


def _stats(**over):
    base = {"postmaster_start": T0, "pgss_reset": T0 + timedelta(minutes=1),
            "db_stats_reset": None, "now": T0 + timedelta(minutes=2)}
    return base | over


def test_stats_valid_when_nothing_moved_during_the_run():
    v = profile.stats_validity(_stats(), _stats(now=T0 + timedelta(minutes=30)))
    assert v["valid"]


@pytest.mark.parametrize("field", ["postmaster_start", "pgss_reset", "db_stats_reset"])
def test_stats_invalid_after_a_suspend_or_reset(field):
    after = _stats(**{field: T0 + timedelta(minutes=20)})
    v = profile.stats_validity(_stats(), after)
    assert not v["valid"]


def test_plan_facts_reads_scans_removed_rows_workers_and_spills():
    plan = [{"Execution Time": 12.5, "Planning Time": 0.4, "Plan": {
        "Node Type": "Limit", "Shared Hit Blocks": 10, "Shared Read Blocks": 3, "Plans": [{
            "Node Type": "Gather", "Workers Planned": 2, "Workers Launched": 1, "Plans": [{
                "Node Type": "Sort", "Sort Method": "external merge", "Sort Space Type": "Disk",
                "Sort Space Used": 9000, "Plans": [{
                    "Node Type": "Seq Scan", "Relation Name": "events",
                    "Rows Removed by Filter": 1000, "Actual Loops": 3}]}]}]}}]
    f = profile.plan_facts(plan)
    assert f["scans"] == ["Seq Scan:events"]
    assert f["rows_removed"] == 3000
    assert (f["workers_planned"], f["workers_launched"]) == (2, 1)
    assert f["spills"] and f["execution_ms"] == 12.5


def test_predicate_query_renumbers_only_the_where_params():
    t = "select id from events where account_id = $1 order by occurred_at desc limit $2 offset $3"
    assert profile.predicate_query(t, (7, 50, 100)) == ("events", "account_id = %s", (7,))
    assert profile.predicate_query("select 1 from t", ()) is None


def test_q_error_is_symmetric_and_floored_at_one_row():
    assert profile.q_error(100, 10) == profile.q_error(10, 100) == 10
    assert profile.q_error(500, 0) == 500


def test_capture_totals_charge_a_timeout_its_full_timeout(tmp_path):
    with (tmp_path / "session-000.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["phase", "shape", "start_ms", "latency_ms", "outcome", "sqlstate"])
        w.writerow(["warmup", "s01", 0, 999, "ok", ""])
        w.writerow(["measure", "s01", 1, 10, "ok", ""])
        w.writerow(["measure", "s01", 2, 5000.3, "timeout", "57014"])
        w.writerow(["measure", "s02", 3, 6001, "client_timeout", ""])
    totals = profile.capture_totals(tmp_path, timeout_ms=5000)
    assert totals == {"s01": {"observations": 2, "total_ms": 5010.0},
                      "s02": {"observations": 1, "total_ms": 5000.0}}


def test_candidate_ddl_names_every_index():
    ddl = profile.candidate_ddl(config.config_space().data)
    assert [i for i, _ in ddl] == [f"I{n}" for n in range(1, 9)]
    assert all(f"cand_{i.lower()} ON" in d for i, d in ddl)


def test_pgss_entries_for_one_shape_are_summed_across_parameter_types():
    templates = {"s01": "select id from events where contact_id = $1 limit $2"}
    rows = [
        {"query": "select id from events where contact_id = $1 limit $2", "calls": 3,
         "total_exec_time": 30.0, "rows": 3, "shared_blks_hit": 5, "shared_blks_read": 1},
        {"query": "select id from events where contact_id = $1 limit $2", "calls": 1,
         "total_exec_time": 10.0, "rows": 1, "shared_blks_hit": 2, "shared_blks_read": 0},
        {"query": "select pg_stat_statements_reset()", "calls": 1, "total_exec_time": 1.0,
         "rows": 1, "shared_blks_hit": 0, "shared_blks_read": 0},
    ]
    got = profile.pgss_by_shape(rows, templates)
    assert got == {"s01": {"calls": 4, "total_exec_time": 40.0, "rows": 4, "shared_blks_hit": 7,
                           "shared_blks_read": 1, "entries": 2, "mean_exec_time": 10.0}}
