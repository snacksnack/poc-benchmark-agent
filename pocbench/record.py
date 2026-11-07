"""The append-only results record (RC1-631, ADR 0020).

One JSON line per run, hash-chained: each row carries the SHA-256 of the row
before it and of itself, so an edited, reordered or deleted row breaks the
chain and `verify` names where. The first line is a header that pins the
criteria, load model and config space the record was written under; every
row must carry the same hashes.

The record only accepts rows a scored run could have produced. Its timing
must be the load model's (warm-up per the cache state, the full measure
window, the per-query timeout), and its slice must match its entry point. A
development aid that shortens the window therefore cannot write here, by
construction rather than by convention.

The file is only ever opened in append mode.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

SCORED, REJECTED = "scored", "rejected"
ENTRY_SLICE = {"development": "profiling", "evaluation": "held_out"}
GENESIS = "0" * 64


class RecordError(RuntimeError):
    """A row the record refuses, or a record whose chain is broken."""


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _seal(row: dict, prev: str) -> dict:
    body = {k: v for k, v in row.items() if k not in ("prev_sha256", "row_sha256")}
    body["prev_sha256"] = prev
    body["row_sha256"] = hashlib.sha256(_canon(body)).hexdigest()
    return body


def verify(path: Path) -> list[dict]:
    """Read the record and check the chain. Returns header plus rows."""
    if not path.exists():
        raise RecordError(f"{path} does not exist")
    rows, prev = [], GENESIS
    for n, line in enumerate(path.read_text().splitlines(), 1):
        row = json.loads(line)
        if row.get("prev_sha256") != prev:
            raise RecordError(f"{path.name} line {n}: chain broken (prev_sha256 mismatch)")
        if _seal(row, prev)["row_sha256"] != row.get("row_sha256"):
            raise RecordError(f"{path.name} line {n}: row was edited after it was written")
        if (n == 1) != (row.get("kind") == "header"):
            raise RecordError(f"{path.name} line {n}: the header must be the first line only")
        prev = row["row_sha256"]
        rows.append(row)
    if not rows:
        raise RecordError(f"{path.name} is empty")
    return rows


class Record:
    """Append-only writer. `load_model` and `cache_states` are the pinned
    files' data; the production entry points pass the verified ones."""

    def __init__(self, path: Path, *, criteria_sha256: str, load_model: dict,
                 load_model_sha256: str, config_space_sha256: str, cache_states: dict):
        self.path = path
        self.pins = {"criteria_sha256": criteria_sha256,
                     "load_model_sha256": load_model_sha256,
                     "config_space_sha256": config_space_sha256}
        t = load_model["timing"]
        self._timing = {"measure_s": t["measure_seconds"], "timeout_ms": t["per_query_timeout_ms"],
                        "grace_ms": t["client_grace_ms"]}
        self._warmup = {cs: (t["warmup_seconds"] if v["uses_warmup"] else 0)
                        for cs, v in cache_states.items()}
        if path.exists():
            existing = verify(path)
            mismatched = [k for k, v in self.pins.items() if existing[0].get(k) != v]
            if mismatched:
                raise RecordError(f"{path.name} was written under other pins: {mismatched}; "
                                  "a new criteria freeze starts a new record")
            self._prev = existing[-1]["row_sha256"]
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._prev = GENESIS
            self._write({"kind": "header", **self.pins,
                         "created_at": datetime.now(UTC).isoformat(timespec="seconds")})

    def _write(self, row: dict) -> dict:
        sealed = _seal(row, self._prev)
        with self.path.open("a") as fh:
            fh.write(json.dumps(sealed, sort_keys=True, default=str) + "\n")
        self._prev = sealed["row_sha256"]
        return sealed

    def check(self, row: dict) -> None:
        """Raise RecordError if `row` is not something a scored run produced."""
        problems = []
        for k, v in self.pins.items():
            if row.get(k) != v:
                problems.append(f"{k} {str(row.get(k))[:12]} != {v[:12]}")
        if row.get("status") not in (SCORED, REJECTED):
            problems.append(f"status {row.get('status')!r}")
        entry = row.get("entry_point")
        if entry not in ENTRY_SLICE:
            problems.append(f"entry point {entry!r}")
        elif row.get("slice") != ENTRY_SLICE[entry]:
            problems.append(f"{entry} rows are on the {ENTRY_SLICE[entry]} slice, "
                            f"not {row.get('slice')!r}")
        cs = row.get("cache_state")
        spec = row.get("spec", {})
        if cs not in self._warmup:
            problems.append(f"cache state {cs!r}")
        elif spec.get("warmup_s") != self._warmup[cs]:
            problems.append(f"warm-up {spec.get('warmup_s')} s != {self._warmup[cs]} s for {cs}")
        for k, v in self._timing.items():
            if spec.get(k) != v:
                problems.append(f"{k} {spec.get(k)} != load model {v}")
        if spec.get("load_model_sha256") != self.pins["load_model_sha256"]:
            problems.append("run spec was built from another load model")
        if problems:
            raise RecordError("record refuses the row: " + "; ".join(problems))

    def append(self, row: dict) -> dict:
        self.check(row)
        return self._write({"kind": "run", **row})
