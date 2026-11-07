# Risk log

Seeded 2026-11-06 from the RC1-595 epic (R1-R8); R9-R11 added by the Phase 0
spike. Reviewed at every phase exit: each review adds a dated line under
"Reviews" and updates the status column. Likelihood and impact are
low / medium / high.

| ID | Risk | Likelihood | Impact | Mitigation | Status |
| --- | --- | --- | --- | --- | --- |
| R1 | Neon tail noise swallows the effect | medium | high | Phase 0 noise gate decides go or fallback (ADR 0009); in the fallback, latency moves to local Postgres and results are reported per platform (ADR 0007) | gate GO 2026-11-06 (p99 spread 1.2%, p95 15.1%; per shape all below 20%); re-checked by interleaved baselines in Phase 4 |
| R2 | Cost overrun from the enlarged matrix | medium | low | Project consumption limit; scale-to-zero disabled per session only and restored in a `finally`; Phase 0 re-estimate from the full matrix (docs/cost-estimate.md: about $32 expected, $40 worst case, over about two weeks) | open: re-estimated, limit set at project creation |
| R3 | Held-out leakage into the loop | low | high | Enforced in Python; candidate hash frozen before any held-out run; the dev entry point reads the profiling slice only; a planted-leak test (ADR 0010) | open: Phase 3 builds it. Phase 1 split is account-level, committed with an assignment hash |
| R4 | Synthetic workload written by the person tuning for it | high | medium | Nastiness list committed before any experiment; held-out slice; disclosed on the card | mitigated in Phase 1: list committed alone (4c58100) before any run; held-out split committed; one trap (Q8) did not bite and is reported. Card disclosure is Phase 7 |
| R5 | Pageserver cache tier unobservable from the client | high | medium | Cache states labeled by procedure; block counters and the Neon file-cache view recorded before and after every run; disclosed (ADR 0013) | mitigated in the load generator |
| R6 | Statistics wiped by compute suspend | medium | medium | Keep the compute awake through a session; `stats_reset` recorded in every run's cache evidence, so a wipe is detected rather than inferred | mitigated: Phase 1 capture disabled scale-to-zero per endpoint; server start predated the pg_stat_statements reset and no clock moved during the 21-minute run (checked in code, both runs valid) |
| R7 | Reads as DBA tuning | medium | medium | Engagement framing: criteria agreed with a buyer first, results reported to that buyer after, losses included; sizing not GUC language (ADR 0001) | open |
| R8 | Index storage on branches | low | low | Reported as a cost line (upper bound $0.43-0.87 for the build) | mitigated in the cost model |
| R9 | Load-generator network noise: GitHub-hosted runners vary in region and hardware between jobs | high | high | `SELECT 1` probe on its own connection beside every run; baselines interleaved inside a job; runner metadata recorded; server time (pg_stat_statements) reported beside client time. Phase 3 rejects and re-runs a scored job whose probe p50 exceeds a fixed bound, or moves to a fixed-location runner (ADR 0014) | realized in Phase 1: probe p50 3-4 ms (Phase 0), 39 ms and 66 ms (Phase 1 jobs). Cross-job client latency is not comparable (docs/phase1-findings.md, finding 4) |
| R10 | The noise gate is optimistic: the spike dataset is cache-resident at 1 CU, the Phase 1 dataset is not | high | medium | A failing gate is decisive; a passing gate is necessary, not sufficient. Real uncertainty is measured alongside Phase 4 experiments from interleaved baselines (ADR 0009). A non-gating 0.25 CU series shows the pageserver-bound spread | accepted; measured in Phase 1: dataset is 12.8x the cache at 0.25 CU, 0.8x at 4 CU; capture file-cache hit ratio 94% at 2 CU |
| R11 | Shared-compute noise at 0.25 and 0.5 CU | low | medium | Measured in the gate: 0.25 CU p99 spread 0.2%, lower than 1 CU. Shared tiers kept in scope and labeled; a shared tier whose interleaved baselines spread wider than the smallest pass effect is reported inconclusive (ADR 0015) | open: Phase 4 measures it |
| R12 | Mix-level percentiles mislead: in a bimodal mix the p95 tracks the slow shape's median, and the tail hides improvements to every other shape | high | high | Phase 2 criteria are per shape with a per-shape sample floor; mix-level numbers reported, never scored (docs/phase0-findings.md, finding 2) | open: Phase 2 |
| R13 | Baselines fail outright: at 10 GB the naive baseline fails 11.5% of queries at 8 sessions on 2 CU (ceiling 1%), and PK-only will be worse, so "p99 from X to Y" has no X | high | medium | Phase 2 makes the failure fraction a criterion and defines X when the baseline times out; the profile counts a timeout at its full timeout | open: Phase 2 |

## Reviews

- 2026-11-06, Phase 0 seed. R1-R8 from the epic. R9-R11 added. R2 re-estimated.
- 2026-11-06, Phase 0 exit. Gate GO (R1). R6 and R9 checked in the run. R11 downgraded; 0.25 and 0.5 CU back in scope (ADR 0015). R12 added.
- 2026-11-07, Phase 1 exit. R4 and R6 mitigated. R9 realized (39 and 66 ms probe on two jobs); likelihood raised to high, Phase 3 gate added. R10 measured. R13 added.
