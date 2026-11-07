"""Held-out split (config-space.toml [split], ADR 0016).

The unit is the account: every query aimed at one tenant lands in the same
slice, so the held-out slice tests tenants the loop never saw, not just fresh
parameter draws for tenants it did. Accounts are stratified by size decile so
both slices hold hot and cold tenants. Queries with no tenant split by
instance.

An account-level split cannot hit an exact per-shape fraction: one hot
tenant carries a large share of several shapes. So the split draws a fixed
number of seeded candidates and keeps the one whose worst per-shape held-out
fraction is closest to the target. The selection reads shape counts only,
never a latency, so it cannot leak performance into the split.
"""

import hashlib
import random
from collections import Counter
from dataclasses import dataclass

PROFILING, HELD_OUT = "profiling", "held_out"


@dataclass(frozen=True)
class Item:
    seq: int
    shape: str
    account: int | None


@dataclass(frozen=True)
class Split:
    held_out_accounts: frozenset[int]
    profiling_accounts: frozenset[int]
    slice_of: dict[int, str]  # seq -> slice
    candidate: int
    worst_shape_deviation: float


def size_deciles(account_sizes: dict[int, int]) -> dict[int, int]:
    """Decile 0 = smallest accounts. Rank-based, ties broken by id."""
    ranked = sorted(account_sizes, key=lambda a: (account_sizes[a], a))
    n = len(ranked)
    return {a: min(9, i * 10 // n) for i, a in enumerate(ranked)}


def _candidate(items: list[Item], deciles: dict[int, int], held_frac: float,
               seed: int, k: int) -> tuple[frozenset[int], dict[int, str]]:
    rng = random.Random(f"split:{seed}:{k}")
    held: set[int] = set()
    for d in range(10):
        members = sorted(a for a, dd in deciles.items() if dd == d)
        rng.shuffle(members)
        held.update(members[:round(len(members) * held_frac)])
    inst_rng = random.Random(f"split:{seed}:{k}:instances")
    slice_of = {}
    for it in items:  # items are in seq order, so the instance draws are reproducible
        if it.account is None:
            slice_of[it.seq] = HELD_OUT if inst_rng.random() < held_frac else PROFILING
        else:
            slice_of[it.seq] = HELD_OUT if it.account in held else PROFILING
    return frozenset(held), slice_of


def per_shape(items: list[Item], slice_of: dict[int, str]) -> dict[str, dict]:
    total, held = Counter(), Counter()
    for it in items:
        total[it.shape] += 1
        held[it.shape] += slice_of[it.seq] == HELD_OUT
    return {s: {"total": total[s], PROFILING: total[s] - held[s], HELD_OUT: held[s],
                "held_out_fraction": round(held[s] / total[s], 4)}
            for s in sorted(total)}


def make(items: list[Item], account_sizes: dict[int, int], *, held_frac: float, seed: int,
         candidates: int) -> Split:
    items = sorted(items, key=lambda it: it.seq)
    deciles = size_deciles(account_sizes)
    best = None
    for k in range(candidates):
        held, slice_of = _candidate(items, deciles, held_frac, seed, k)
        dev = max(abs(v["held_out_fraction"] - held_frac)
                  for v in per_shape(items, slice_of).values())
        if best is None or dev < best[0]:
            best = (dev, k, held, slice_of)
    dev, k, held, slice_of = best
    return Split(held, frozenset(account_sizes) - held, slice_of, k, round(dev, 4))


def assignment_sha256(slice_of: dict[int, str]) -> str:
    """Fingerprint of the full seq -> slice vector, so a re-derived split can
    be checked against the committed manifest."""
    bits = "".join("h" if slice_of[s] == HELD_OUT else "p" for s in sorted(slice_of))
    return hashlib.sha256(bits.encode()).hexdigest()
