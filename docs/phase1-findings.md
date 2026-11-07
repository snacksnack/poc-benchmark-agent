# Phase 1 findings

RC1-629, 2026-11-07. What the corpus load, profile and tier probe taught,
beyond the numbers in `docs/phase1-profile.md`. Raw results:
`results/phase1/`.

## 1. Exit: profile, nastiness list, split manifest

- **Profile:** 140,154 log rows (the profiling slice of a 200,000-row log),
  9 shapes, 10.98 GB dataset of 56.0 million rows. Dataset to compute cache
  runs from 12.8x at 0.25 CU to 0.8x at 4 CU: the corpus fits in cache only
  at the top tier.
- **Nastiness list:** `docs/nastiness.md`, committed alone (4c58100) before
  any Neon run.
- **Split manifest:** `results/phase1/split-manifest.json`. 600 of 2,000
  accounts held out, 60 per size decile, every shape's held-out share within
  0.6 points of 30%.
- The profiler recovered all 9 designed shapes from the SQL alone, none split
  and none merged (checked against the ground truth after profiling).

## 2. The profile reproduces

The profile ran twice on fresh branches (the first run's pg_stat_statements
figures had a bug, fixed in this PR). The query log and the split matched
byte for byte. Captures agreed closely: 9,340 against 9,299 observations,
1,103 against 1,071 failures, every shape's share of capture time within
about one point. Only the fast shapes' client p50 moved, by the change in
network round trip (finding 4).

## 3. Which traps bit

| Trap | Seen in the profile |
| --- | --- |
| Q2 expression index | Yes. `lower(email)` scans 6M contacts (about 1.5 s server time) on every call; the planner estimates 30,000 rows against 1. 57% of server time. |
| Q3 index that looks useful | Yes. The planner picks naive's `events(occurred_at)` for the account timeline; 30% of those calls time out. |
| Q6 correlated predicates | Yes. For a mismatched region and country the planner expects about 40,000 rows and finds none; matching pairs are underestimated 2.5-5x. About 16% of segment calls time out, close to the 20% mismatch rate. |
| Q7 skewed aggregate | Yes. The planner estimates every non-hot campaign at 3,106 rows; actual counts run from 438 to 17,467. |
| Q9 time-window report | Yes, under load. Alone it takes about 750 ms; at 8 sessions 91% of calls time out. |
| Q1 N+1 | Partly. Naive's `events(contact_id)` keeps it at about 68 ms server time; it is 40% of calls but 7% of server time. |
| Q5 useless-looking index | Naive builds a full `email_status` btree, which the planner uses; the partial index's advantage is size (224 KiB against 40 MiB), not speed. |
| Q8 large sort | **No.** The largest sampled sort stayed in memory (776 kB, quicksort). The sample that would have spilled timed out under EXPLAIN. Reported, not dropped. |

## 4. Runner network round trip varies by job (R9 realized)

The `SELECT 1` probe ran at p50 3-4 ms in Phase 0, then 39 ms and 66 ms in
the two Phase 1 profile jobs. GitHub assigns a different machine per job,
sometimes far from aws-us-east-1. At 66 ms a 2 ms query reads as 67 ms on the
client. The profile reports pg_stat_statements server time beside client
time for this reason.

**Carry into Phase 3:** a scored job records its probe and is rejected and
re-run when the probe p50 exceeds a fixed bound (or runs from a fixed
location in us-east-1). Configurations are compared only within one job;
cross-job comparisons of client latency are invalid.

## 5. The naive baseline is overloaded at 2 CU

At 8 sessions the capture failed 11.5% of queries (timeouts), against a 1%
error ceiling, at 7.7 queries per second. Naive therefore cannot qualify at
8 sessions on 2 CU, and the PK-only default will be worse.

**Carry into Phase 2:** criteria have to stay meaningful when a baseline
fails outright: the failure fraction is itself a criterion, and "p99 from X
to Y" needs X defined when X is the timeout.

## 6. Tier probe: worker counts do not explain Phase 0's gap

Phase 0 saw the funnel scan slower at 1 CU than at 0.25 CU. Parallel
settings are identical at every tier: 2 workers per gather, 8 in total,
`work_mem` 4 MB. At 8 concurrent sessions the 8-worker pool runs out and
about half the queries launch no workers, at every tier. On the spike
dataset, 0.25, 0.5 and 1 CU run the scan at the same speed (about 200 ms
alone, 850 ms at 8 sessions); 2 CU and 4 CU are faster. Unverified
explanation: shared computes are not CPU-capped at their nominal size for
short bursts. The file cache limit is 80% of RAM (819 MB per 0.25 CU), not
the assumed 75%.

## 7. Small parameter spaces cannot support a tail

Three shapes fall under the distinct-instance floor (ADR 0018) in the
held-out slice because their parameter space is small: `segment_preview`
has 80 region and country pairs, `daily_report` 365 days, `bounced_contacts`
one instance per account. They are flagged, not dropped. Phase 2 writes p95
or no tail criterion for them.

## 8. Measured sizes replace the estimates

Events are 182 heap bytes per row (estimate 160), contacts 102 (200). The 8
candidate indexes measure 3.9 GB (I1 and I2 1.5 GB each, I4 BRIN 304 KiB, I6
partial 224 KiB). `config-space.toml` now carries `measured_gb` and
`measured_bytes`; the cost estimate holds at $32 expected, $40 worst case.
