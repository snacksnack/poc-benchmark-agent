"""The append-only record: hash chain, pins, and the rows it refuses."""

import json

import pytest

from pocbench import config, record

LM = config.load_model()
SPACE = config.config_space()
PINS = {"criteria_sha256": "c" * 64, "load_model_sha256": LM.sha256,
        "config_space_sha256": SPACE.sha256}


def _open(path, **over):
    kw = {"criteria_sha256": PINS["criteria_sha256"], "load_model": LM.data,
          "load_model_sha256": LM.sha256, "config_space_sha256": SPACE.sha256,
          "cache_states": SPACE.data["cache_states"]} | over
    return record.Record(path, **kw)


def _row(**over):
    t = LM.data["timing"]
    spec = {"warmup_s": t["warmup_seconds"], "measure_s": t["measure_seconds"],
            "timeout_ms": t["per_query_timeout_ms"], "grace_ms": t["client_grace_ms"],
            "load_model_sha256": LM.sha256}
    row = {"status": record.SCORED, "entry_point": "development", "slice": "profiling",
           **PINS, "cache_state": "warm", "level": 8, "spec": spec}
    for k, v in over.items():
        if k.startswith("spec_"):
            row["spec"] = row["spec"] | {k[5:]: v}
        else:
            row[k] = v
    return row


def test_rows_chain_and_verify(tmp_path):
    rec = _open(tmp_path / "r.jsonl")
    rec.append(_row())
    rec.append(_row(status=record.REJECTED, reasons=["slow network"]))
    rows = record.verify(tmp_path / "r.jsonl")
    assert [r["kind"] for r in rows] == ["header", "run", "run"]
    assert rows[1]["prev_sha256"] == rows[0]["row_sha256"]


def test_reopening_appends_to_the_same_chain(tmp_path):
    _open(tmp_path / "r.jsonl").append(_row())
    _open(tmp_path / "r.jsonl").append(_row(level=32))
    assert len(record.verify(tmp_path / "r.jsonl")) == 3


def test_an_edited_row_is_detected(tmp_path):
    path = tmp_path / "r.jsonl"
    _open(path).append(_row())
    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["level"] = 32
    lines[1] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(record.RecordError, match="edited"):
        record.verify(path)


def test_a_deleted_row_breaks_the_chain(tmp_path):
    path = tmp_path / "r.jsonl"
    rec = _open(path)
    rec.append(_row())
    rec.append(_row(level=32))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(record.RecordError, match="chain broken"):
        record.verify(path)


def test_a_dev_aid_with_a_shortened_window_cannot_write(tmp_path):
    rec = _open(tmp_path / "r.jsonl")
    with pytest.raises(record.RecordError, match="measure_s"):
        rec.append(_row(spec_measure_s=30, spec_warmup_s=5))


def test_development_rows_are_profiling_slice_only(tmp_path):
    rec = _open(tmp_path / "r.jsonl")
    with pytest.raises(record.RecordError, match="profiling slice"):
        rec.append(_row(slice="held_out"))
    with pytest.raises(record.RecordError, match="entry point"):
        rec.append(_row(entry_point="dev-aid"))


def test_warm_up_must_match_the_cache_state(tmp_path):
    rec = _open(tmp_path / "r.jsonl")
    rec.append(_row(cache_state="post-restart-with-eviction", spec_warmup_s=0))
    with pytest.raises(record.RecordError, match="warm-up"):
        rec.append(_row(cache_state="warm", spec_warmup_s=0))
    with pytest.raises(record.RecordError, match="cache state"):
        rec.append(_row(cache_state="cold"))


def test_rows_must_carry_the_record_pins(tmp_path):
    rec = _open(tmp_path / "r.jsonl")
    with pytest.raises(record.RecordError, match="criteria_sha256"):
        rec.append(_row(criteria_sha256="d" * 64))


def test_a_new_criteria_freeze_cannot_reuse_an_old_record(tmp_path):
    _open(tmp_path / "r.jsonl").append(_row())
    with pytest.raises(record.RecordError, match="other pins"):
        _open(tmp_path / "r.jsonl", criteria_sha256="d" * 64)
