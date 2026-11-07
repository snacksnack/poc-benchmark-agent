"""The frozen candidate or refusal record (ADR 0010, ADR 0011, ADR 0020).

Phase 4 ends by freezing exactly one of two outputs: a candidate
configuration, or a refusal. Either is written once, pinned by SHA-256, and
bound to the criteria it was chosen under. The evaluation entry point reads
it through `load_frozen` and refuses to start without it, so nothing touches
the held-out slice before the choice is fixed.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from pocbench import config

FROZEN_PATH = config.REPO_ROOT / "candidates" / "frozen.json"
PIN_PATH = config.REPO_ROOT / "candidates" / "frozen.sha256"
KINDS = ("candidate", "refusal")
_SEAL = object()  # only load_frozen makes a Frozen the slice loader accepts


class NotFrozenError(RuntimeError):
    """No frozen candidate hash or refusal record, or one that does not verify."""


@dataclass(frozen=True)
class Frozen:
    kind: str
    sha256: str
    doc: dict
    _seal: object = field(default=None, repr=False, compare=False)

    @property
    def verified(self) -> bool:
        return self._seal is _SEAL


def _canonical(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


def freeze(doc: dict, path: Path = FROZEN_PATH, pin_path: Path = PIN_PATH) -> str:
    """Write the Phase 4 output once. Overwriting is refused."""
    if doc.get("kind") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if path.exists() or pin_path.exists():
        raise NotFrozenError(f"{path.name} is already frozen; refusing to overwrite")
    raw = _canonical(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    pin_path.write_text(f"{sha}  {path.name}\n")
    return sha


def load_frozen(criteria_sha256: str, path: Path = FROZEN_PATH,
                pin_path: Path = PIN_PATH) -> Frozen:
    if not path.exists() or not pin_path.exists():
        raise NotFrozenError("evaluation refuses to run: no frozen candidate hash or refusal "
                             f"record ({path.name} and {pin_path.name} are required)")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    pinned = pin_path.read_text().split()[0]
    if sha != pinned:
        raise NotFrozenError(f"{path.name} sha256 {sha[:12]} != pinned {pinned[:12]}")
    doc = json.loads(raw)
    if doc.get("kind") not in KINDS:
        raise NotFrozenError(f"{path.name} kind {doc.get('kind')!r} is not one of {KINDS}")
    if doc.get("criteria_sha256") != criteria_sha256:
        raise NotFrozenError(f"{path.name} was frozen under other criteria")
    if doc["kind"] == "candidate":
        cfg = doc.get("config", {})
        if not {"name", "cu", "ddl"} <= set(cfg):
            raise NotFrozenError("a frozen candidate needs config.name, config.cu, config.ddl")
    return Frozen(doc["kind"], sha, doc, _SEAL)
