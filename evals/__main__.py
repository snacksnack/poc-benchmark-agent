"""`python -m evals`: the poc-benchmark-agent eval suite (RC1-634, ADR 0026).

Six cases; three are billed, because the real Claude proposer drives the
real loop over two modeled workloads (a few dollars a run; each record
carries its exact cost). The other three are free and offline.

    uv run --group evals python -m evals
    uv run --group evals python -m evals --case report-honesty
    uv run --group evals python -m evals --post-datadog

Needs ANTHROPIC_API_KEY exported for the billed cases (the anthropic SDK
reads it; this repo's .env holds only Neon credentials). EVAL_DATABASE_URL
makes the run comparable to every earlier one; without it agent_evals
records to local JSONL and says so. With DD_API_KEY, the run is traced in LLM
Obs under ml_app poc-benchmark-agent, service evals (each loop iteration a
span with its spend), and `--post-datadog` publishes the pocbench.* gauges.
Both are decoration: the run is unaffected without them.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from agent_evals import llmobs
from agent_evals.runner import UnknownCase, exit_code, print_result, record_run, select_cases

from evals import subject, world
from pocbench import loop, record, telemetry

CLASSES = (loop.BUDGET_EXHAUSTED, loop.INSUFFICIENT_EVIDENCE, loop.NO_IMPROVEMENT,
           loop.CRITERIA_UNMET)


def _slug(cls: str) -> str:
    return cls.lower().replace(" ", "_")


def engagement_outputs() -> dict[str, int]:
    """Loop outputs across the committed engagement logs: refusals by class
    (ADR 0023's metric) and recommendations. Every class is reported, zero
    included, so a dashboard shows the classes that never fired."""
    out = {_slug(c): 0 for c in CLASSES} | {"recommendation": 0}
    for path in subject.ENGAGEMENT_LOGS:
        for r in record.verify(path):
            if r.get("kind") != "frozen":
                continue
            cls = r.get("class")
            out[_slug(cls) if cls else "recommendation"] += 1
    return out


def gauges(results) -> list[tuple[str, float, dict[str, str]]]:
    """The publishable figures. A None observation is left out, never sent
    as zero: "not measured" and "zero" are different facts."""
    by = {r.case_id: r for r in results}
    pts: list[tuple[str, float, dict[str, str]]] = []

    def put(metric: str, case: str, key: str, tags: dict | None = None) -> None:
        r = by.get(case)
        if r is None or r.error:
            return
        v = r.observations.get(key)
        if isinstance(v, bool):
            v = int(v)
        if v is not None:
            pts.append((metric, float(v), tags or {}))

    put("prediction.interval_coverage", "prediction-calibration", "interval_coverage")
    put("prediction.verdict_agreement", "prediction-calibration", "verdict_agreement")
    put("evals.recommendation.cost_ratio", "recommendation-quality", "cost_ratio_to_reference")
    for r in results:
        if r.error:
            continue
        if r.usage.cost_usd:
            pts.append(("evals.model_usd", float(r.usage.cost_usd), {"case": r.case_id}))
    for kind, n in engagement_outputs().items():
        pts.append(("loop.outputs", float(n), {"output": kind}))
    pts.append(("evals.cases_passed", float(sum(r.passed for r in results)), {}))
    pts.append(("evals.cases_failed", float(sum(not r.passed for r in results)), {}))
    pts.append(("evals.advisory_failures",
                float(sum(len(r.advisory_failures) for r in results)), {}))
    return pts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="run a single case by id")
    parser.add_argument("--post-datadog", action="store_true",
                        help="publish the pocbench.* gauges to Datadog")
    args = parser.parse_args(argv)
    try:
        cases = select_cases(subject.CASES, args.case)
    except UnknownCase as exc:
        print(exc, file=sys.stderr)
        return 2
    billed = [c.id for c in cases if "billed" in c.tags]
    if billed and not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"ANTHROPIC_API_KEY is not exported; billed cases {billed} need it.",
              file=sys.stderr)
        return 78

    telemetry.apply_version()  # or the eval traces index with an empty version tag
    traced = llmobs.enable(telemetry.ML_APP, service="evals")
    version = subject.version()
    print(f"{len(cases)} case(s) against {version.subject} {version.code_version}, "
          f"{version.model}, {version.prompt_version}; billed: {billed or 'none'}; "
          f"tracing {'on' if traced else 'off'}.\n")
    started = datetime.now(UTC)
    results = []
    for case in cases:
        with llmobs.case(case.id, input_data=case.input) as span:
            result = subject.run(case)
            span.record(result)
        print_result(result)
        results.append(result)
    for r in world.cached():
        print(f"loop log: {r.log_path}")
    record_run(version, started, results)
    if not os.environ.get("EVAL_DATABASE_URL"):
        print("\nEVAL_DATABASE_URL is not set: this run recorded to local JSONL and is not "
              "comparable to the shared history.")
    total = sum(float(r.usage.cost_usd) for r in results)
    print(f"model spend this run: ${total:.4f}")
    if args.post_datadog:
        pts = gauges(results)
        ok = telemetry.submit(pts)
        print(f"datadog: {len(pts)} gauge(s) {'submitted' if ok else 'not submitted'}")
    telemetry.flush()
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
