import json

import pytest

from pocbench import gate
from pocbench.neon import Variant


class FakeNeon:
    def __init__(self):
        self.created, self.deleted = [], []

    def create_variant(self, name, cu, parent_id=None):
        v = Variant(f"br-{len(self.created)}", f"ep-{len(self.created)}", cu, name)
        self.created.append(v)
        return v

    def delete_variant(self, v):
        self.deleted.append(v.name)

    def connection_uri(self, branch_id, database, role):
        return f"postgresql://fake/{branch_id}"


def _overall(p99):
    return {"sample_count": 20_000, "ok_count": 20_000, "error_count": 0, "timeout_count": 0,
            "p50_ms": 1.0, "p95_ms": 10.0, "p99_ms": p99, "max_ms": p99 * 2, "p99_stable": True}


@pytest.fixture
def fake(monkeypatch):
    neon = FakeNeon()
    monkeypatch.setattr(gate, "Neon", lambda: neon)
    monkeypatch.setattr(gate, "_apply", lambda dsn, stmts: 1.0)
    return neon


def test_gate_deletes_every_branch_it_created_and_records_the_verdict(fake, monkeypatch, tmp_path):
    p99s = iter([20.0, 21.0, 20.5, 20.2, 20.8, 300.0, 40.0, 44.0, 41.0])
    monkeypatch.setattr(gate.loadgen, "run", lambda spec, inst, out: {
        "overall": _overall(next(p99s)), "probe": _overall(1.0), "throughput_qps": 100.0,
        "by_shape": {}})
    result = gate.run_gate(tmp_path, "neondb", "owner", measure_s=1)
    assert result["verdict"]["go"] is True
    assert sorted(result["branches_deleted"]) == sorted(v.name for v in fake.created)
    assert len(fake.created) == 3  # indexed, default, shared-compute supplementary
    assert result["supplementary"]["gating"] is False
    assert json.loads((tmp_path / "gate.json").read_text())["verdict"]["go"] is True


def test_branches_are_deleted_even_when_a_run_fails(fake, monkeypatch, tmp_path):
    def boom(spec, inst, out):
        raise RuntimeError("compute went away")

    monkeypatch.setattr(gate.loadgen, "run", boom)
    with pytest.raises(RuntimeError):
        gate.run_gate(tmp_path, "neondb", "owner", measure_s=1)
    assert fake.deleted and set(fake.deleted) == {v.name for v in fake.created}
    assert "branches_deleted" in json.loads((tmp_path / "gate.json").read_text())


def test_report_renders_the_verdict_runs_and_housekeeping(fake, monkeypatch, tmp_path):
    p99s = iter([20.0, 26.0, 20.5, 20.2, 20.8, 300.0, 40.0, 44.0, 41.0])
    monkeypatch.setattr(gate.loadgen, "run", lambda spec, inst, out: {
        "overall": _overall(next(p99s)), "probe": _overall(1.0), "throughput_qps": 100.0,
        "by_shape": {}})
    text = gate.render(gate.run_gate(tmp_path, "neondb", "owner", measure_s=1), "https://x")
    assert "## Verdict: FALLBACK" in text
    assert "p99 spread" in text  # the reason is printed, not just the verdict
    assert text.count("| gate-") == 5 and text.count("| shared-") == 3
    assert "Branches deleted:" in text and "Delete failures" not in text
