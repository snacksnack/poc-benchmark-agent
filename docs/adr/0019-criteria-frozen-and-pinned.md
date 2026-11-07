# ADR 0019: Success criteria are generated, frozen once and pinned with their inputs

Status: accepted
Date: 2026-11-07
Source: RC1-630 Phase 2

## Context

ADR 0002 requires the criteria to be hash-pinned before any experiment, with
a changed file failing the run. Phase 1 added three constraints: criteria
are per shape (R12), a thin held-out slice cannot support a p99 (ADR 0018),
and client latency moves with the runner's network round trip (R9). The
naive baseline also fails 11.5% of queries today (R13), so a criterion
cannot assume the baseline produces a usable tail.

## Decision

- **One human input.** `config/goal.toml` holds the buyer's goal (Reid,
  2026-11-07): hold p99 on the four shapes that dominate time at 8 and 32
  sessions, no other shape worse than today, lowest-cost tier. Ceilings:
  daily report 2 s, email lookup 100 ms, campaign funnel 2 s, account
  timeline 1 s, doubled after a restart with eviction.
- **Generated, not hand-written.** `pocbench criteria-freeze` builds
  `criteria/criteria.json`: per goal shape, a latency ceiling at each level
  and cache state; every other shape, no worse than today by more than 10%;
  every shape, at most 1% errors and timeouts; throughput at least and cost
  per successful query at most today's. 80 criteria.
- **Method fixed in the file.** Net latency (client latency minus the run's
  median probe round trip; a run with probe p50 over 100 ms or p99 - p50
  over 15 ms is discarded and re-run), 5 interleaved repetitions in
  randomized order on the held-out slice, pooled percentiles with a 95%
  distribution-free interval, a verdict rule for every criterion kind
  (inconclusive counts as not met), and the noise and under-sampled rules.
- **Frozen once.** `freeze` refuses to overwrite. The pin file holds the
  SHA-256 of the canonical JSON. The JSON embeds the SHA-256 of the load
  model, config space, goal, split manifest, profile and nastiness list.
- **Checked everywhere.** `pocbench.criteria.load_verified` is the only way
  later stages read the criteria. CI and every benchmark workflow job run
  `pocbench verify-criteria` first. A test edits the frozen file, and each
  pinned input in turn, and expects the run to fail.

## Consequences

- The treatment space (config-space.toml) is frozen with the criteria.
  Changing a tier, a candidate index or a budget now means deleting the
  criteria in a visible commit and freezing again, before any experiment.
- The pin detects post-hoc edits by the pipeline. It does not stop someone
  editing both the file and its pin; git history is the audit trail, and the
  sha256 is recorded in this repo's docs and on the epic.
- Net latency is an estimate of server time: it removes a stable round trip,
  not jitter. Server-side percentiles would need per-statement server timing,
  which Neon does not expose to the client.
- The default baseline will likely fail its error ceilings everywhere at this
  scale; the criteria still evaluate it and report the failures.
