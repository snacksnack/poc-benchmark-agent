import json
import shutil
import tomllib
from pathlib import Path

import pytest

from pocbench import config, criteria, stats


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of every pinned input, so tests can tamper without touching the repo."""
    for rel in criteria.PINNED_INPUTS.values():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.REPO_ROOT / rel, dst)
    return tmp_path


@pytest.fixture
def frozen(root: Path) -> Path:
    criteria.freeze(criteria.build(root, frozen_at="2026-11-07"),
                    root / "criteria" / "criteria.json", root / "criteria" / "criteria.sha256")
    return root


def test_freshly_frozen_criteria_verify(frozen: Path):
    doc = criteria.verify(frozen)
    assert len(doc["criteria"]) > 0
    assert set(doc["pins"]) == set(criteria.PINNED_INPUTS)


def test_post_hoc_edit_of_a_criterion_fails_the_run(frozen: Path):
    path = frozen / "criteria" / "criteria.json"
    doc = json.loads(path.read_text())
    doc["criteria"][0]["ceiling_ms"] *= 10  # loosen a ceiling after seeing results
    path.write_bytes(criteria.canonical(doc))
    with pytest.raises(criteria.CriteriaPinError, match="criteria.json sha256"):
        criteria.verify(frozen)


def test_even_a_whitespace_edit_fails(frozen: Path):
    path = frozen / "criteria" / "criteria.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(criteria.CriteriaPinError):
        criteria.verify(frozen)


@pytest.mark.parametrize("name", sorted(criteria.PINNED_INPUTS))
def test_changing_any_pinned_input_fails_the_run(frozen: Path, name: str):
    path = frozen / criteria.PINNED_INPUTS[name]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(criteria.CriteriaPinError, match="changed after the freeze"):
        criteria.verify(frozen)


def test_missing_criteria_fail_and_refreezing_is_refused(root: Path, frozen: Path):
    with pytest.raises(criteria.CriteriaPinError, match="already frozen"):
        criteria.freeze(criteria.build(frozen), frozen / "criteria" / "criteria.json",
                        frozen / "criteria" / "criteria.sha256")
    (frozen / "criteria" / "criteria.sha256").unlink()
    with pytest.raises(criteria.CriteriaPinError, match="not frozen"):
        criteria.verify(frozen)


def test_generation_is_deterministic(root: Path):
    a = criteria.canonical(criteria.build(root, frozen_at="2026-11-07"))
    b = criteria.canonical(criteria.build(root, frozen_at="2026-11-07"))
    assert a == b


def test_every_latency_criterion_names_level_cache_state_and_percentile(root: Path):
    doc = criteria.build(root, frozen_at="2026-11-07")
    lm = config.load_model(root / "config" / "load-model.toml").data
    for c in doc["criteria"]:
        if c["kind"] in ("latency_ceiling", "no_regression"):
            assert c["level"] not in lm["sessions"]["diagnostic_only"]
            assert c["cache_state"] in doc["method"]["cache_states"]
            assert "percentile" in c


def test_goal_shapes_get_ceilings_and_every_other_shape_no_regression(root: Path):
    doc = criteria.build(root, frozen_at="2026-11-07")
    manifest = json.loads((root / "results/phase1/split-manifest.json").read_text())
    goal = set(doc["goal"]["shapes"])
    ceiling = {c["shape"] for c in doc["criteria"] if c["kind"] == "latency_ceiling"}
    noreg = {c["shape"] for c in doc["criteria"] if c["kind"] == "no_regression"}
    assert ceiling == goal and not goal & noreg
    assert ceiling | noreg == set(manifest["shapes"])


def test_thin_held_out_shape_drops_to_a_supported_percentile(root: Path):
    doc = criteria.build(root, frozen_at="2026-11-07")
    manifest = json.loads((root / "results/phase1/split-manifest.json").read_text())
    for c in doc["criteria"]:
        if c["kind"] != "latency_ceiling":
            continue
        sup = manifest["shapes"][c["shape"]]["held_out_tail_supported"]
        if sup["p99"]:
            assert c["percentile"] == 99
        else:
            assert c["percentile"] < 99 and "note" in c


def test_post_restart_ceiling_is_the_multiplier_times_warm(root: Path):
    doc = criteria.build(root, frozen_at="2026-11-07")
    goal = tomllib.loads((root / "config/goal.toml").read_text())
    k = goal["ceilings"]["post_restart_multiplier"]
    by = {(c["shape"], c["level"], c["cache_state"]): c["ceiling_ms"]
          for c in doc["criteria"] if c["kind"] == "latency_ceiling"}
    for (shape, level, cs), ms in by.items():
        if cs == "post-restart-with-eviction":
            assert ms == by[(shape, level, "warm")] * k


def test_goal_fingerprint_mismatch_is_rejected(root: Path):
    path = root / "config/goal.toml"
    path.write_text(path.read_text().replace('fingerprint = "', 'fingerprint = "x', 1))
    with pytest.raises(ValueError, match="fingerprint"):
        criteria.build(root)


def test_committed_criteria_verify():
    """The repo's frozen criteria hold against the repo's current inputs."""
    criteria.load_verified()


def test_quantile_interval_brackets_the_point_and_tightens_with_n():
    lo, hi = stats.quantile_ci_ranks(10_000, 99)
    assert lo < 9_900 < hi
    small = stats.quantile_ci_ranks(1_000, 99)
    assert (hi - lo) / 10_000 < (small[1] - small[0]) / 1_000
    values = list(range(1, 101))
    a, b = stats.quantile_ci(values, 50)
    assert a <= 50 <= b
