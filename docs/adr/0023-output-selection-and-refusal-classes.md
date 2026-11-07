# ADR 0023: The output is chosen in Python; refusal classes have a fixed precedence

Status: accepted
Date: 2026-11-07
Source: RC1-632 Phase 4

## Context

ADR 0011 allows exactly two outputs, a frozen recommendation or a refusal;
ADR 0008 names four refusal classes. The ticket asks for bundle-level
predicted effects with confidence, per-change effects only where a change ran
in isolation (ADR 0006), and a refusal count by class as a metric. Which
output, and which class, must not be the model's call.

## Decision

- **Re-prediction.** After the stop, every tested bundle is re-predicted
  against the final naive pool, so all comparisons use the same, largest
  baseline evidence.
- **Recommendation.** A bundle qualifies when all 80 predicted criteria are
  met. Among qualifying bundles, the one with the lowest cost per successful
  query at 32 sessions, warm (the goal's objective) is chosen; ties go to the
  lower tier, then fewer indexes. The frozen document holds the
  configuration, its DDL, per-criterion predicted value, interval and
  confidence (high when the conservative end clears the bound by more than
  the 20% smallest pass effect, medium when met within it, low when not met;
  overall the lowest), and per-change effects only where two tested bundles
  differ by exactly that change. It pins the criteria, config space, loop
  log and development record by hash.
- **Refusal.** Otherwise the output is a refusal: "no tested candidate within
  the permitted space and budget met the success criteria", every tested
  bundle with its evidence, the stopping reason, the shapes named by unmet
  criteria of the best tested bundle (most criteria met, then cheapest), and
  that bundle labeled "best tested candidate; fails to qualify". Class,
  first match wins:
  1. **BUDGET EXHAUSTED:** a budget stopped the loop (experiments, compute or
     model). The search was cut short, whatever it had found.
  2. **INSUFFICIENT EVIDENCE:** no experiment ran, or the best bundle's unmet
     criteria are all inconclusive, unsupported or missing (none failed).
  3. **NO IMPROVEMENT FOUND:** no tested bundle meets more latency-ceiling
     and error-ceiling criteria than naive does on the same cells.
  4. **CRITERIA UNMET:** otherwise. Something improved; some criterion failed.
- **Freeze.** `candidate.freeze` writes the document once and pins it; the
  loop log records the freeze. A refusal's id is derived from its content.
- **Metric.** The refusal class is recorded on the loop log's `frozen` row and
  in the frozen document; Phase 6 emits the count by class.
- **Fixtures.** `fixtures/phase4/qualifying.json` must produce the 1 CU
  bundle; `fixtures/phase4/refusal-criteria-unmet.json` must refuse as
  CRITERIA UNMET naming s07. Tests also cover the other three classes.

## Consequences

- A qualifying bundle found before a budget stop is still recommended: a
  budget stop only decides the class when nothing qualified.
- Inconclusive counts as not met, so a noisy or thin experiment can produce a
  refusal. That is the honest reading of the frozen rules.
