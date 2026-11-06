import copy
import hashlib

import pytest

from pocbench import config, sizing, spike


def test_load_model_fingerprint_is_sha256_of_the_file_bytes():
    pinned = config.load_model()
    assert pinned.sha256 == hashlib.sha256(config.LOAD_MODEL_PATH.read_bytes()).hexdigest()


def test_committed_load_model_matches_the_epic():
    lm = config.load_model().data
    assert lm["generator"] == "closed-loop"
    assert lm["sessions"]["levels"] == [1, 8, 32]
    assert lm["sessions"]["diagnostic_only"] == [1]
    assert lm["errors"]["count_as_results"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(generator="open-loop"),
        lambda m: m["sessions"].update(levels=[8, 1]),
        lambda m: m["sessions"].update(diagnostic_only=[1, 8, 32]),
        lambda m: m["connections"].update(pool_size=10),
        lambda m: m["errors"].update(count_as_results=False),
        lambda m: m["errors"].update(error_ceiling_fraction=0.5, abort_run_fraction=0.25),
    ],
)
def test_validator_rejects_a_load_model_the_harness_cannot_honor(mutate):
    lm = copy.deepcopy(config.load_model().data)
    mutate(lm)
    with pytest.raises(ValueError):
        config.validate_load_model(lm)


def test_config_space_excludes_shared_compute_and_keeps_baselines_on_the_default_tier():
    space = config.config_space().data
    assert min(space["tiers"]["compute_units"]) >= 1
    assert space["tiers"]["default_cu"] in space["tiers"]["compute_units"]
    assert space["baselines"]["default"]["cu"] == space["tiers"]["default_cu"]
    assert space["baselines"]["naive"]["cu"] == space["tiers"]["default_cu"]


def test_split_fractions_sum_to_one():
    split = config.config_space().data["split"]
    assert split["profiling_fraction"] + split["held_out_fraction"] == pytest.approx(1.0)


def test_every_cache_state_in_a_protocol_has_a_named_procedure():
    space = config.config_space().data
    for proto in space["protocol"].values():
        for state in proto["cache_states"]:
            assert space["cache_states"][state]["procedure"]


def test_index_candidates_are_bounded_and_uniquely_named():
    ids = [c["id"] for c in config.config_space().data["index"]]
    assert len(ids) == len(set(ids)) == 8


def test_spike_variant_indexes_are_drawn_from_the_candidate_set():
    # The spike's indexed variant is I1, I2, I5, I7; it must not invent indexes.
    ddl = {c["ddl"].split(" ON ")[1] for c in config.config_space().data["index"]}
    for stmt in spike.VARIANT_INDEXES:
        if stmt.startswith("CREATE INDEX"):
            assert stmt.split(" ON ")[1] in ddl


def test_btree_estimate_is_in_the_expected_range():
    # 50M rows, 16-byte key: ~28 bytes per entry at 90% fill -> about 1.6 GB.
    size = sizing.btree_bytes(50_000_000, 16)
    assert 1.4e9 < size < 1.8e9


def test_partial_index_scales_with_selectivity_and_brin_is_tiny():
    ds = config.config_space().data["dataset"]
    by_id = {c["id"]: c for c in config.config_space().data["index"]}
    assert sizing.candidate_bytes(by_id["I6"], ds) < sizing.candidate_bytes(by_id["I5"], ds) / 100
    assert sizing.candidate_bytes(by_id["I4"], ds) < 1e6
