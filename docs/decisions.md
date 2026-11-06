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

**Compute tiers:** 1, 2, 4 CU fixed size. Default tier for both baselines:
2 CU. 0.25 and 0.5 CU are shared compute and are excluded (ADR 0015).

**Dataset** (Phase 1 builds it): about 10.5 GB, 6M contacts and 50M events,
against 3 / 6 / 12 GB of compute cache. Sized by ratio to cache, not in
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

Results: `docs/noise-gate.md`.

## Cost

`docs/cost-estimate.md`, generated from the two config files by
`pocbench cost` (a test fails if it goes stale). At Phase 0: about **$45
expected, $51 worst case** for the build, and about $3.70 a month to keep the
dataset afterward. The epic's provisional $100-130 was high: compute for the
whole matrix is 160-200 CU-hours (about $17-21); storage, history and the model
budgets make up the rest.
