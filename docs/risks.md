# Risk log

Seeded 2026-11-06 from the RC1-595 epic (R1-R8); R9-R11 added by the Phase 0
spike. Reviewed at every phase exit: each review adds a dated line under
"Reviews" and updates the status column. Likelihood and impact are
low / medium / high.

| ID | Risk | Likelihood | Impact | Mitigation | Status |
| --- | --- | --- | --- | --- | --- |
| R1 | Neon tail noise swallows the effect | medium | high | Phase 0 noise gate decides go or fallback (ADR 0009); in the fallback, latency moves to local Postgres and results are reported per platform (ADR 0007) | open: gate pending |
| R2 | Cost overrun from the enlarged matrix | medium | low | Project consumption limit; scale-to-zero disabled per session only and restored in a `finally`; Phase 0 re-estimate from the full matrix (docs/cost-estimate.md: about $35 expected, $40 worst case, over about two weeks) | open: re-estimated, limit set at project creation |
| R3 | Held-out leakage into the loop | low | high | Enforced in Python; candidate hash frozen before any held-out run; the dev entry point reads the profiling slice only; a planted-leak test (ADR 0010) | open: Phase 3 builds it |
| R4 | Synthetic workload written by the person tuning for it | high | medium | Nastiness list committed before any experiment; held-out slice; disclosed on the card | open: Phase 1 commits the list |
| R5 | Pageserver cache tier unobservable from the client | high | medium | Cache states labeled by procedure; block counters and the Neon file-cache view recorded before and after every run; disclosed (ADR 0013) | mitigated in the load generator |
| R6 | Statistics wiped by compute suspend | medium | medium | Keep the compute awake through a session; `stats_reset` recorded in every run's cache evidence, so a wipe is detected rather than inferred | partly mitigated: Free cannot disable scale-to-zero, so the gate runs back to back |
| R7 | Reads as DBA tuning | medium | medium | Engagement framing: criteria agreed with a buyer first, results reported to that buyer after, losses included; sizing not GUC language (ADR 0001) | open |
| R8 | Index storage on branches | low | low | Reported as a cost line (upper bound $0.43-0.87 for the build) | mitigated in the cost model |
| R9 | Load-generator network noise: GitHub-hosted runners vary in region and hardware between jobs | medium | high | `SELECT 1` probe on its own connection beside every run; baselines interleaved inside a job; runner metadata recorded. If the probe shows the network dominates the tail, move to a fixed-location runner (ADR 0014) | open: the gate measures it |
| R10 | The noise gate is optimistic: the spike dataset is cache-resident at 1 CU, the Phase 1 dataset is not | high | medium | A failing gate is decisive; a passing gate is necessary, not sufficient. Real uncertainty is measured alongside Phase 4 experiments from interleaved baselines (ADR 0009). A non-gating 0.25 CU series shows the pageserver-bound spread | accepted |
| R11 | Shared-compute noise at 0.25 and 0.5 CU | high | medium | Excluded from the v1 tier list (ADR 0015); measured once in the gate's supplementary series for the record | mitigated by scope |

## Reviews

- 2026-11-06, Phase 0 seed. R1-R8 from the epic. R9-R11 added. R2 re-estimated.
