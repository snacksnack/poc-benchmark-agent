# ADR 0013: Cache states are named for the procedure attempted, with evidence recorded

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D13

## Context

"Cold" and "warm" are claims about state the client cannot fully observe on Neon: the pageserver tier is invisible, and a restart does not guarantee an empty local file cache.

## Decision

Cache states are named for what was done: `warm` (the load model's warm-up on the run's own mix) and `post-restart-with-eviction` (restart the compute, then run a scan over tables outside the mix). Every run records `pg_stat_database` block counters and `neon_stat_file_cache` before and after, so eviction is measured, never assumed. Results from different states are never averaged together.

## Consequences

- The Phase 0 load generator already records this evidence; on stock Postgres the Neon view is recorded as unavailable rather than dropped.
- `stats_reset` in the same snapshot detects a suspend that wiped statistics mid-run (R6).
