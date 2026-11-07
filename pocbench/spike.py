"""Phase 0 spike dataset and workload.

A scaled-down cut of the Phase 1 contact/event schema, sized to fit the Free
plan's 1 GB project limit with one indexed variant branch beside it. It exists
to prove the pipeline end to end and to feed the noise gate. It is NOT the
Phase 1 corpus: it is fully cache-resident at 1 CU, so its spread is an
optimistic floor for the noise the real dataset will show (docs/decisions.md).

Data is generated server-side, so loading costs compute, not egress.
Skew: a contact's account is a deterministic hash of its id pushed through
u^3, so a few accounts own most contacts and events.
"""

import random

from pocbench.loadgen import Instance

ACCOUNTS = 200
CAMPAIGNS = 2_000
CONTACTS = 300_000
EVENTS = 2_500_000

SCHEMA = f"""
DROP TABLE IF EXISTS events, contacts, campaigns, accounts;
CREATE OR REPLACE FUNCTION skew_account(id bigint) RETURNS int
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  AS $$ SELECT 1 + floor({ACCOUNTS} *
        power((hashint8(id) & 2147483647)::float8 / 2147483648.0, 3))::int $$;
CREATE TABLE accounts (id int PRIMARY KEY, name text NOT NULL, plan text NOT NULL);
CREATE TABLE campaigns (id int PRIMARY KEY, account_id int NOT NULL, name text NOT NULL);
CREATE TABLE contacts (
  id bigint PRIMARY KEY, account_id int NOT NULL, email text NOT NULL,
  status text NOT NULL, email_status text NOT NULL, country text NOT NULL,
  region text NOT NULL, score int NOT NULL, created_at timestamptz NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY, contact_id bigint NOT NULL, account_id int NOT NULL,
  campaign_id int NOT NULL, event_type smallint NOT NULL,
  occurred_at timestamptz NOT NULL, url text NOT NULL, properties jsonb NOT NULL);
"""

LOAD = [
    f"""INSERT INTO accounts
        SELECT g, 'account-' || g, (ARRAY['free','pro','enterprise'])[1 + g % 3]
        FROM generate_series(1, {ACCOUNTS}) g""",
    f"""INSERT INTO campaigns
        SELECT g, 1 + floor({ACCOUNTS} * power(random(), 3))::int, 'campaign-' || g
        FROM generate_series(1, {CAMPAIGNS}) g""",
    # Region implies country: the correlated-predicate trap from the nastiness list.
    f"""INSERT INTO contacts
        SELECT g, skew_account(g), 'user' || g || '@example.invalid',
          CASE WHEN r1 < 0.7 THEN 'active' WHEN r1 < 0.9 THEN 'unsubscribed' ELSE 'inactive' END,
          CASE WHEN r2 < 0.005 THEN 'bounced' ELSE 'ok' END,
          CASE WHEN reg < 8 THEN 'US' WHEN reg < 12 THEN 'CA' WHEN reg < 16 THEN 'GB' ELSE 'DE' END,
          'R' || reg, (r1 * 100)::int,
          timestamptz '2024-01-01' + g * interval '1 minute'
        FROM (SELECT g, random() r1, random() r2, floor(random() * 20)::int reg
              FROM generate_series(1, {CONTACTS}) g) s""",
    # Generated in time order, so a BRIN on occurred_at is meaningful.
    f"""INSERT INTO events
        SELECT g, c, skew_account(c),
          1 + floor({CAMPAIGNS} * power(random(), 2))::int,
          1 + floor(random() * 6)::smallint,
          timestamptz '2026-01-01' + g * interval '10 seconds',
          'https://example.invalid/p/' || floor(random() * 1000)::int,
          jsonb_build_object('ua', 'agent-' || floor(random() * 50)::int, 'v', 1)
        FROM (SELECT g, 1 + floor(random() * {CONTACTS})::bigint c
              FROM generate_series(1, {EVENTS}) g) s""",
    "ANALYZE",
]

# The indexed variant: I1, I2, I5, I7 from config/config-space.toml.
VARIANT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS events_contact_time ON events (contact_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS events_account_time ON events (account_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS contacts_account_status ON contacts (account_id, status)",
    "CREATE INDEX IF NOT EXISTS contacts_lower_email ON contacts (lower(email))",
    "ANALYZE",
]

SHAPES = {
    "latest_events": (0.40, "SELECT id, event_type, occurred_at FROM events "
                            "WHERE contact_id = %s ORDER BY occurred_at DESC LIMIT 5"),
    "account_contacts": (0.25, "SELECT id, email FROM contacts WHERE account_id = %s "
                               "AND status = 'active' ORDER BY id LIMIT 50"),
    "email_lookup": (0.20, "SELECT id, status FROM contacts WHERE lower(email) = lower(%s)"),
    "campaign_funnel": (0.10, "SELECT event_type, count(*) FROM events "
                              "WHERE campaign_id = %s GROUP BY event_type"),
    "deep_page": (0.05, "SELECT id, occurred_at FROM events WHERE account_id = %s "
                        "ORDER BY occurred_at OFFSET %s LIMIT 50"),
}


def _skewed_account(rng: random.Random) -> int:
    return 1 + int(ACCOUNTS * rng.random() ** 3)


def instances(n: int = 10_000, seed: int = 628) -> list[Instance]:
    """A fixed, seeded set of query instances; shape frequencies follow SHAPES."""
    rng = random.Random(seed)
    params = {
        "latest_events": lambda: (rng.randint(1, CONTACTS),),
        "account_contacts": lambda: (_skewed_account(rng),),
        "email_lookup": lambda: (f"User{rng.randint(1, CONTACTS)}@example.invalid",),
        "campaign_funnel": lambda: (1 + int(CAMPAIGNS * rng.random() ** 2),),
        "deep_page": lambda: (_skewed_account(rng), rng.choice([0, 500, 2000])),
    }
    out = []
    for shape, (weight, sql) in SHAPES.items():
        out += [Instance(shape, sql, params[shape]()) for _ in range(round(n * weight))]
    rng.shuffle(out)
    return out
