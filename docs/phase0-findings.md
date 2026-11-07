# Phase 0 findings

RC1-628, 2026-11-06. What the noise gate run
([workflow run 42314904088](https://github.com/snacksnack/poc-benchmark-agent/actions/runs/42314904088))
taught, beyond the verdict in `docs/noise-gate.md`. Per-run data:
`results/phase0/runs/*/summary.json`.

## 1. Gate: GO, and it holds per shape

By the declared rule (mix-level p95 and p99 run-to-run spread below 20%), the
gate passed: p95 15.1%, p99 1.2%, every run above 15,000 samples, no errors.

The rule turned out to be measured on the wrong unit (finding 2), so the same
check was repeated per query shape. Every per-shape p95 and p99 spread is
also below 20%:

| Shape | Share of mix | Samples per run | p95 spread | p99 spread |
| --- | ---: | ---: | ---: | ---: |
| latest_events | 40% | ~6,150 | 8.6% | 5.1% |
| account_contacts | 25% | ~3,830 | 3.6% | 5.7% |
| email_lookup | 20% | ~3,040 | 6.6% | 6.8% |
| campaign_funnel | 10% | ~1,520 | 1.6% | 0.9% |
| deep_page | 5% | ~740 | 11.6% | 19.7% |

`deep_page` sits at the edge with about 740 samples per run, far below the
10,000-sample floor for a stable p99. Rare shapes cannot support p99 claims
inside a 300-second window.

## 2. Mix-level percentiles are the wrong unit for criteria

The mix is bimodal: about 90% index-backed lookups in single-digit
milliseconds and 10% campaign-funnel scans at one to three seconds. The
mix-level p95 lands inside the slow population and tracks the funnel's
*median*: per run, mix p95 was 1,152 / 1,155 / 1,006 / 1,074 / 993 ms against
funnel p50 of 1,153 / 1,172 / 1,007 / 1,136 / 995 ms. That is where the 15.1%
p95 spread came from.

The same effect hides real improvements. Indexes took `latest_events` from a
p50 of 1,243 ms to 4.3 ms and raised throughput from 9.4 to 51 queries per
second, yet mix-level p99 moved only from 2,713 to 2,479 ms, because the tail
belongs to the shape the indexes did not touch.

**Carry into Phase 2:** criteria are stated per shape (or per declared shape
class), with a per-shape sample floor. A shape too rare to reach the floor
gets a p95 criterion or more repetitions, never a p99 claim. A mix-level
number may be reported, never scored.

## 3. Shared compute was not noisier, and was faster on the tail

ADR 0015 excluded 0.25 and 0.5 CU because shared compute adds noise the
experiment cannot control. In this run the 0.25 CU series (confirmed by the
server: `max_connections` 112, `effective_cache_size` 819 MB) had a p99
spread of 0.2%, and its tail was faster than 1 CU: funnel p99 2,306 ms
against 2,711-2,735 ms.

One 18-minute sample cannot show that neighbor noise never happens, but it
does remove the evidence for the exclusion. It also shows why excluding small
tiers is costly: "the smaller, cheaper tier is as fast on your tail" is
exactly the kind of answer this agent exists to give. **Decision:** 0.25 and
0.5 CU are back in the tier list, labeled shared (ADR 0015).

**Open question for Phase 1:** why the funnel scan is slower on 1 CU than on
0.25 CU. Candidate explanations, none verified: parallel workers per query
scale with compute size and oversubscribe the CPU at 8 sessions; CPU quota
behavior differs between shared and dedicated computes. Phase 1 checks
`EXPLAIN (ANALYZE)` worker counts per tier before any tier result is trusted.

## 4. Risks checked by the run

- **R6 (stats wiped by suspend):** `stats_reset` was unchanged across every
  run. Running the gate back to back kept the Free-plan compute awake.
- **R9 (runner network noise):** the `SELECT 1` probe ran at p50 3-4 ms, p99
  10-15 ms, against a 2.5 s workload tail. Network jitter is not what this
  tail measures. One runner, one job: cross-job variance is still untested.
- **R10 (optimistic gate):** confirmed as expected. Neon's file cache hit
  ratio was 99.9% or higher in every run; the spike dataset never left
  memory. The Phase 1 dataset will.

## 5. Pipeline

End to end on Neon from a GitHub runner: three variant branches created by
API, indexes built (7.6 s), nine measured runs, cache evidence recorded
before and after each, all three branches deleted. Exit criterion met.
