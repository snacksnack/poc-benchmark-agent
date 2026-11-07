# ADR 0022: In the loop, the model proposes and Python decides; predictions use the frozen verdict rules

Status: accepted
Date: 2026-11-07
Source: RC1-632 Phase 4

## Context

ADR 0005 makes the recommendation step an adaptive loop with experiment,
compute and model-spend budgets. Epic rule 1 says everything that protects
the result is enforced in Python, never asked of the model. The frozen
criteria (ADR 0019) define verdicts for the held-out evaluation, but nothing
yet applied them. Phase 3 also showed that with 2 development repetitions the
naive baseline's slow shapes (s09: about 85 successful queries) cannot
support even a p50, so a single experiment's paired baseline would leave
every no-regression check on those shapes unsupported.

## Decision

- **Roles.** Claude (`claude-opus-5-5`, effort high) reads the profile, the
  frozen criteria, the space and every development result so far, states a
  hypothesis and names one experiment, or stops. It returns a JSON object
  under a schema (structured outputs; forced tool use is not available on
  this model). Python validates the proposal (`pocbench.space`), enforces the
  three budgets, runs the experiment, logs it, and after the stop chooses the
  output (ADR 0023). The model never chooses the winner.
- **Validator.** A tier from `tiers.compute_units`, index ids from
  `[[index]]`, no duplicate, no index made redundant by another in the bundle
  (same table and kind, leading columns), no bundle already tested, nothing
  else. Memory and autoscaling settings and free DDL are rejected by name as
  out of scope. A rejected proposal is logged and costs model spend, not an
  experiment; three in a row stop the loop.
- **Budgets.** `[budgets.phase4]`: 20 experiments, 120 CU-hours, $10 of model
  spend. Model spend is computed from each response's usage at the listed
  prices; the loop refuses a call whose worst case would cross the cap.
  Compute is the CU-hours both branches held during the experiment, including
  index builds and aborted experiments; an experiment whose estimate would
  cross the cap is not started. Exhausting a budget is a recorded stop.
- **One experiment.** The candidate on its own branch (primary keys plus its
  indexes, at its tier) and the naive baseline, measured interleaved through
  the development entry point: profiling slice, 8 and 32 sessions, both cache
  states, 2 repetitions (`protocol.dev`). The cost estimate assumed the
  default baseline interleaved; naive is used because it is "today" in the
  criteria.
- **Naive is pooled across experiments.** For predictions, naive is pooled
  over every development repetition so far (same configuration, same slice).
  Its raw logs are committed gzipped so later jobs can pool them; the
  per-experiment naive runs also measure drift across jobs (R1, R10).
- **Predictions use the frozen rules.** `pocbench.verdicts` executes
  `method.verdicts` as written. Interpretations the text leaves open: a
  missing cell is not met; a shape with no successful query has no latency;
  the noise rule reads the baseline's per-repetition spread of the compared
  statistic and treats an unmeasured spread as inconclusive; a failing
  baseline's percentiles are flagged survivors-only.
- **Tail support is projected for predictions only.** Every repetition
  replays the same seeded query sequence, so 5 evaluation repetitions give
  2.5 times the development observations beyond a percentile and the same
  distinct instances. A development tail counts as supported for a
  prediction when 2.5 times its observations beyond, and its distinct
  instances unscaled, reach the ADR 0018 floors; such verdicts are labeled
  `projected`. Held-out verdicts use measured support only.
- **Isolation (R3).** The loop accepts only profiling-slice evidence;
  `loop.require_profiling` raises on anything else, the development entry
  point re-checks every instance (ADR 0020), and the held-out slice is only
  released against a frozen record.

## Consequences

- A first live call cost $0.10 with the static briefing cached, so the
  experiment or compute budget will bind before the model budget.
- At about (candidate CU + 2) x 1.7 CU-hours per experiment, the compute
  budget binds near 15-18 experiments at mid tiers. That is the budget doing
  its job; the stop is logged.
- Predictions are labeled as predictions everywhere. The profiled-versus-
  held-out gap is measured on the same protocol in evaluation (ADR 0024).
