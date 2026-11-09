"""The suite's world: the real loop, the real Claude proposer, a modeled
workload (RC1-634, ADR 0026).

The billed half drives `loop.run_loop` with `ClaudeProposer` over the two
Phase 4 fixture workloads (`qualifying` and `refusal-criteria-unmet`) through
`FixtureRunner`, so the model makes every proposal it would make in an
engagement while the experiments cost nothing: no Neon branch, no runner.
The validator, budgets, verdicts and output selection are the production
code. Each fixture names the output a correct loop reaches, which is the
ground truth the cases score against.

The briefing is the production one (`phase4.static_context`: the frozen
criteria, the permitted space, the Phase 1 shapes and plans), with today's
numbers taken from the fixture's naive model instead of the Phase 1 capture,
so the static and dynamic evidence describe one workload. Nothing tells the
model it is a fixture.

Loop runs are cached per process: two cases score the `qualifying` run, so
the suite pays for each loop once. Logs are written under `eval-runs/loops/`
(git-ignored) so a run can be read after the fact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pocbench import config, criteria, fixtures, loop, phase4
from pocbench.proposer import ClaudeProposer

LOOP_DIR = config.REPO_ROOT / "eval-runs" / "loops"
BILLED_FIXTURES = ("qualifying", "refusal-criteria-unmet")


class RecordingProposer:
    """Wraps a proposer and keeps every context it was handed, so the
    isolation case can check what the model actually saw."""

    def __init__(self, inner):
        self.inner = inner
        self.contexts: list[dict] = []

    def worst_case_usd(self) -> float:
        return self.inner.worst_case_usd()

    def propose(self, context: dict) -> loop.Proposal:
        self.contexts.append(context)
        return self.inner.propose(context)


@dataclass
class LoopRun:
    fixture: str
    fx: dict
    log_path: Path
    result: dict
    decision: dict
    runner: fixtures.FixtureRunner
    static: dict
    contexts: list[dict] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def log(self) -> loop.LoopLog:
        return loop.LoopLog(self.log_path, {"criteria_sha256": "fixture",
                                            "config_space_sha256": "fixture"})

    def proposals(self) -> list[dict]:
        return self.log.of("proposal")

    def usage(self) -> dict[str, float]:
        tot = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
               "cache_read_input_tokens": 0, "model_usd": 0.0}
        for r in self.proposals():
            for k in tot:
                src = r.get("usage") or {}
                tot[k] += (r.get("model_usd") or 0.0) if k == "model_usd" else (src.get(k) or 0)
        return tot


def briefing(fx: dict, doc: dict, sp: dict) -> dict:
    """The production static context for a fixture workload."""
    prof = phase4._profile()
    naive = [c | {"config": "naive"} for c in fixtures.FixtureRunner(fx).naive_cells()]
    static = phase4.static_context(doc, sp, prof, naive)
    for s in static["shapes"]:
        # Today's figures come from this workload's naive model (in
        # phase3_baselines_dev), not from the Phase 1 capture.
        s.pop("today_on_naive", None)
        s.pop("time_share", None)
        s["log_share"] = fx["shapes"][s["id"]]["share"]
    return static


_cache: dict[str, LoopRun] = {}


def claude_loop(name: str) -> LoopRun:
    """The real loop with the real proposer over one fixture workload. Raises
    whatever the API raises; the case records it as an error."""
    if name in _cache:
        return _cache[name]
    fx = fixtures.load(name)
    doc = criteria.load_verified()
    sp = config.config_space().data
    static = briefing(fx, doc, sp)
    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = LOOP_DIR / f"{stamp}-{name}.jsonl"
    proposer = RecordingProposer(ClaudeProposer(static))
    t0 = time.monotonic()
    result, decision, runner = fixtures.run_fixture(fx, doc, sp, path, proposer=proposer,
                                                    static_context=static)
    run = LoopRun(name, fx, path, result, decision, runner, static, proposer.contexts,
                  (time.monotonic() - t0) * 1000)
    _cache[name] = run
    return run


def cached() -> list[LoopRun]:
    return list(_cache.values())
