"""Phase 1 query log: the prospect's workload, as literal SQL (docs/nastiness.md).

The generator plays the application. It emits one literal statement per log
line, the way `log_statement` or an APM trace would record it, and keeps the
designed shape name in a separate ground-truth column. The profiler is given
the SQL only and must recover the shapes itself; the ground truth exists so a
test can check that it did.

The few facts the application would know (account sizes, which contacts an
account page lists first) are read from the loaded dataset once, so the log
is a pure function of the dataset, the seed and N.
"""

import bisect
import itertools
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from pocbench import corpus

# Designed mix. Order is the order of docs/nastiness.md.
MIX = {
    "latest_events": 0.40,
    "email_lookup": 0.15,
    "account_contacts": 0.10,
    "account_activity": 0.10,
    "bounced_contacts": 0.06,
    "campaign_funnel": 0.06,
    "segment_preview": 0.05,
    "daily_report": 0.05,
    "campaign_last_touch": 0.03,
}
# How many of an account's active contacts, lowest id first, the account
# page can list. N+1 lookups (Q1) are drawn from these.
LISTED_CONTACTS = 200
COUNTRY_OF_REGION = {f"R{r}": ("US" if r < 8 else "CA" if r < 12 else "GB" if r < 16 else "DE")
                     for r in range(20)}
COUNTRIES = ("US", "CA", "GB", "DE")
MISMATCHED_SEGMENT_FRACTION = 0.20
MISSING_EMAIL_FRACTION = 0.05


@dataclass(frozen=True)
class Facts:
    contacts: int
    accounts: tuple[int, ...]
    account_weights: tuple[int, ...]  # contact count per account
    listed: dict[int, tuple[int, ...]]  # account -> first active contact ids


@dataclass(frozen=True)
class LogEntry:
    seq: int
    sql: str
    shape: str  # ground truth; never shown to the profiler


def read_facts(conn: psycopg.Connection) -> Facts:
    contacts = conn.execute("SELECT count(*) FROM contacts").fetchone()[0]
    sizes = conn.execute(
        "SELECT a.id, count(c.id) FROM accounts a LEFT JOIN contacts c ON c.account_id = a.id "
        "GROUP BY a.id ORDER BY a.id").fetchall()
    listed: dict[int, list[int]] = {}
    for account_id, contact_id in conn.execute(
            "SELECT account_id, id FROM (SELECT account_id, id, row_number() OVER "
            "(PARTITION BY account_id ORDER BY id) AS rn FROM contacts WHERE status = 'active') s "
            "WHERE rn <= %s ORDER BY account_id, id", (LISTED_CONTACTS,)):
        listed.setdefault(account_id, []).append(contact_id)
    return Facts(contacts=contacts,
                 accounts=tuple(a for a, n in sizes if n),
                 account_weights=tuple(n for _, n in sizes if n),
                 listed={a: tuple(ids) for a, ids in listed.items()})


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class _Gen:
    def __init__(self, facts: Facts, rng: random.Random):
        self.f, self.rng = facts, rng
        self.cum = list(itertools.accumulate(facts.account_weights))
        self.start = datetime.fromisoformat(corpus.TIME_START)

    def account(self) -> int:
        x = self.rng.random() * self.cum[-1]
        return self.f.accounts[bisect.bisect_right(self.cum, x)]

    def campaign(self, account: int) -> int:
        k = int(corpus.CAMPAIGNS_PER_ACCOUNT * self.rng.random() ** 2)
        return (account - 1) * corpus.CAMPAIGNS_PER_ACCOUNT + 1 + k

    def listed_contact(self) -> int:
        ids = self.f.listed.get(self.account())
        if not ids:
            return self.rng.randint(1, self.f.contacts)
        # The first page (50) is viewed far more often than later ones.
        top = min(len(ids), 50) if self.rng.random() < 0.7 else len(ids)
        return ids[self.rng.randrange(top)]

    def latest_events(self) -> str:
        return ("SELECT id, event_type, occurred_at FROM events "
                f"WHERE contact_id = {self.listed_contact()} ORDER BY occurred_at DESC LIMIT 5")

    def email_lookup(self) -> str:
        if self.rng.random() < MISSING_EMAIL_FRACTION:
            c = self.f.contacts + self.rng.randint(1, self.f.contacts)
        else:
            c = self.rng.randint(1, self.f.contacts)
        return ("SELECT id, account_id, status FROM contacts "
                f"WHERE lower(email) = lower({_q(f'User{c}@Example.Invalid')})")

    def account_contacts(self) -> str:
        offset = self.rng.choices((0, 50, 100), weights=(80, 15, 5))[0]
        return ("SELECT id, email, score FROM contacts "
                f"WHERE account_id = {self.account()} AND status = 'active' "
                f"ORDER BY id LIMIT 50 OFFSET {offset}")

    def account_activity(self) -> str:
        r = self.rng.random()
        offset = (0 if r < 0.6 else 50 * self.rng.randint(1, 10) if r < 0.9
                  else 50 * self.rng.randint(20, 100))
        return ("SELECT id, contact_id, event_type, occurred_at FROM events "
                f"WHERE account_id = {self.account()} "
                f"ORDER BY occurred_at DESC LIMIT 50 OFFSET {offset}")

    def bounced_contacts(self) -> str:
        return ("SELECT id, email FROM contacts "
                f"WHERE account_id = {self.account()} AND email_status = 'bounced' "
                "ORDER BY id LIMIT 100")

    def campaign_funnel(self) -> str:
        return ("SELECT event_type, count(*) FROM events "
                f"WHERE campaign_id = {self.campaign(self.account())} GROUP BY event_type")

    def segment_preview(self) -> str:
        region = f"R{self.rng.randrange(20)}"
        country = COUNTRY_OF_REGION[region]
        if self.rng.random() < MISMATCHED_SEGMENT_FRACTION:
            country = self.rng.choice([c for c in COUNTRIES if c != country])
        return ("SELECT id, email FROM contacts "
                f"WHERE country = {_q(country)} AND region = {_q(region)} "
                "AND status = 'active' ORDER BY id LIMIT 100")

    def daily_report(self) -> str:
        day = self.start + timedelta(days=self.rng.randrange(corpus.TIME_SPAN_DAYS))
        lo, hi = (d.strftime("%Y-%m-%d %H:%M:%S+00") for d in (day, day + timedelta(days=1)))
        return ("SELECT campaign_id, event_type, count(*) AS n FROM events "
                f"WHERE occurred_at >= {_q(lo)} AND occurred_at < {_q(hi)} "
                "GROUP BY campaign_id, event_type ORDER BY n DESC LIMIT 50")

    def campaign_last_touch(self) -> str:
        return ("SELECT DISTINCT ON (contact_id) contact_id, occurred_at, event_type FROM events "
                f"WHERE campaign_id = {self.campaign(self.account())} "
                "ORDER BY contact_id, occurred_at DESC LIMIT 200")


def generate(facts: Facts, n: int, seed: int) -> list[LogEntry]:
    """N log entries. Shape counts are exact (round(n * weight)), order shuffled."""
    rng = random.Random(f"workload:{seed}")
    gen = _Gen(facts, rng)
    shapes = [s for s, w in MIX.items() for _ in range(round(n * w))]
    rng.shuffle(shapes)
    return [LogEntry(i, getattr(gen, s)(), s) for i, s in enumerate(shapes)]
