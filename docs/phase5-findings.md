# Phase 5 findings

RC1-633, 2026-11-08. What generating the results document from the Phase 4
run showed. Documents: `docs/results.md` (real run) and
`docs/fixtures/results-{losing,under-sampled,refusal}.md`. Design: ADR 0025.

## 1. Exit

- **Real-run document:** `docs/results.md`, refusal `refusal-4e24a549455d`,
  CRITERIA UNMET. Generated from verified inputs only: criteria hash, frozen
  hash, the 120-row evaluation record's hash chain, the frozen pin on every
  row, and the committed verdicts re-derived exactly from the pooled cells.
- **Fixture documents:** losing (a 40x held-out regression on s04 leads),
  under-sampled (8 distinct instances beyond s02's p99 leads), refusal (the
  refusal and the best tested bundle lead; the bundle is labeled "fails to
  qualify" on every line it appears). A clean-win control leads with the
  headline. All four are tests.
- **Platform join:** a test splits fixture cells across Neon and local
  Postgres; every comparison across them raises `PlatformJoinError`, and the
  rendered document has two summaries and two sections, neither headline
  naming the other platform.
- **Freshness:** tests fail if any committed document differs from what the
  code renders.

## 2. Every evaluation failure was a timeout

79,183 of 229,434 evaluation observations (both slices) failed, all of them
timeouts, 0 errors. The run records keep errors and timeouts apart; the
pooled cells did not. The document now shows the split per
shape, and `harness.pool` emits it for later runs.

## 3. Today has no usable "before" number at the objective

At 32 sessions, warm, held-out, today (naive, 2 CU) times out on 97% of s07,
77% of s02, 67% of s05 and 36% of s03. The survivors' tails have 2 to 18
observations beyond the percentile against a floor of 100. The epic's
headline form, "cut p99 from X to Y", has no supported X in this run. The
document prints those figures with "under-sampled" and "latency of the
survivors only" beside each one instead of a clean number.

## 4. Development verdicts predicted today well; development intervals did not

Today's baseline, predicted from the development pool (profiling slice, 2
repetitions, projected support) and scored on held-out: verdicts agree on 71
of 80 criteria, but the held-out value fell inside the predicted 95% interval
on only 14 of 30 latency criteria. 12 of the 16 misses were faster on
held-out. The interval is an order-statistic interval over the pooled
observations; it does not include variation between jobs or between slices.
Verdicts survived because most values sit far from their bounds. A candidate
close to a bound would get a predicted confidence the interval cannot back.
Recorded as R18; Phase 6 should report interval coverage as an eval metric.

## 5. Not done here

- The rejection message still blames the network for probe sample-floor
  misses (Phase 4 follow-up, unchanged).
- The fallback platform has never run; only its document shape is tested.
