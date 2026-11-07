import random

import pytest

from pocbench import split

SIZES = {a: 5000 // a + 3 for a in range(1, 201)}


def _items(n=20_000, seed=0):
    rng = random.Random(seed)
    accounts = list(SIZES)
    weights = list(SIZES.values())
    out = []
    for seq in range(n):
        shape = rng.choice(["a", "b", "c", "none"])
        account = None if shape == "none" else rng.choices(accounts, weights)[0]
        out.append(split.Item(seq, shape, account))
    return out


def test_size_deciles_rank_accounts_from_smallest():
    d = split.size_deciles(SIZES)
    assert d[1] == 9 and d[200] == 0
    assert sorted(set(d.values())) == list(range(10))


def test_no_account_lands_in_both_slices_and_every_decile_is_held_out():
    items = _items()
    s = split.make(items, SIZES, held_frac=0.3, seed=628, candidates=8)
    assert not s.held_out_accounts & s.profiling_accounts
    for it in items:
        if it.account is not None:
            expected = split.HELD_OUT if it.account in s.held_out_accounts else split.PROFILING
            assert s.slice_of[it.seq] == expected
    deciles = split.size_deciles(SIZES)
    assert {deciles[a] for a in s.held_out_accounts} == set(range(10))
    assert len(s.held_out_accounts) == pytest.approx(0.3 * len(SIZES), abs=10)


def test_tenantless_instances_split_by_instance():
    items = _items()
    s = split.make(items, SIZES, held_frac=0.3, seed=628, candidates=4)
    none = [it for it in items if it.account is None]
    held = sum(s.slice_of[it.seq] == split.HELD_OUT for it in none)
    assert held / len(none) == pytest.approx(0.3, abs=0.02)


def test_split_is_reproducible_and_more_candidates_never_do_worse():
    items = _items()
    a = split.make(items, SIZES, held_frac=0.3, seed=628, candidates=16)
    b = split.make(list(reversed(items)), SIZES, held_frac=0.3, seed=628, candidates=16)
    assert split.assignment_sha256(a.slice_of) == split.assignment_sha256(b.slice_of)
    one = split.make(items, SIZES, held_frac=0.3, seed=628, candidates=1)
    assert a.worst_shape_deviation <= one.worst_shape_deviation


def test_per_shape_counts_add_up():
    items = _items(2_000)
    s = split.make(items, SIZES, held_frac=0.3, seed=1, candidates=2)
    counts = split.per_shape(items, s.slice_of)
    assert sum(v["total"] for v in counts.values()) == len(items)
    for v in counts.values():
        assert v[split.PROFILING] + v[split.HELD_OUT] == v["total"]
