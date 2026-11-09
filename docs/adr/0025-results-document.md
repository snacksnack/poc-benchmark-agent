# ADR 0025: The results document is generated from verified evidence, one platform per figure, bad news first

Status: accepted
Date: 2026-11-08
Source: RC1-633 Phase 5

## Context

Phase 5 turns the Phase 4 run into the customer-facing document. The ticket
asks for per-criterion verdicts, a headline with uncertainty, regressions by
name, an error and timeout comparison, the profiled versus held-out gain,
prediction accuracy, cost lines kept apart, platform labels (ADR 0007), a
refusal case, and an honest-reporting rule: a loss, a missed criterion or an
under-sampled key tail leads the summary. The real run ended in a refusal, so
the candidate paths can only be proved from fixtures.

## Decision

- **Generated, never edited.** `pocbench results-doc` writes
  `docs/results.md`; `pocbench results-fixture <name>` writes
  `docs/fixtures/results-<name>.md`. Tests fail when a committed document
  differs from what the code renders today.
- **Verified inputs.** The real document reads the criteria through
  `load_verified`, the frozen output through `load_frozen`, and the
  evaluation record through `record.verify`; every scored row must pin the
  frozen sha256, and the committed `eval-verdicts.json` must re-derive
  exactly from `eval-cells.json`. Errors and timeouts per shape come from the
  hash-verified record rows and must agree with the pooled failure fraction.
  `harness.pool` now keeps them apart for future runs.
- **What is scored.** The frozen candidate against today; after a refusal,
  both baselines (the evaluation ran nothing else). Verdicts use measured
  support only.
- **Lead order.** Refusal (with the best tested bundle), then losses against
  today (failed no-regression, throughput or cost criteria), then
  under-sampled key tails (goal-shape latency ceilings that are unsupported),
  then every other unmet criterion. The headline follows. With nothing to
  lead, the headline is first.
- **Headline.** Per goal shape at the objective level, warm, held-out: the
  percentile the shape's criterion names, with its 95% interval, tail support,
  and the error and timeout share. A percentile from a configuration over the
  error ceiling is labeled "latency of the survivors only". Cost is the
  candidate's cost per successful query over today's at the same cell.
- **Prediction accuracy is bundle-level.** For a candidate: verdict agreement
  per criterion and whether the held-out value fell inside the predicted
  interval. Per-change effects are the frozen document's, isolated or "not
  decomposed", labeled as development evidence. Today's baseline is checked
  in every outcome (development pool against held-out), because it is the one
  bundle measured both ways after a refusal.
- **Cost lines.** Cost per successful query over the stated window; index
  build (one-time) and index storage (monthly) from the evaluation jobs'
  builds; projected operating cost with its assumptions; benchmark execution
  by job task. Never summed (`phase3.cost_lines`).
- **Platforms.** `verdicts.one_platform` raises `PlatformJoinError` when
  cells from two platforms reach one comparison; `verdicts.evaluate` and every
  figure function call it. Each platform gets its own summary and section;
  every table carries a `Platform:` caption. A platform without cost says so.
  Fixture cells are platform `fixture-model`, and fixture documents carry a
  banner saying no figure is a measurement.
- **Fixtures.** `fixtures/phase5/*.json` name a Phase 4 workload and a plant
  on the held-out slice only (`latency_factor`, `distinct_cap`). The builder
  runs the real loop to a freeze, then synthesizes the evaluation the freeze
  unlocks. The loop log takes an injectable clock so fixture logs, refusal
  ids and frozen hashes are the same on every run.

## Consequences

- A document can only say what the record supports. If the record, the cells
  or the committed verdicts drift, generation fails instead of printing.
- The real run's "before" figures are mostly survivor-only and under-sampled
  (today times out on most goal-shape queries at 32 sessions), so the epic's
  "cut p99 from X to Y" headline has no X for this run. The document says so
  rather than printing a number that looks like one.
- The fallback has never run; its document shape is proved by a test that
  splits fixture cells across two platforms.
