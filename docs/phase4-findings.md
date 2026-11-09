# Phase 4 findings

RC1-632, 2026-11-08. What the first live run of the adaptive loop and the
held-out evaluation showed. State: `results/phase4/` (loop log, dev record,
eval record, pooled cells, verdicts, jobs) and `candidates/frozen.json`.
Jobs: loop 42412888558; evaluation 42429705933, 42451338079, 42473007580;
report 42484111479.

## 1. Exit

- **Output: refusal `refusal-4e24a549455d`, class CRITERIA UNMET**, frozen
  as `candidates/frozen.json` (sha256 09e832e8...). It names the shapes
  unmet by the best tested bundle (s03-s09) and states "no tested candidate
  within the permitted space and budget met the success criteria".
- **Loop log:** 2 experiments, 1 aborted attempt, 4 proposals, a proposer
  stop and the freeze, hash-chained under the criteria and config-space pins.
- **Held-out evaluation complete:** 120 runs, 120 scored, 0 rejected. Both
  baselines, levels 1/8/32, both cache states, 5 repetitions, interleaved, on
  both slices. Every row carries the refusal's sha256.
- **Gates:** each loop job ran `harness-eval` before the loop and it refused
  (no freeze yet); after the freeze it reports the refusal verified. The
  planted illegal tier (`cu: 3`), the out-of-scope parameter (`work_mem`),
  the planted held-out read and both fixtures (qualifying candidate; refusal
  with class CRITERIA UNMET) are tests that pass in CI.
- **Spend:** 2 of 20 experiments; 19.8 CU-h counted by the loop against its
  120 CU-h budget; model $0.60 of $10. All Phase 4 compute: 71.9 CU-h held
  across the recorded jobs plus about 2.5 CU-h from a job whose state was
  lost (section 4), about $7.90 at $0.106/CU-h.

## 2. The refusal is earned, and traceable to a planted trap

| Bundle | Met | Failed | Inconclusive | Unsupported | Compute |
|---|---|---|---|---|---|
| I1-I8 at 2 CU | 43 | 15 | 8 | 14 | 7.0 CU-h |
| I1-I8 at 4 CU (strongest permitted) | 49 | 14 | 11 | 6 | 10.6 CU-h |

At 4 CU with every candidate index, the 1% error ceiling still fails:
s07 15-28% at 32 sessions, s08 4-20%, s09 21-24%. s07's warm p95 at 32
sessions is 4,156 ms against 2,000, with measured support. Claude stopped:
a lower tier has less CPU and cache, and removing an index cannot add an
access path.

I checked the stop rather than trust it. Every failure is a server statement
timeout (SQLSTATE 57014, at 5 s), not an error the harness made. s08's
timeouts are determined by the query instance: 64 of its 422 instances time
out every time, 358 never. That is the Phase 1 large-sort trap (s08 orders a
filtered contact segment by id). Fixing it needs `work_mem` or an index
outside the candidate list, both excluded from the permitted space by design.

> **Correction, 2026-11-09 (RC1-635).** s08 is not the large-sort trap.
> s08 is `segment_preview`, trap Q6 in `docs/nastiness.md` (correlated
> predicates): a mismatched country and region makes the planner expect tens
> of thousands of matches and walk the primary key over the whole table.
> Phase 1 measured about 16% of segment calls timing out against a 20%
> mismatch rate. The fix would be extended statistics, which is also outside
> the permitted space. The large sort, Q8, is s09. The refusal is unchanged.

The loop never tried 4 CU minus one index. The statement says "no tested
candidate", which is exactly what the evidence supports.

## 3. Held-out agrees with profiling

| Slice | Default met / failed | Naive met / failed |
|---|---|---|
| held_out | 8 / 41 | 33 / 22 |
| profiling | 7 / 41 | 30 / 23 |

The failing shapes are the same on both slices (naive: error ceilings on
s02, s03, s05, s07, s08, s09; profiling adds s06). The profiling slice the
loop learned on is representative of the held-out accounts.

## 4. Four first-run defects, all fixed on this branch

1. **LoopConfig wiring.** `may_start` was passed by position into
   `support_scale`; the first job failed before any experiment (about 5
   minutes of compute). The chain did not re-dispatch after the failure.
2. **State commit staged nothing.** `git add results/phase4 candidates` with
   no `candidates/` yet aborts the whole add, and `2>/dev/null || true` hid
   it. One job's loop log was lost (about 2.5 CU-h outside the loop's
   count). The step now stages only existing paths, and the chain refuses to
   dispatch unless the commit step finished.
3. **"Network" rejections were the load generator.** Two jobs stopped on the
   probe sample floor at the naive 32-session post-restart cell (255-258 of
   300 samples, latency healthy). The measured window opened at a fixed time
   while 33 processes were still connecting to the just-restarted compute:
   first probe sample at 42 s, last session at 41 s. Processes now connect
   first and the clock starts when all report (cap 120 s). After the fix,
   all 60 post-restart evaluation runs took all 300 probe samples. The
   rejection message still blames the network (ADR 0014); it should name the
   probe sample floor separately.
4. **Restart over a running job.** I re-dispatched while a chain job on the
   old code was still running; I cancelled it after 4 minutes (0.23 CU-h),
   before it could dispatch a second chain.

## 5. Thin development evidence (R16) is real

Even at 4 CU, s05 and s03 p99 stayed unsupported at 8 sessions (17-33
observations beyond, floor 100) after the x2.5 projection, and the noise
rule marked 11 no-regression criteria inconclusive (s08 baseline spread up
to 442%). Here it did not change the outcome: error-ceiling failures alone
refuse. A run that comes close will need more development repetitions on
the shapes near their floors before the loop can tell a qualifying bundle
from one that only lacks evidence.
