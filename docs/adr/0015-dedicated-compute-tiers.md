# ADR 0015: Tiers in scope are 1, 2 and 4 CU fixed size; shared-compute sizes excluded

Status: reopened 2026-11-06 by the Phase 0 noise gate (see Amendment)
Date: 2026-11-06
Source: RC1-628 Phase 0

## Context

The epic sized the dataset against 0.25 to 4 CU. Neon documents 0.25 and 0.5 CU as shared compute (neon.com/docs/manage/computes, read 2026-11-06): a noise source from other tenants that the experiment cannot control and that changes independently of the configuration under test.

## Decision

v1 tiers are 1, 2 and 4 CU, fixed size (autoscaling min = max). The default tier, where both baselines run, is 2 CU. The dataset is about 10.5 GB against 3, 6 and 12 GB of compute cache, which still spans "working set does not fit" to "fits". The Phase 0 gate also runs a non-gating 0.25 CU series to record how noisy shared compute actually is.

## Consequences

- The epic's "0.25-4 CU" line is superseded; the epic is updated to match.
- A prospect running on 0.25 or 0.5 CU is outside the v1 space; the results document says so rather than extrapolating.

## Amendment, 2026-11-06 (noise gate)

The noise rationale above was not supported by the data. The gate's 0.25 CU
series had a p99 run-to-run spread of 0.2%, lower than 1 CU's 1.2%, and its
tail was faster (campaign-funnel p99 2,306 ms against 2,711-2,735 ms at 1 CU).
One 18-minute sample does not prove neighbor noise never occurs, but the
exclusion now rests on an unmeasured risk, and it would hide the answer "the
smaller tier is as fast on your tail", which is one this agent should be able
to give. Decision on whether to re-include 0.25 and 0.5 CU is pending.
Details: docs/phase0-findings.md, finding 3.
