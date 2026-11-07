"""The permitted treatment space and the proposal validator (RC1-632; ADR 0003,
ADR 0006, ADR 0015, ADR 0022).

A proposal is a tier and a set of index candidate ids, nothing else. The
validator is the only way a proposal becomes a runnable configuration; the
model's output is untrusted input to it. It rejects:

- a tier not in `tiers.compute_units`;
- an index id not in the `[[index]]` list, a duplicate id, or an index made
  redundant by another in the same bundle (same table and kind, no
  predicate, its key columns a leading prefix of the other's);
- any other key. Memory and autoscaling settings are named as out of scope
  (ADR 0001, ADR 0003) so the log says why, not just "unknown key";
- a bundle already tested (it would spend budget to learn nothing).
"""

import hashlib
import json
import re
from dataclasses import dataclass

ALLOWED_KEYS = {"cu", "index_ids"}
# Named so a rejection explains itself. Any other unknown key is rejected too.
OUT_OF_SCOPE = {
    "shared_buffers": "derived from compute size on Neon (ADR 0001)",
    "effective_cache_size": "derived from compute size on Neon (ADR 0001)",
    "work_mem": "per-operation memory; deferred until an aggregate budget exists (ADR 0003)",
    "maintenance_work_mem": "deferred with work_mem (ADR 0003)",
    "hash_mem_multiplier": "deferred with work_mem (ADR 0003)",
    "autoscaling_limit_min_cu": "autoscaling is deferred (ADR 0003)",
    "autoscaling_limit_max_cu": "autoscaling is deferred (ADR 0003)",
    "autoscaling": "autoscaling is deferred (ADR 0003)",
    "min_cu": "autoscaling is deferred (ADR 0003)",
    "max_cu": "autoscaling is deferred (ADR 0003)",
    "ddl": "indexes are chosen by candidate id, never as free DDL",
}


class InvalidProposal(ValueError):
    """A proposal outside the permitted space."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class IndexCandidate:
    id: str
    ddl: str
    table: str
    kind: str
    columns: tuple[str, ...]
    partial: bool
    measured_bytes: int
    serves: str


@dataclass(frozen=True)
class Bundle:
    cu: float
    index_ids: tuple[str, ...]  # sorted

    @property
    def key(self) -> str:
        return f"{self.cu:g}cu-" + ("+".join(self.index_ids) or "pk")

    @property
    def name(self) -> str:
        """Configuration name, stable for the same bundle."""
        return "x-" + self.key.replace(".", "_")

    @property
    def sha256(self) -> str:
        body = json.dumps({"cu": self.cu, "index_ids": list(self.index_ids)}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


_HEAD = re.compile(r"CREATE INDEX ON (\w+)(?: USING (\w+))? \(")


def _split_ddl(ddl: str) -> tuple[str, str, list[str], str | None]:
    """table, kind, key expressions, predicate. Key expressions may contain
    parentheses (lower(email)), so the key list is matched by depth."""
    m = _HEAD.match(ddl)
    if not m:
        raise ValueError(f"cannot parse {ddl!r}")
    depth, cols, cur, i = 1, [], "", m.end()
    while depth:
        ch = ddl[i]
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 1 and ch == ",":
            cols.append(cur.strip())
            cur = ""
        elif depth:
            cur += ch
        i += 1
    cols.append(cur.strip())
    rest = ddl[i:].strip()
    where = rest.removeprefix("WHERE ").strip() if rest.startswith("WHERE ") else None
    if rest and where is None:
        raise ValueError(f"cannot parse {ddl!r}")
    return m.group(1), m.group(2) or "btree", cols, where


def parse_candidates(space: dict) -> dict[str, IndexCandidate]:
    out = {}
    for ix in space["index"]:
        table, kind, cols, where = _split_ddl(ix["ddl"])
        columns = tuple(c.removesuffix(" DESC").removesuffix(" ASC") for c in cols)
        out[ix["id"]] = IndexCandidate(ix["id"], ix["ddl"], table, kind, columns,
                                       where is not None, ix["measured_bytes"], ix["serves"])
    return out


def redundant_pairs(cands: list[IndexCandidate]) -> list[tuple[str, str]]:
    """(made redundant, by): same table and kind, neither partial, the first's
    columns a leading prefix of the second's (or identical)."""
    out = []
    for a in cands:
        for b in cands:
            if a.id == b.id or a.table != b.table or a.kind != b.kind or a.partial or b.partial:
                continue
            if b.columns[:len(a.columns)] == a.columns and (
                    len(a.columns) < len(b.columns) or a.id > b.id):
                out.append((a.id, b.id))
    return out


def validate(proposal: dict, space: dict, tested: set[str] = frozenset()) -> Bundle:
    """Turn an untrusted proposal into a Bundle, or raise InvalidProposal
    with every reason."""
    if not isinstance(proposal, dict):
        raise InvalidProposal([f"a proposal is an object, not {type(proposal).__name__}"])
    reasons = []
    for k in sorted(set(proposal) - ALLOWED_KEYS):
        why = OUT_OF_SCOPE.get(k)
        reasons.append(f"{k} is out of scope: {why}" if why
                       else f"{k} is not part of the permitted space (tier and index ids only)")
    tiers = [float(t) for t in space["tiers"]["compute_units"]]
    cu = proposal.get("cu")
    if isinstance(cu, bool) or not isinstance(cu, int | float):
        reasons.append(f"cu must be one of {tiers}, got {cu!r}")
        cu = None
    elif float(cu) not in tiers:
        reasons.append(f"tier {cu:g} CU is not in the permitted tiers {tiers}")
    ids = proposal.get("index_ids", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        reasons.append("index_ids must be a list of candidate ids")
        ids = []
    known = parse_candidates(space)
    for i in sorted({i for i in ids if i not in known}):
        reasons.append(f"{i} is not an index candidate ({sorted(known)})")
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        reasons.append(f"duplicate index ids {dups}")
    chosen = [known[i] for i in sorted(set(ids)) if i in known]
    for a, b in redundant_pairs(chosen):
        reasons.append(f"{a} is redundant with {b} (same table, leading columns)")
    if not reasons:
        bundle = Bundle(float(cu), tuple(sorted(set(ids))))
        if bundle.key in tested:
            reasons.append(f"bundle {bundle.key} was already tested")
        else:
            return bundle
    raise InvalidProposal(reasons)


def ddl(bundle: Bundle, space: dict) -> tuple[str, ...]:
    known = parse_candidates(space)
    return tuple(known[i].ddl for i in bundle.index_ids)
