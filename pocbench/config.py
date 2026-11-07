"""Load the committed config files and fingerprint them.

The fingerprint is the SHA-256 of the file's bytes. Phase 2 embeds the load
model's fingerprint in the success criteria; a run whose load model no longer
matches fails (epic rule 1). Phase 0 only records it.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOAD_MODEL_PATH = REPO_ROOT / "config" / "load-model.toml"
CONFIG_SPACE_PATH = REPO_ROOT / "config" / "config-space.toml"


@dataclass(frozen=True)
class Pinned:
    path: Path
    sha256: str
    data: dict


def load(path: Path) -> Pinned:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    return Pinned(path=path, sha256=sha, data=tomllib.loads(raw.decode()))


def load_model(path: Path = LOAD_MODEL_PATH) -> Pinned:
    pinned = load(path)
    validate_load_model(pinned.data)
    return pinned


def config_space(path: Path = CONFIG_SPACE_PATH) -> Pinned:
    return load(path)


def validate_load_model(lm: dict) -> None:
    """Reject a load model the harness could not honor as written."""
    if lm.get("generator") != "closed-loop":
        raise ValueError("v1 supports only the closed-loop generator (ADR 0004)")
    levels = lm["sessions"]["levels"]
    if not levels or any(n < 1 for n in levels) or levels != sorted(set(levels)):
        raise ValueError(f"session levels must be ascending, unique and >= 1: {levels}")
    diagnostic = set(lm["sessions"]["diagnostic_only"])
    if not diagnostic.issubset(levels):
        raise ValueError("diagnostic_only levels must be a subset of the session levels")
    if set(levels) <= diagnostic:
        raise ValueError("at least one level must be able to support a sizing claim")
    if lm["connections"]["pool_size"] != "sessions":
        raise ValueError("pool size must equal the session count (one connection per session)")
    timing = lm["timing"]
    if timing["measure_seconds"] <= 0 or timing["per_query_timeout_ms"] <= 0:
        raise ValueError("measure window and per-query timeout must be positive")
    errors = lm["errors"]
    if not errors["count_as_results"]:
        raise ValueError("errors and timeouts must be counted as results (epic rule 2)")
    if not 0 < errors["error_ceiling_fraction"] < errors["abort_run_fraction"] <= 1:
        raise ValueError("need 0 < error ceiling < abort fraction <= 1")
