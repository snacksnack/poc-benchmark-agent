# ADR 0021: Post-restart eviction scans a filler table sized from the live file-cache limit

Status: accepted
Date: 2026-11-07
Source: RC1-631 Phase 3

## Context

The frozen config space (ADR 0013, ADR 0019) names two cache states: `warm`
and `post-restart-with-eviction`, the latter "restart the compute, then run a
scan workload over tables outside the mix". The corpus has no table outside
the mix. The Phase 1 tier probe also shows Neon prewarm settings
(`neon.file_cache_prewarm_*`), so a restart may not leave the file cache
empty. The ticket lists a third state, a plain post-restart first run with
startup latency recorded apart; the config space was frozen with two.

## Decision

- **Filler.** Each configuration's branch gets an `eviction_filler` table
  after it is resized to its run tier. Its size is 1.25 times the live
  file-cache limit read from `pg_settings` on that compute (6.4 GB at 2 CU,
  so about 8 GB), in rows of about 930 bytes that stay inline. It lives and
  dies with the branch, so it adds no lasting storage, and the corpus branch
  is never written to.
- **Procedure.** Restart through the Neon API, wait for the first connection
  and query (recorded as startup, apart from the run), snapshot the cache
  evidence, scan the filler, snapshot again, then measure with no warm-up.
  The run's own before and after snapshots give its file-cache and
  shared-buffer hit ratios, so eviction is measured, not assumed.
- **Third state.** Not added: the config space and criteria are frozen. The
  plain post-restart state's one distinct fact, startup latency, is recorded
  on every `post-restart-with-eviction` run.

## Consequences

- Building the filler costs a few minutes at 2 CU per branch per job. If
  Phase 4's per-experiment branches make that bind on the 6-hour job limit,
  the filler can move to the corpus branch once (about $3/month storage).
- The pageserver tier is still not controlled (R5); a scan past the file
  cache does not touch it.
- Local Postgres (the ADR 0007 fallback) cannot restart through the harness,
  so its post-restart cells are skipped and listed as skipped.
