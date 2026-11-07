"""Normalize literal SQL into query shapes, from the log text alone.

Every string and numeric literal becomes a positional parameter, the way
pg_stat_statements normalizes a statement, and whitespace and case outside
literals are folded. Two log lines with the same normalized text are the same
shape. Nothing here knows the generator's shape names.

From a shape's template the profiler also reads: the table, the predicate
columns (which drive the naive baseline, rule fixed in config-space.toml),
and which parameter is the tenant key (which drives the held-out split).
"""

import hashlib
import re
from dataclasses import dataclass

_TOKEN = re.compile(
    r"(?P<str>'(?:[^']|'')*')"
    r"|(?P<num>(?<![\w$.])\d+(?:\.\d+)?(?![\w.]))"
    r"|(?P<ws>\s+)"
)
_FROM = re.compile(r"\bfrom\s+(\w+)")
# `col = $n`, `col >= $n`, `lower(col) = lower($n)` and the like.
_PREDICATE = re.compile(
    r"(?:\blower\((\w+)\)|\b(\w+))\s*(=|>=|<=|<>|<|>)\s*(?:lower\()?\$(\d+)")
_WHERE = re.compile(r"\bwhere\s+(.*?)(?=\s+(?:group by|order by|limit|offset)\b|$)")


@dataclass(frozen=True)
class Normalized:
    template: str  # $n placeholders, folded case and whitespace
    params: tuple

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.template.encode()).hexdigest()[:12]

    @property
    def executable(self) -> str:
        """The template in psycopg's placeholder style."""
        return re.sub(r"\$\d+", "%s", self.template.replace("%", "%%"))


def normalize(sql: str) -> Normalized:
    out, params = [], []
    pos = 0
    for m in _TOKEN.finditer(sql):
        out.append(sql[pos:m.start()].lower())
        if m.group("str") is not None:
            params.append(m.group("str")[1:-1].replace("''", "'"))
            out.append(f"${len(params)}")
        elif m.group("num") is not None:
            text = m.group("num")
            params.append(float(text) if "." in text else int(text))
            out.append(f"${len(params)}")
        else:
            out.append(" ")
        pos = m.end()
    out.append(sql[pos:].lower())
    return Normalized("".join(out).strip(), tuple(params))


@dataclass(frozen=True)
class Predicate:
    column: str
    op: str
    param: int  # 1-based
    expression: str | None  # "lower" when the column is wrapped


def table_of(template: str) -> str:
    m = _FROM.search(template)
    if not m:
        raise ValueError(f"no FROM table in {template!r}")
    return m.group(1)


def predicates(template: str) -> list[Predicate]:
    where = _WHERE.search(template)
    if not where:
        return []
    out = []
    for m in _PREDICATE.finditer(where.group(1)):
        wrapped, bare, op, n = m.groups()
        out.append(Predicate(wrapped or bare, op, int(n), "lower" if wrapped else None))
    return out


def where_clause(template: str) -> str | None:
    m = _WHERE.search(template)
    return m.group(1) if m else None


def label(template: str) -> str:
    """A short human label: table, predicate columns, ordering."""
    cols = ", ".join(sorted({(f"lower({p.column})" if p.expression else p.column)
                             for p in predicates(template)}))
    order = re.search(r"\border by\s+(.*?)(?=\s+(?:limit|offset)\b|$)", template)
    group = re.search(r"\bgroup by\s+(.*?)(?=\s+(?:order by|limit|offset)\b|$)", template)
    parts = [f"{table_of(template)} by {cols or 'nothing'}"]
    if group:
        parts.append(f"group by {group.group(1)}")
    if order:
        parts.append(f"order by {order.group(1)}")
    return "; ".join(parts)


def naive_indexes(templates: list[str]) -> list[str]:
    """The naive baseline: one single-column btree on every column that
    appears in a WHERE predicate (config-space.toml [baselines]). An
    expression like lower(email) contributes the bare column, which is the
    point: naive indexing does not read the expression."""
    seen: dict[tuple[str, str], None] = {}
    for t in templates:
        for p in predicates(t):
            seen[(table_of(t), p.column)] = None
    return [f"CREATE INDEX IF NOT EXISTS naive_{tbl}_{col} ON {tbl} ({col})"
            for tbl, col in sorted(seen) if col != "id"]


# Tenant-key resolution for the account-level split. Schema knowledge an FDE
# writes down once per engagement: which predicate columns identify a tenant,
# and how to map each to the account. Order is precedence.
TENANT_KEYS = ("account_id", "contact_id", "campaign_id", "email")


def tenant_key(template: str) -> Predicate | None:
    preds = {p.column: p for p in predicates(template) if p.op == "="}
    for col in TENANT_KEYS:
        if col in preds:
            return preds[col]
    return None
