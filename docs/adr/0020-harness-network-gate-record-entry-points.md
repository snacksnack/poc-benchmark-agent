# ADR 0020: The harness gates on the network, writes an append-only record, and has two entry points

Status: accepted
Date: 2026-11-07
Source: RC1-631 Phase 3

## Context

Phase 1 realized R9: the runner-to-Neon round trip was 3.5 ms on one job and
39 and 66 ms on others. The frozen criteria (ADR 0019) score net latency,
client latency minus the run's median probe round trip, and declare a run
valid only if probe p50 <= 100 ms and probe p99 - p50 <= 15 ms. Something
has to enforce that. ADR 0010 also requires a development entry point that
cannot reach the held-out slice and an evaluation entry point that refuses
without a frozen candidate, and the ticket requires append-only results that
no development aid can write to.

## Decision

- **Criteria in one way.** `harness.open_session` reads the criteria only
  through `criteria.load_verified`, derives the criteria hash from the
  document it returned, and checks the load model and config space it loads
  against the criteria's pins. The probe rule, order seed, tail floors and
  interval come from the frozen file, not from module constants.
- **Network gate (R9).** A job opens with a 60-second preflight probe on the
  first branch. A slow or jittery network stops the job (exit 2) before index
  builds or runs spend compute. Every run is then checked against the frozen
  rule. A probe that measured fewer than 90% of its expected round trips, or
  lost any, also fails: an unmeasured network is not a stable one. A failed
  run is recorded as `rejected` with its reasons and re-run, up to three
  attempts per cell; a cell with no valid run stops the job, because ADR
  0014 names a fixed-location runner as the fix, not more repetitions.
- **Server checks (R6).** A run is also rejected if the compute restarted,
  statistics reset, or a session failed to connect inside the window.
- **Append-only record.** `pocbench/record.py`: one hash-chained JSON line
  per run after a header that pins the criteria, load model and config space.
  Each row carries those hashes, the configuration hash, slice, cache state,
  level, repetition, attempt, platform, runner, probe verdict, per-shape
  client and net summaries, cost, cache evidence and the SHA-256 of every raw
  log. `verify` names an edited, reordered or deleted row. A record opened
  under other pins is refused: a new freeze starts a new record.
- **What the record accepts.** Only rows a scored run could produce: the load
  model's measure window, timeout and grace, the cache state's warm-up, the
  pinned load model hash, and the slice that matches the entry point. The
  development aid (`pocbench harness-try`) refuses a full window and never
  opens the record; if it did, the record would refuse its rows.
- **Entry points.** `development` takes the profiling slice and re-checks
  every instance against the rebuilt assignment and the manifest's account
  list before running; the planted-leak test plants a held-out item and
  expects a refusal before any target is touched. `held_out()` needs a
  `Frozen` record that only `candidate.load_frozen` can make, after checking
  its pin and its criteria hash. `harness-eval` refuses before opening the
  record or creating a branch.
- **Order.** Each repetition runs every (configuration, level, cache state)
  cell once, in a seeded random order (criteria seed 630, tagged by entry
  point), so the baselines interleave and drift lands in the spread.
- **Pooling.** Scored rows only; raw logs must match their recorded hashes.
  Percentiles are pooled per configuration, level and cache state, with the
  95% order-statistic interval, per-repetition values and spread, tail
  support (ADR 0018) from the instance ids the load generator now logs, and
  mix-level numbers labeled reported, never scored.

## Consequences

- A far or noisy runner costs a rejected job, not a misleading result. If
  rejections become routine, the fallback is a fixed-location runner.
- Phase 3 runs the development protocol at all three load-model levels
  (protocol.dev lists 8 and 32; level 1 is added for the exit criterion and
  stays diagnostic), 2 repetitions, both cache states: 24 runs.
- The chain detects edits by the pipeline, not someone rewriting the whole
  file; git history is the audit trail, as for the criteria.
- Phase 4 appends to a record by committing it and reopening it under the
  same pins; a job-local record is enough for Phase 3.
