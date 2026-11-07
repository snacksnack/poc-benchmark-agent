# Phase 3 findings

RC1-631, 2026-11-07. What running both baselines through the harness taught,
beyond the numbers in `docs/phase3-baselines.md`. Raw results:
`results/phase3/` (record, job, pooled cells). Run:
https://github.com/snacksnack/poc-benchmark-agent/actions/runs/42388461508.

## 1. Exit

- **Coverage:** default (PK only) and naive (10 indexes), 2 CU, levels 1, 8
  and 32, warm and post-restart-with-eviction, 2 repetitions in a seeded
  interleaved order: 24 runs, 24 scored, 0 rejected. The record verifies
  (25 lines, chain intact) and every row carries the criteria, load model,
  config space and configuration hashes, slice, cache state, level, platform
  and the SHA-256 of each raw log.
- **Evaluation refuses without a frozen hash:** the job's gate step ran
  `pocbench harness-eval` and got "evaluation refuses to run: no frozen
  candidate hash or refusal record". Tests cover a wrong pin, other criteria,
  a planted held-out item and the static rule that criteria come only through
  `criteria.load_verified()`.
- **No dev aid writes the record:** `harness-try` never opens it, and the
  record refuses rows whose window, timeout, grace, warm-up, load model hash
  or slice differ from a scored run's.
- **Cost, four separate lines:** benchmark execution $1.07 (10.05 CU-h held
  over 2.5 hours); index build $0.00 (default) and $0.01 (naive, 84 s);
  index storage $0.00 and $0.99 per month; projected operating cost $158.60
  and $159.59 per month. Cost per successful query is per cell.

## 2. The network gate worked both ways

The first job (42387934432) was rejected at preflight: p50 69 ms, "p99"
139 ms. The preflight took 60 samples, so its p99 was the single slowest
round trip. The preflight now takes as many round trips as a run's probe
(300). The second job's runner was close (preflight p50 18.6 ms, p99 - p50
0.4 ms) and every run passed: probe p50 18.2-25.9 ms, p99 - p50 1.9-12.2 ms.
The rejected job spent 0.07 CU-h and built nothing. R9 is now enforced, not
just recorded.

## 3. Both baselines fail the criteria, as R13 predicted

| Config | Level | Shapes over the 1% error ceiling (warm) | Successful qps (warm) |
| --- | ---: | ---: | ---: |
| default | 8 | 7 of 9 | 0.9 |
| default | 32 | 8 of 9 | 2.2 |
| naive | 8 | 5 of 9 | 6.9 |
| naive | 32 | 6 of 9 | 12.7 |

Default times out on every call of five shapes (s01, s03, s05, s07, s09),
so it has no latency to report there. Naive is 7-8x the throughput at the
same 2 CU for $1 per month of index storage. Projected monthly cost is
dominated by always-on compute ($154.76 of either config); storage is the
small line. The first comparison a candidate has to win is naive, which is
what the criteria name.

## 4. The baselines cannot support a tail, and that is a speed problem

Of 64 per-shape cells at levels 8 and 32, only one p95 is supported (naive,
s01, level 32: 4,114 observations) and no p99. ADR 0018 needs 2,000
observations for a p95 and 10,000 for a p99. The criteria count an
unsupported tail in a ceiling as not met and fall back to the highest
percentile supported on both sides for no-regression (ADR 0019), so the
baselines lose on support as well as on latency.

In a closed loop, observations scale with speed. Naive's s02 got 754
observations at level 8 because calls take 1.4 s; at 50 ms the same window
gives more than ten times as many, enough for a p99 over the evaluation protocol's
5 repetitions. A candidate earns tail support by being fast. A slow
candidate cannot reach a supported tail, so its verdict is not met.

## 5. Post-restart-with-eviction measures the same as warm

Every post-restart run did restart (server start time moved each time). The
filler scan then read about 9 GB (1.1 million file-cache misses, 2.2 million
writes) and filled the file cache. The eviction worked. Over the 5-minute
window the runs still look warm:

| Config | Level | File-cache hit ratio, warm | Post-restart |
| --- | ---: | --- | --- |
| default | 8 | 67.8%, 70.0% | 64.7%, 64.2% |
| default | 32 | 90.1%, 89.6% | 89.2%, 88.8% |
| naive | 8 | 92.9%, 93.1% | 92.9%, 93.0% |
| naive | 32 | 97.4%, 97.2% | 97.3%, 97.3% |

Only default at level 8 is a few points colder, and its latency did not move with it. Median latency in the first 30 seconds after eviction matched the rest of the
window and the warm runs, within noise. Naive's hot set is small index pages
that come back within seconds. Default's scans miss the cache in both states.
Restart cost is real but outside the window: the restart API took 9.5-18 s
(one 43 s outlier), and the first query after it 19-26 ms.

For these baselines the two cache states will not separate. Nothing in the
frozen criteria changes; criteria at both states stay. Phase 4 should expect
post-restart verdicts to track warm ones and report that, not present them as
an independent result. A cold measurement that matters would need a shorter
window starting at the restart, which is a new cache state and so a new
freeze.

## 6. Runtime and limits

- The job took 2.5 hours for 24 runs (about 6 minutes per run including
  restarts, filler scans and probes), plus 84 s of index builds and 33 s
  of filler per branch. The evaluation protocol (3 levels x 2
  states x 5 repetitions x 2 baselines, plus the candidate) is 90 runs:
  about 9 hours, past the 6-hour job limit. Phase 4 needs to split
  evaluation across jobs, appending to one committed record under the same
  pins (ADR 0020 already allows this).
- 32 load sessions on a 4-vCPU runner did not show as probe jitter: the
  worst run's p99 - p50 was 12.2 ms, under the 15 ms bound but not by much.
  A heavier candidate mix could cross it.
