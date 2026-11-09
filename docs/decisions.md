# Phase 0 decisions

RC1-628, 2026-11-06. This is the readable whole; each choice points to the
file that enforces it and the ADR that holds the reasoning. The machine-read
sources of truth are `config/load-model.toml` and `config/config-space.toml`.
`uv run pocbench fingerprint` prints their hashes.

## Platform

- Neon Postgres 18 in `aws-us-east-1`, in a new organization, separate from
  the one that bills `esp-review-gate`, so moving to Launch never bills that
  project. Phase 0 runs on Free; Phase 1 upgrades the new org to Launch.
- Sizing and cost, not GUC tuning (ADR 0001).
- Every configuration variant is a child branch of the dataset branch with
  its own fixed-size compute (`pocbench.neon.Neon.create_variant`), and is
  deleted by the same code path that created it.
- Project consumption limit set when the project is created. Scale-to-zero is
  disabled per session only, restored in a `finally`, never left as a project
  setting (`Neon.set_suspend_timeout`). Free cannot disable it at all, so the
  gate's runs go back to back to keep the compute awake (R6).
- History window: 1 day on Launch, the shortest that keeps a same-day undo.

## Configuration space (ADR 0003, ADR 0015)

**Compute tiers:** 0.25, 0.5, 1, 2, 4 CU fixed size. Default tier for both
baselines: 2 CU. 0.25 and 0.5 CU are shared compute; first excluded, then
kept after the noise gate measured them steady, and labeled shared in every
result (ADR 0015).

**Dataset** (Phase 1 builds it): about 10.5 GB, 6M contacts and 50M events,
against 0.75 / 1.5 / 3 / 6 / 12 GB of compute cache. Sized by ratio to cache, not in
absolute GB.

**Index candidates**, with estimated storage. Estimates are upper bounds from
`pocbench.sizing` with no btree deduplication credit, so I3, I5 and I8 (whose
leading keys repeat) will measure smaller. Phase 1 replaces every figure with
`pg_relation_size`. Each built index is diverged pages on its branch and is
billed storage (R8).

| ID | Index | Serves | Est. size |
| --- | --- | --- | ---: |
| I1 | `events (contact_id, occurred_at DESC)` | N+1 latest-event lookups | 1,579 MB |
| I2 | `events (account_id, occurred_at)` | per-account time ranges, deep pagination | 1,579 MB |
| I3 | `events (campaign_id, event_type)` | campaign funnel counts | 1,130 MB |
| I4 | `events USING brin (occurred_at)` | large time-window sorts and aggregates | 0.3 MB |
| I5 | `contacts (account_id, status)` | skewed account contact lists | 190 MB |
| I6 | `contacts (email_status) WHERE email_status = 'bounced'` | the useless-looking useful index | 1 MB |
| I7 | `contacts (lower(email))` | case-insensitive email lookup | 299 MB |
| I8 | `contacts (country, region)` | correlated predicates | 190 MB |

All eight together: about 5.0 GB, roughly half the dataset again.

**Deferred**, with reasons in ADR 0003: autoscaling (time-dependent, needs its
own load protocol); `work_mem` and `maintenance_work_mem` (per-operation and
per-worker consumption makes a per-setting cap unsafe); `shared_buffers` and
`effective_cache_size` (derived from compute size, never in play); the Neon
pooler (adds its own queue; not a v1 treatment).

## Baselines (ADR 0002)

- **Default:** primary keys only (Postgres does not index foreign keys), at
  2 CU.
- **Naive:** one single-column btree on every column that appears in a WHERE
  predicate of the profiled workload, at 2 CU. The rule is fixed now; Phase 1
  derives the column list from the query shapes mechanically, so the naive
  baseline cannot be weakened after the fact.
- Considered and rejected as a second naive baseline: "jump a tier". The tier
  ladder is inside the search space, so the loop's own candidates cover it,
  and a third baseline would add about 3.7 hours of held-out runs.

## Held-out split

70 / 30, profiling / held-out, at the account level, stratified by
account-size decile so both slices hold hot and cold accounts. A query
instance belongs to the slice of the account it targets; queries with no
account predicate split 70/30 by instance. Seed 628, recorded. Splitting by
account rather than by query instance means the held-out slice exercises
different skew hot spots than the profiled one, which is the overfitting the
check exists to catch.

## Load model (ADR 0004, ADR 0012, ADR 0014)

`config/load-model.toml`, embedded by hash in the Phase 2 criteria:

- Closed loop at 1, 8 and 32 sessions; 1 is diagnostic only.
- One dedicated connection per session (pool size = sessions), direct, no
  pooler.
- 60 s warm-up, 300 s measured window.
- 5,000 ms per-query timeout (server `statement_timeout`), plus 1,000 ms
  client grace.
- Errors and timeouts are results. Error ceiling 1%: above it a configuration
  cannot qualify at that level. Above 25% the run aborts and is recorded as
  failed.
- Mix weighted by observed frequency; seeded per session.
- `SELECT 1` probe once a second on its own connection, reported separately.
- p50 / p95 / p99 / max with sample counts; a p99 from fewer than 10,000
  samples is flagged unstable.

Generator and location: one OS process per session, run from GitHub-hosted
runners (ADR 0014). pgbench was rejected because a statement timeout ends a
pgbench client in Postgres 18.6, which would shrink the session count exactly
when the system is stressed.

## Cache states (ADR 0013)

`warm` and `post-restart-with-eviction`, named for the procedure. Block
counters, `neon_stat_file_cache` and `stats_reset` are recorded before and
after every run.

## Phase 4 budgets (ADR 0005)

| Budget | Cap |
| --- | --- |
| Experiments | 20 candidate bundles |
| Compute | 120 CU-hours |
| Model spend | 10 USD |

Development protocol (profiling slice): 8 and 32 sessions, 2 repetitions,
both cache states, default baseline interleaved; about 1 hour of runs per
configuration. Evaluation protocol (held-out): 1, 8 and 32 sessions, 5
repetitions, both cache states, randomized and interleaved; about 3.7 hours
per configuration.

## Noise gate (ADR 0009)

On the Free plan, 1 CU, 8 sessions, warm, 5 runs of the spike dataset's
indexed variant. The smallest effect the criteria may call a pass is set at a
20% p99 improvement (Phase 2 may raise it; lowering it re-opens the gate).
**Go** when both p95 and p99 run-to-run spread, (max - min) / median, are
below 20%, every run has a stable p99, and no run breaks the error ceiling.
Otherwise the fallback (ADR 0007).

The spike dataset (434 MB: 300k contacts, 2.5M events, same schema and skew
as Phase 1) is cache-resident at 1 CU, so the gate's spread is an optimistic
floor (R10). A non-gating series at 0.25 CU records how noisy shared,
pageserver-bound compute is. The gate also runs the default variant once at
the same level: that is the one end-to-end variant-versus-default comparison
the Phase 0 exit asks for.

Results: `docs/noise-gate.md` (**GO**: p99 spread 1.2%, p95 15.1%) and
`docs/phase0-findings.md`, which carries two changes forward: Phase 2 criteria
are per shape, and 0.25 and 0.5 CU are back in the tier list (ADR 0015).

## Cost

`docs/cost-estimate.md`, generated from the two config files by
`pocbench cost` (a test fails if it goes stale). At Phase 0: about **$32
expected, $40 worst case** for the build, assuming about two weeks of calendar
time (the binding constraint is about 62 hours of benchmark runs, not build
work), and about $3.70 a month to keep the dataset afterward. The epic's
provisional $100-130 was high: compute for the whole matrix is 130-200
CU-hours (about $14-21); the model budgets, storage and history make up the
rest.

## Phase 1 (RC1-629)

The corpus is a seeded 10.98 GB contact/event dataset on its own `corpus`
branch; every variant is a child of it. The prospect's query log is 200,000
literal SQL statements; the profiler derives shapes from the text alone.
Traps are listed in `docs/nastiness.md`, committed before any run. Three
decisions:

- **ADR 0016:** the profile's timing comes from a capture of the profiling
  slice on the naive baseline at 2 CU, because PK-only cannot serve the
  workload at this scale (Reid's call).
- **ADR 0017:** the account-level split is the best of 64 seeded candidates,
  chosen on per-shape counts only.
- **ADR 0018:** a tail is supported when 100 observations from 10 distinct
  instances lie beyond it (p95: 2,000 and 200; p99: 10,000 and 1,000).

Results: `docs/phase1-profile.md` and `docs/phase1-findings.md`, which carry
two changes forward: Phase 3 gates scored jobs on the network probe (R9),
and Phase 2 criteria handle baselines that fail outright (R13).

## Phase 2 (RC1-630)

The buyer's goal (`config/goal.toml`) plus the Phase 1 profile generate
`criteria/criteria.json`: 80 criteria (latency ceilings on the four goal
shapes, no regression against today on the other five, per-shape error
ceilings, throughput and cost bounds) at 8 and 32 sessions and both cache
states, with the uncertainty method written into the file. Frozen once,
sha256 `da51e19e9bcae98581003a0e86ed776e20860b032f0ca158ad850d43c2c7d616`,
embedding the hashes of the load model, config space, goal, split manifest,
profile and nastiness list. CI and every benchmark job verify the pin
(ADR 0019). Rendering: `docs/criteria.md`.


## Phase 3 (RC1-631)

The harness reads the criteria only through `criteria.load_verified()`,
gates every job and run on the network probe, writes a hash-chained
append-only record, and has a development entry point (profiling slice only)
and an evaluation entry point that refuses without a frozen candidate pin.
Two decisions:

- **ADR 0020:** network gate (preflight of 300 round trips, then the frozen
  rule on every run, three attempts per cell), server checks, the record and
  what it accepts, the two entry points, seeded interleaving, and pooling
  from scored rows with verified raw logs.
- **ADR 0021:** post-restart eviction scans a per-branch filler table sized
  at 1.25 times the live file-cache limit; startup latency is recorded on
  every post-restart run instead of adding a third cache state.

Both baselines ran across 3 levels and both cache states, 24 of 24 runs
scored (`docs/phase3-baselines.md`). `docs/phase3-findings.md` carries three
things forward to Phase 4: the baselines fail on errors and tail support
(R13), post-restart reads as warm (R14), and evaluation needs more than one
job (R15).


## Phase 4 (RC1-632)

An adaptive loop proposes, Python decides, and Phase 4 runs across GitHub jobs.
Three decisions:

- **ADR 0022:** Claude proposes one experiment per iteration under a JSON
  schema; `pocbench.space` validates it (tiers and index ids only, no
  redundant index, memory and autoscaling rejected by name); Python enforces
  the experiment, CU-hour and model-spend budgets. An experiment is the
  candidate and naive interleaved on the development protocol. Predictions
  apply the frozen verdict rules (`pocbench.verdicts`), with naive pooled
  across experiments and tail support projected to the evaluation protocol.
- **ADR 0023:** after the stop, Python picks the cheapest bundle whose 80
  predicted criteria are all met, or refuses with a class (BUDGET EXHAUSTED,
  then INSUFFICIENT EVIDENCE, NO IMPROVEMENT FOUND, CRITERIA UNMET). Two
  fixtures pin both outputs.
- **ADR 0024:** state is committed to the story branch after each job; one
  `phase4` task runs whatever is next; jobs dispatch the next job only after
  a clean exit or network rejection, with a hop counter and a kill switch.
  Evaluation runs both slices with every row pinned to the freeze.
- **First live jobs (2026-11-07).** Two loop jobs stopped on the probe rule
  at the naive 32-session post-restart cell, 3 attempts each (255-258 of 300
  probe samples, latency healthy). The cause was the load generator, not the
  network: the measured window opened at a fixed time while 33 processes
  were still connecting to the just-restarted compute (first probe sample at
  42 s). Processes now connect first and the clock starts when all report
  (cap 120 s); the wait is recorded as `connect_wait`. The first of the two
  jobs also lost its state (`git add` with a missing `candidates/` path staged
  nothing); about 2.5 CU-h from it is outside the loop's compute count.

## Phase 6 (RC1-634)

- **ADR 0026:** the eval suite drives the real loop with the real Claude
  proposer over the two Phase 4 fixture workloads (production validator,
  budgets, verdicts and output rule; only the experiments are modeled). Six
  cases, three billed (about $0.55 a run). Each loop iteration is a traced
  LLM Obs span with its spend under ml_app `poc-benchmark-agent`; the
  experiment runs outside the span. Gauges: refusals by class, interval
  coverage, verdict agreement. Findings: `docs/phase6-findings.md`.
