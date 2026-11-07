# ADR 0018: A tail estimate needs 100 observations and 10 distinct instances beyond it

Status: accepted
Date: 2026-11-06
Source: RC1-629 Phase 1

## Context

Phase 0 showed rare shapes cannot reach the 10,000-sample p99 floor in a
300-second run (docs/phase0-findings.md, finding 1), and Phase 2 criteria are
per shape (R12). The load model's p99 floor was a single number with no rule
behind it, and nothing set a floor for p95 or for how many different query
instances a tail is drawn from. Replay draws instances with replacement, so
10,000 observations drawn from 200 instances is really a p99 over 200
parameter values.

## Decision

One rule, in load-model.toml [report]: a percentile is supported when at
least 100 observations, from at least 10 distinct instances, lie beyond it.

| Percentile | Observations | Distinct instances |
| --- | ---: | ---: |
| p95 | 2,000 | 200 |
| p99 | 10,000 | 1,000 |

The existing `min_samples_for_p99 = 10000` is kept and validated against the
rule. Below either floor the percentile is still reported, marked
unsupported, never dropped. The profile applies the observation floor to the
capture; the split manifest applies the distinct floor to each slice.

## Consequences

- Phase 2 can only write a p99 criterion for a shape whose held-out slice
  has at least 1,000 distinct instances and whose run reaches 10,000
  observations; otherwise it writes a p95 criterion or adds repetitions.
- The floor is a convention, not a confidence interval. It is stated, so a
  reader can disagree with it.
