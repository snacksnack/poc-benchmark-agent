"""The loop log page (RC1-635): verified before it is read, every row
rendered, the model's words verbatim, and the committed page current."""

import json
import shutil

import pytest

from pocbench import loop_report, record


@pytest.fixture(scope="module")
def committed():
    return loop_report.load()


def test_committed_page_is_current(committed):
    assert loop_report.DOC_PATH.read_text() == loop_report.render(*committed), \
        "regenerate: uv run pocbench loop-log-doc --write docs/loop-log.md"


def test_every_row_after_the_header_gets_a_section(committed):
    rows, _, _ = committed
    md = loop_report.render(*committed)
    assert md.count("\n### ") == len(rows) - 1


def test_the_model_is_quoted_in_full(committed):
    rows, _, _ = committed
    md = loop_report.render(*committed)
    for r in rows:
        for key in ("why", "hypothesis", "expected", "stop_reason", "reason"):
            if r.get(key) and r["kind"] in ("proposal", "stop"):
                for line in r[key].splitlines():
                    assert line in md, (r["kind"], key)


def test_the_page_names_the_refusal(committed):
    md = loop_report.render(*committed)
    assert "Refusal `refusal-4e24a549455d`, class CRITERIA UNMET" in md


def _copy(tmp_path):
    dst = tmp_path / "loop-log.jsonl"
    shutil.copy(loop_report.LOG_PATH, dst)
    return dst


def test_an_edited_row_is_refused(tmp_path):
    path = _copy(tmp_path)
    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["why"] = row["why"].replace("2 CU", "1 CU")
    lines[1] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(record.RecordError, match="edited"):
        loop_report.load(path)


def test_a_log_that_continues_past_the_freeze_is_refused(tmp_path):
    path = _copy(tmp_path)
    rows = record.verify(path)
    extra = record._seal({"at": "2026-11-09T00:00:00+00:00", "kind": "stop", "by": "test",
                          "budget": False, "reason": "appended later"},
                         rows[-1]["row_sha256"])
    with path.open("a") as fh:
        fh.write(json.dumps(extra, sort_keys=True) + "\n")
    with pytest.raises(record.RecordError, match="frozen output pinned"):
        loop_report.load(path)
