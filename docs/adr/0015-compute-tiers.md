# ADR 0015: Compute tiers are 0.25, 0.5, 1, 2 and 4 CU, fixed size

Status: accepted (revised the same day by the Phase 0 noise gate; history below)
Date: 2026-11-06
Source: RC1-628 Phase 0

## Context

The epic sized the dataset against 0.25 to 4 CU. Neon documents 0.25 and 0.5
CU as shared compute (neon.com/docs/manage/computes, read 2026-11-06). Phase 0
first excluded them on the grounds that shared compute adds noise from other
tenants that the experiment cannot control.

The noise gate then measured it. The 0.25 CU series had a p99 run-to-run
spread of 0.2%, lower than 1 CU's 1.2%, and its tail was faster
(campaign-funnel p99 2,306 ms against 2,711-2,735 ms at 1 CU). Details:
docs/phase0-findings.md, finding 3.

## Decision

v1 tiers are 0.25, 0.5, 1, 2 and 4 CU, fixed size (autoscaling min = max).
The default tier, where both baselines run, stays 2 CU. The dataset is about
10.5 GB against 0.75, 1.5, 3, 6 and 12 GB of compute cache, spanning "14x the
cache" to "fits".

Shared-compute noise is handled the way all noise is: measured from
interleaved baselines and randomized order in Phase 4 (ADR 0009), not assumed
away by exclusion. Every result carries its tier's shared or dedicated label.

## Consequences

- The tool can give the answer "the smaller, cheaper tier is enough", which
  the exclusion would have hidden.
- One 18-minute sample does not show neighbor noise never happens. If Phase 4
  interleaved baselines on a shared tier spread wider than the smallest pass
  effect, that tier's results are reported as inconclusive rather than dropped.
- Phase 1 explains why the funnel scan ran slower on 1 CU than on 0.25 CU
  (`EXPLAIN (ANALYZE)` worker counts per tier) before any tier result is
  trusted.

## History

- 2026-11-06, first version: tiers 1, 2, 4 CU; 0.25 and 0.5 excluded as
  shared compute. Reopened by the noise gate the same day; revised to this
  version on Reid's call after reading the gate results.
