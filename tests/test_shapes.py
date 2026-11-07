from collections import Counter

import pytest

from pocbench import shapes, workload


def test_literals_become_positional_params_and_case_and_space_fold():
    n = shapes.normalize("SELECT id FROM contacts\n  WHERE lower(email) = lower('A''b@X.io') "
                         "AND score >= 42 LIMIT 5")
    assert n.template == ("select id from contacts where lower(email) = lower($1) "
                          "and score >= $2 limit $3")
    assert n.params == ("A'b@X.io", 42, 5)


def test_same_shape_with_different_literals_shares_a_fingerprint():
    a = shapes.normalize("SELECT 1 FROM events WHERE contact_id = 7 LIMIT 5")
    b = shapes.normalize("select 1   from events where contact_id = 123456 limit 5")
    assert a.fingerprint == b.fingerprint
    assert a.params != b.params


def test_digits_inside_identifiers_and_existing_placeholders_are_not_literals():
    n = shapes.normalize("SELECT md5(x) FROM t2 WHERE c = $1")
    assert n.template == "select md5(x) from t2 where c = $1"
    assert n.params == ()


def test_executable_template_escapes_percent_for_psycopg():
    n = shapes.normalize("SELECT id FROM t WHERE a % 7 = 3")
    assert n.executable == "select id from t where a %% %s = %s"


def test_predicates_label_and_tenant_key():
    t = shapes.normalize("SELECT id FROM events WHERE account_id = 5 "
                         "ORDER BY occurred_at DESC LIMIT 50 OFFSET 100").template
    assert [p.column for p in shapes.predicates(t)] == ["account_id"]
    assert shapes.label(t) == "events by account_id; order by occurred_at desc"
    assert shapes.tenant_key(t).column == "account_id"
    # No tenant column: no tenant key.
    t2 = shapes.normalize("SELECT 1 FROM events WHERE occurred_at >= '2025-01-01'").template
    assert shapes.tenant_key(t2) is None


def test_lower_wrapped_column_is_a_tenant_key_through_the_expression():
    t = shapes.normalize("SELECT id FROM contacts WHERE lower(email) = lower('U1@x')").template
    p = shapes.tenant_key(t)
    assert (p.column, p.expression, p.param) == ("email", "lower", 1)


def test_naive_rule_indexes_the_bare_column_even_under_an_expression():
    ts = [shapes.normalize(s).template for s in (
        "SELECT 1 FROM contacts WHERE lower(email) = lower('a')",
        "SELECT 1 FROM contacts WHERE id = 3",
        "SELECT 1 FROM events WHERE occurred_at >= 'x' AND occurred_at < 'y'",
    )]
    assert shapes.naive_indexes(ts) == [
        "CREATE INDEX IF NOT EXISTS naive_contacts_email ON contacts (email)",
        "CREATE INDEX IF NOT EXISTS naive_events_occurred_at ON events (occurred_at)",
    ]


FACTS = workload.Facts(
    contacts=10_000, accounts=tuple(range(1, 21)),
    account_weights=tuple(1000 // a + 5 for a in range(1, 21)),
    listed={a: tuple(range(a, 10_000, 97))[:200] for a in range(1, 21)})


def test_log_counts_follow_the_mix_exactly_and_are_seeded():
    log = workload.generate(FACTS, 10_000, seed=629)
    counts = Counter(e.shape for e in log)
    assert counts == {s: round(10_000 * w) for s, w in workload.MIX.items()}
    assert [e.sql for e in log] == [e.sql for e in workload.generate(FACTS, 10_000, seed=629)]
    assert [e.sql for e in log] != [e.sql for e in workload.generate(FACTS, 10_000, seed=630)]


def test_every_designed_shape_normalizes_to_one_distinct_profiler_shape():
    """The profiler recovers the design from the SQL alone: no designed shape
    splits into two fingerprints, and no two designed shapes merge."""
    by_design: dict[str, set] = {}
    for e in workload.generate(FACTS, 5_000, seed=1):
        by_design.setdefault(e.shape, set()).add(shapes.normalize(e.sql).fingerprint)
    assert all(len(fps) == 1 for fps in by_design.values()), by_design
    assert len({next(iter(fps)) for fps in by_design.values()}) == len(workload.MIX)


def test_segment_preview_mismatches_region_and_country_about_a_fifth_of_the_time():
    log = [e for e in workload.generate(FACTS, 40_000, seed=3) if e.shape == "segment_preview"]
    mismatched = 0
    for e in log:
        country, region = shapes.normalize(e.sql).params[:2]
        mismatched += workload.COUNTRY_OF_REGION[region] != country
    assert mismatched / len(log) == pytest.approx(workload.MISMATCHED_SEGMENT_FRACTION, abs=0.03)
