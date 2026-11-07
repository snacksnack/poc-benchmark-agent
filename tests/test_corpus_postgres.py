"""Corpus and profiler against a real Postgres 18, at test scale.

Skipped unless POCBENCH_TEST_DSN is set. Loads into a throwaway database so
the loader's DROP TABLE never touches anything else.
"""

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pocbench import config, corpus, phase1_report, profile, split

DSN = os.environ.get("POCBENCH_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="POCBENCH_TEST_DSN not set")
TEST_DB = "pocbench_corpus_test"


@pytest.fixture(scope="module")
def db():
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    dsn = make_conninfo(**(conninfo_to_dict(DSN) | {"dbname": TEST_DB}))
    corpus.load(dsn, corpus.TEST_SPEC, fresh=True, chunk_rows=50_000, log=lambda _: None)
    yield dsn
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


CHECKSUM = """SELECT count(*), sum(hashtext(e::text)::bigint) FROM events e"""


def test_load_is_deterministic_and_resumes_without_duplicates(db):
    with psycopg.connect(db) as conn:
        first = conn.execute(CHECKSUM).fetchone()
    # A resumed load with every chunk recorded is a no-op.
    corpus.load(db, corpus.TEST_SPEC, chunk_rows=50_000, log=lambda _: None)
    with psycopg.connect(db) as conn:
        assert conn.execute(CHECKSUM).fetchone() == first
    assert first[0] == pytest.approx(corpus.TEST_SPEC.events, rel=0.02)


def test_dataset_traps_hold(db):
    with psycopg.connect(db) as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        # D4: region implies country.
        assert q("SELECT count(DISTINCT (region, country)) FROM contacts") == 20
        # D5: no churned account has an event after its churn date.
        assert q("SELECT count(*) FROM events e JOIN accounts a ON a.id = e.account_id "
                 "WHERE e.occurred_at >= a.churned_at") == 0
        assert q("SELECT count(*) FROM accounts WHERE churned_at IS NOT NULL") > 0
        # Denormalized keys agree with their parents.
        assert q("SELECT count(*) FROM events e JOIN contacts c ON c.id = e.contact_id "
                 "WHERE c.account_id <> e.account_id") == 0
        assert q("SELECT count(*) FROM events e JOIN campaigns c ON c.id = e.campaign_id "
                 "WHERE c.account_id <> e.account_id") == 0
        # D1: account 1 is the largest tenant.
        assert q("SELECT account_id FROM contacts GROUP BY 1 ORDER BY count(*) DESC LIMIT 1") == 1
        # D6: the heap is in time order.
        assert q("SELECT count(*) FROM (SELECT occurred_at < lag(occurred_at) OVER "
                 "(ORDER BY ctid) AS back FROM events) s WHERE back") == 0


def test_prepare_split_and_every_shape_executes(db, tmp_path: Path):
    space = config.config_space().data | {}
    space["corpus"] = space["corpus"] | {"log_entries": 5_000}
    space["split"] = space["split"] | {"candidates": 4}
    with psycopg.connect(db, autocommit=True) as conn:
        p = profile.prepare(conn, space, tmp_path)
        for sid in p.templates:
            seq = next(e.seq for e in p.entries if p.shape_of[e.seq] == sid)
            conn.execute(p.norm[seq].executable, p.norm[seq].params).fetchall()
    assert len(p.templates) == 9
    resolved = sum(a is not None for a in p.account_of.values())
    assert resolved / len(p.entries) > 0.85  # everything but tenantless shapes and missing emails
    man = profile.manifest(p, space, config.load_model().data)
    assert man["slices"]["profiling"]["entries"] + man["slices"]["held_out"]["entries"] == 5_000
    assert not set(man["slices"]["profiling"]["accounts"]) & set(
        man["slices"]["held_out"]["accounts"])
    assert man["assignment_sha256"] == split.assignment_sha256(p.split.slice_of)
    # The log file reproduces byte for byte.
    first = (tmp_path / "query-log.jsonl.gz").read_bytes()
    with psycopg.connect(db, autocommit=True) as conn:
        again = profile.prepare(conn, space, tmp_path / "again")
    assert (tmp_path / "again" / "query-log.jsonl.gz").read_bytes() == first
    assert again.log_sha256 == p.log_sha256
    # Ground truth check used by the report: each designed shape maps to one shape.
    mapping = phase1_report.truth_mapping(tmp_path, man)
    assert len(mapping) == 9 and all(len(c) == 1 for c in mapping.values())


def test_diagnose_reads_plans_and_selectivity(db, tmp_path: Path):
    space = config.config_space().data | {}
    space["corpus"] = space["corpus"] | {"log_entries": 2_000}
    space["split"] = space["split"] | {"candidates": 2}
    cap = space["capture"] | {"explain_samples": 2, "count_samples": 2}
    with psycopg.connect(db, autocommit=True) as conn:
        p = profile.prepare(conn, space, tmp_path)
    diag = profile.diagnose(db, p, cap, seed=1)
    assert set(diag) == set(p.templates)
    for d in diag.values():
        assert d["plans"] and all("execution_ms" in x for x in d["plans"])
        assert all(x["q_error"] >= 1 for x in d["selectivity"])
