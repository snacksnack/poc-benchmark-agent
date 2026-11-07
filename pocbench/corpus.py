"""Phase 1 dataset: the contact/event corpus (docs/nastiness.md, D1-D6).

Every value is a seeded hash of a row id (`hashint8extended`, the function
Postgres uses for hash partitioning, so it is stable across versions), never
`random()`. The dataset is therefore a pure function of this file and the row
counts: two loads produce identical tables, and a variant branch can be
rebuilt from scratch if it is ever lost.

Data is generated server-side in chunks of ids. Each chunk commits together
with a row in `pb_load`, so a load that dies (laptop sleep, runner timeout)
resumes at the first missing chunk. Primary keys are added after the load.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

# Event timeline: one year, generated in time order (D6).
TIME_START = "2025-01-01 00:00:00+00"
TIME_SPAN_DAYS = 365
CAMPAIGNS_PER_ACCOUNT = 10
CHUNK_ROWS = 1_000_000
# Accounts at or below this id never churn, so the biggest tenants stay live.
CHURN_MIN_ACCOUNT = 3
CHURN_FRACTION = 0.25


@dataclass(frozen=True)
class CorpusSpec:
    accounts: int
    contacts: int
    events: int

    @property
    def campaigns(self) -> int:
        return self.accounts * CAMPAIGNS_PER_ACCOUNT

    @classmethod
    def from_config(cls, space: dict) -> "CorpusSpec":
        rows = space["dataset"]["rows"]
        if rows["campaigns"] != rows["accounts"] * CAMPAIGNS_PER_ACCOUNT:
            raise ValueError(f"campaigns must be {CAMPAIGNS_PER_ACCOUNT} per account")
        return cls(rows["accounts"], rows["contacts"], rows["events"])


# A tiny corpus for tests and local development: same generator, same traps.
TEST_SPEC = CorpusSpec(accounts=60, contacts=30_000, events=300_000)


def functions_sql(spec: CorpusSpec) -> str:
    # pb_u references its argument once, so the planner inlines it and the
    # generator runs at C speed instead of paying a function call per value.
    return f"""
CREATE OR REPLACE FUNCTION pb_u(x bigint, salt bigint) RETURNS float8
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  AS $$ SELECT (hashint8extended(x, salt) & 9007199254740991)::float8
               / 9007199254740992.0 $$;
CREATE OR REPLACE FUNCTION pb_account_of(contact_id bigint) RETURNS int
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  AS $$ SELECT 1 + floor({spec.accounts} * power(pb_u(contact_id, 2), 3))::int $$;
"""


SCHEMA = """
DROP TABLE IF EXISTS events, contacts, campaigns, accounts, pb_load;
CREATE TABLE pb_load (tbl text NOT NULL, chunk int NOT NULL, rows bigint NOT NULL,
                      seconds float8 NOT NULL, PRIMARY KEY (tbl, chunk));
CREATE TABLE accounts (id int NOT NULL, name text NOT NULL, plan text NOT NULL,
                       churned_at timestamptz);
CREATE TABLE campaigns (id int NOT NULL, account_id int NOT NULL, name text NOT NULL);
CREATE TABLE contacts (
  id bigint NOT NULL, account_id int NOT NULL, email text NOT NULL,
  status text NOT NULL, email_status text NOT NULL, country text NOT NULL,
  region text NOT NULL, score int NOT NULL, created_at timestamptz NOT NULL);
CREATE TABLE events (
  id bigint NOT NULL, contact_id bigint NOT NULL, account_id int NOT NULL,
  campaign_id int NOT NULL, event_type smallint NOT NULL,
  occurred_at timestamptz NOT NULL, url text NOT NULL, properties jsonb NOT NULL);
"""


def _accounts_sql(spec: CorpusSpec) -> str:
    return f"""
INSERT INTO accounts
SELECT g, 'account-' || g, (ARRAY['free','pro','enterprise'])[1 + g % 3],
  CASE WHEN g > {CHURN_MIN_ACCOUNT} AND pb_u(g, 10) < {CHURN_FRACTION}
       THEN timestamptz '{TIME_START}'
            + (0.1 + 0.8 * pb_u(g, 11)) * interval '{TIME_SPAN_DAYS} days' END
FROM generate_series(1, {spec.accounts}) g"""


def _campaigns_sql(spec: CorpusSpec) -> str:
    return f"""
INSERT INTO campaigns
SELECT g, (g - 1) / {CAMPAIGNS_PER_ACCOUNT} + 1, 'campaign-' || g
FROM generate_series(1, {spec.campaigns}) g"""


def _contacts_sql(lo: int, hi: int) -> str:
    # Region implies country, bounced implies mostly inactive (D4).
    return f"""
INSERT INTO contacts
SELECT g, pb_account_of(g), 'user' || g || '@example.invalid',
  CASE WHEN bounced THEN (CASE WHEN pb_u(g, 7) < 0.8 THEN 'inactive' ELSE 'active' END)
       WHEN pb_u(g, 3) < 0.7 THEN 'active'
       WHEN pb_u(g, 3) < 0.9 THEN 'unsubscribed' ELSE 'inactive' END,
  CASE WHEN bounced THEN 'bounced' ELSE 'ok' END,
  CASE WHEN reg < 8 THEN 'US' WHEN reg < 12 THEN 'CA' WHEN reg < 16 THEN 'GB' ELSE 'DE' END,
  'R' || reg,
  floor(100 * pb_u(g, 6))::int,
  timestamptz '{TIME_START}' + pb_u(g, 8) * interval '{TIME_SPAN_DAYS} days'
FROM (SELECT g, pb_u(g, 4) < 0.005 AS bounced, floor(20 * pb_u(g, 5))::int AS reg
      FROM generate_series({lo}, {hi}) g) s"""


def events_generated(spec: CorpusSpec, keep_fraction: float) -> int:
    """Ids to generate so that about spec.events survive the churn filter."""
    return math.ceil(spec.events / keep_fraction)


def _events_sql(spec: CorpusSpec, generated: int, lo: int, hi: int) -> str:
    step_s = TIME_SPAN_DAYS * 86400 / generated
    # Event types: sent, delivered, open, click, convert, unsubscribe (Q7 funnel).
    return f"""
INSERT INTO events
SELECT s.g, s.c, a.id,
  (a.id - 1) * {CAMPAIGNS_PER_ACCOUNT} + 1
    + floor({CAMPAIGNS_PER_ACCOUNT} * power(pb_u(s.g, 21), 2))::int,
  CASE WHEN t < 0.50 THEN 1 WHEN t < 0.75 THEN 2 WHEN t < 0.87 THEN 3
       WHEN t < 0.95 THEN 4 WHEN t < 0.98 THEN 5 ELSE 6 END,
  s.at,
  'https://example.invalid/c/' || floor(1000 * pb_u(s.g, 24))::int
    || '/p?utm_source=email&utm_content=v'
    || floor(20 * pb_u(s.g, 25))::int,
  jsonb_build_object('ua', 'agent-' || floor(50 * pb_u(s.g, 23))::int, 'v', 1)
FROM (SELECT g, 1 + floor({spec.contacts} * power(pb_u(g, 20), 2))::bigint AS c,
             pb_u(g, 22) AS t,
             timestamptz '{TIME_START}' + g * interval '{step_s:.9f} seconds' AS at
      FROM generate_series({lo}, {hi}) g) s
JOIN accounts a ON a.id = pb_account_of(s.c)
WHERE a.churned_at IS NULL OR s.at < a.churned_at
ORDER BY s.g"""  # the join would otherwise reorder the chunk and break D6


FINISH = [
    "ALTER TABLE accounts ADD PRIMARY KEY (id)",
    "ALTER TABLE campaigns ADD PRIMARY KEY (id)",
    "ALTER TABLE contacts ADD PRIMARY KEY (id)",
    "ALTER TABLE events ADD PRIMARY KEY (id)",
    # VACUUM sets the visibility map, as a long-lived production table has.
    "VACUUM (ANALYZE)",
]


def _chunks(total: int, size: int) -> list[tuple[int, int, int]]:
    return [(i, lo, min(lo + size - 1, total))
            for i, lo in enumerate(range(1, total + 1, size))]


def _done(conn: psycopg.Connection) -> set[tuple[str, int]]:
    return set(conn.execute("SELECT tbl, chunk FROM pb_load").fetchall())


def _run_chunk(conn: psycopg.Connection, tbl: str, chunk: int, sql: str) -> int:
    t0 = time.perf_counter()
    with conn.transaction():
        rows = conn.execute(sql).rowcount
        conn.execute("INSERT INTO pb_load VALUES (%s, %s, %s, %s)",
                     (tbl, chunk, rows, time.perf_counter() - t0))
    return rows


def churn_keep_fraction(conn: psycopg.Connection, spec: CorpusSpec) -> float:
    """Expected share of generated events that survive the churn filter.

    Each generated event lands on account a with the account's contact share
    and survives with probability (churned_at - start) / span. Computed from
    the loaded accounts and contacts, so it is exact for this dataset.
    """
    row = conn.execute(f"""
        SELECT sum(w * CASE WHEN a.churned_at IS NULL THEN 1.0
                       ELSE extract(epoch FROM a.churned_at - timestamptz '{TIME_START}')
                            / ({TIME_SPAN_DAYS} * 86400.0) END) / sum(w)
        FROM (SELECT pb_account_of(c) AS account_id, count(*) AS w
              FROM (SELECT 1 + floor({spec.contacts} * power(pb_u(g, 20), 2))::bigint AS c
                    FROM generate_series(1, 200000) g) s
              GROUP BY 1) e
        JOIN accounts a ON a.id = e.account_id""").fetchone()
    return float(row[0])


def load(dsn: str, spec: CorpusSpec, *, fresh: bool = False, chunk_rows: int = CHUNK_ROWS,
         log: Callable[[str], None] = print) -> dict:
    """Create (or resume) the corpus. Returns the load record."""
    t_start = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute("SET statement_timeout = 0")
        exists = conn.execute("SELECT to_regclass('pb_load') IS NOT NULL").fetchone()[0]
        if fresh or not exists:
            conn.execute(SCHEMA)
        conn.execute(functions_sql(spec))
        for ext in ("pg_stat_statements", "neon"):
            try:
                conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            except psycopg.Error:
                log(f"extension {ext} unavailable here")
        done = _done(conn)
        for tbl, sql in (("accounts", _accounts_sql(spec)), ("campaigns", _campaigns_sql(spec))):
            if (tbl, 0) not in done:
                _run_chunk(conn, tbl, 0, sql)
        for i, lo, hi in _chunks(spec.contacts, chunk_rows):
            if ("contacts", i) not in done:
                _run_chunk(conn, "contacts", i, _contacts_sql(lo, hi))
                log(f"contacts chunk {i} ({lo}-{hi})")
        keep = churn_keep_fraction(conn, spec)
        generated = events_generated(spec, keep)
        for i, lo, hi in _chunks(generated, chunk_rows):
            if ("events", i) not in done:
                rows = _run_chunk(conn, "events", i, _events_sql(spec, generated, lo, hi))
                log(f"events chunk {i} ({lo}-{hi}): {rows} rows")
        has_pk = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'events'::regclass "
            "AND contype = 'p'").fetchone()[0]
        if not has_pk:
            for sql in FINISH:
                log(sql)
                conn.execute(sql)
        record = measure(conn)
    record.update(spec={"accounts": spec.accounts, "contacts": spec.contacts,
                        "events_target": spec.events, "campaigns": spec.campaigns},
                  events_generated_ids=generated, churn_keep_fraction=round(keep, 6),
                  load_seconds=round(time.perf_counter() - t_start, 1))
    return record


def measure(conn: psycopg.Connection) -> dict:
    """Measured sizes: rows, heap and index bytes, bytes per row."""
    tables = {}
    for (name,) in conn.execute(
            "SELECT unnest(ARRAY['accounts','campaigns','contacts','events'])").fetchall():
        rows, heap, idx, total = conn.execute(
            "SELECT (SELECT count(*) FROM " + name + "), pg_table_size(%s), "
            "pg_indexes_size(%s), pg_total_relation_size(%s)", (name, name, name)).fetchone()
        tables[name] = {"rows": rows, "heap_bytes": heap, "index_bytes": idx,
                        "total_bytes": total,
                        "heap_bytes_per_row": round(heap / rows, 1) if rows else None}
    db = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    return {"tables": tables, "database_bytes": db}
