# ADR 0016: The profile's timing comes from a capture on the naive baseline

Status: accepted
Date: 2026-11-06
Source: RC1-629 Phase 1

## Context

The profiler works from the query log, and a real log carries a duration for
every statement. Here the log is generated, so its durations have to come
from running it somewhere. The prospect "runs today" at 2 CU
(config-space.toml), and the default baseline at that tier has primary keys
only.

On the 10 GB corpus a primary-key-only database cannot serve the workload.
Every per-contact lookup is a full scan of 50 million events, about 20-40 s
against the 5 s timeout. A capture there is mostly timeouts, so total time
per shape stops ranking anything, and no real prospect runs that way.

## Decision

The capture runs on the **naive baseline** at 2 CU: one single-column btree
per WHERE column, derived mechanically from the profiling slice's shapes
before any timing. That is what a team that "indexed what it filters on"
runs, and it is the configuration the recommendation has to beat.

The capture is a closed-loop run of the profiling slice only, with
pg_stat_statements reset first and the compute held awake (R6). Its window
(20 minutes after 60 s of warm-up) is set in config-space.toml [capture],
and it is labeled a capture, never a scored run. Reid's call, 2026-11-06.

## Consequences

- The naive traps (an `email` btree that cannot serve `lower(email)`, the
  `events(occurred_at)` index that walks churned accounts) show up in the
  profile the way an FDE would find them on a real system.
- The default baseline stays primary keys only for Phase 4. At this scale it
  will probably fail the error ceiling at every level; that is a finding to
  report, not a reason to change the baseline.
- The profile's latencies are the naive configuration's, not a neutral
  property of the workload. Shape frequencies, selectivity and estimate
  errors do not depend on the configuration; tails and time shares do.
