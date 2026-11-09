# ADR 0026: The eval suite runs the real proposer on modeled workloads; each loop iteration is a traced span

Status: accepted
Date: 2026-11-08
Source: RC1-634 Phase 6

## Context

The ticket asks for a billed agent-evals subject (recommendation quality on
held-out, refusal calibration by class, loop discipline, held-out isolation,
report honesty on the losing and under-sampled fixtures), LLM Obs with its own
ml_app and version tag and one traced span per loop iteration with spend, CI
Test Optimization, a catalog entity, and delivery monitor coverage. R18 adds
interval coverage as an eval metric. ADR 0023 says Phase 6 emits the refusal
count by class.

A real loop costs about 20 CU-hours and two to three days of runner time per
engagement. An eval that ran it would be run rarely and would mostly measure
Neon.

## Decision

- **Billed half: the real proposer, a modeled workload.** `evals.world`
  drives `loop.run_loop` with `ClaudeProposer` over the two Phase 4 fixture
  workloads through `FixtureRunner`. The validator, budgets, verdicts and
  output selection are the production code; only the experiments are
  modeled. The briefing is `phase4.static_context` with today's numbers taken
  from the fixture, so static and dynamic evidence describe one workload.
  Nothing tells the model it is a fixture. Budgets are the production ones.
  A run costs about $0.55.
- **Six cases.** Three billed: `recommendation-quality`,
  `refusal-calibration`, `loop-discipline`. Three free:
  `held-out-isolation`, `report-honesty`, `prediction-calibration`. Each loop
  is run once per process and charged to the first case that reads it.
- **What gates and what is advisory.**
  - Gating: the recommendation meets all 80 criteria on a synthesized
    held-out evaluation, with no loss against today; the refusal workload
    refuses as CRITERIA UNMET naming s07, and the qualifying one does not
    refuse (both directions, because a loop that always refuses is never
    wrong about a refusal); all four classes are reachable from scripted
    variants; every log verifies, stays within budget, logs each iteration
    with spend and request id, gives each valid proposal an outcome, and has
    one stop with a reason; the planted leak is refused and no context sent
    to the model mentions the held-out slice; the three fixture documents
    lead with their plants.
  - Advisory: whether the chosen bundle ranks at least as well as the
    fixture's reference bundle under the output rule (a measure of how well
    the model searched; the rule only chooses among tested bundles), and
    interval coverage on the real run against a 90% floor (R18; it is 14 of
    30, and a gate that is red from day one gets muted).
- **Cost.** Recorded from the proposer's own price table. The agent-evals
  snapshot prices Opus 5.5 cache reads at 0.1x input; the list price is
  0.05x, and this is the first cached Opus 5.5 caller on the estate. The
  first run recorded $0.539254; Datadog estimated 539,254,000 nanodollars.
- **Spans.** `pocbench.telemetry`: ml_app `poc-benchmark-agent`, version from
  the package, set on ddtrace's config after import (RC1-548). Each iteration
  is a workflow span `loop.iteration` around propose, validate and the budget
  checks, carrying model spend, tokens and the CU-hour estimate as metrics and
  the validator outcome as a tag; the anthropic llm span nests inside. The
  experiment runs outside the span: a span held open for two hours on a
  runner dies with the job, and the loop log already has the measured
  CU-hours. Iterations of one loop share a session id (the log's header hash).
  The eval suite traces under `service:evals` through `agent_evals.llmobs`;
  the Phase 4 loop job traces under `service:poc-benchmark-agent` when the
  workflow passes `DD_API_KEY`.
- **Gauges.** `--post-datadog` sends `pocbench.loop.outputs{output:*}` (every
  refusal class and recommendations, zeros included, from the committed
  engagement logs), `pocbench.prediction.interval_coverage` and
  `verdict_agreement`, per-case model spend, and case pass, fail and advisory
  counts. An unmeasured value is left out, never sent as zero.
- **Where it runs.** By hand and from the weekly agent-evals sweep, never in
  CI (the store credential lives on one machine). CI runs the scorers in
  `tests/test_evals.py` with the scripted proposer in Claude's place.
- **Datadog objects.** Catalog entity `poc-benchmark-agent`, the repo in
  `security_posture.REPOS`, and one catch-all monitor on
  `pocbench.evals.cases_failed > 0` (no-data off; the sweep's own monitor
  watches the sweep). No dashboard or SLO: a weekly suite gives about four
  points a month.

## Consequences

- The billed cases test the model's judgment on two workloads only. A
  modeled workload is cleaner than a real one (no noise across jobs, R18), so
  a pass here is necessary, not sufficient.
- The suite found its own first finding: on `qualifying`, Claude stopped at
  seven indexes, 0.17% costlier per successful query than the eight the
  script reaches, inside the 20% noise floor and one index build cheaper.
  The output rule ranks by point estimate; the advisory reports it red.
- This repo's Actions runs did not reach CI Visibility until the per-repo
  GitHub Actions toggle was turned on (found here, fixed 2026-11-08). The
  entity carries the CI pipeline fingerprint.
