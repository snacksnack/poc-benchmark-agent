# ADR 0014: Load generator: one process per session, run from a GitHub-hosted runner

Status: accepted
Date: 2026-11-06
Source: RC1-628 Phase 0

## Context

The load model (ADR 0004) has to be executed without the client distorting the latencies it reports, and from a place whose network noise is small and measurable. Phase 0 checked pgbench first: in Postgres 18.6 a non-retryable error such as a statement timeout ends that pgbench client (only serialization and deadlock failures can be retried via `--max-tries`), so a closed loop of N sessions silently becomes fewer than N exactly when the system is under stress. Errors and timeouts must be results, not session deaths. pgbench also cannot replay a fixed list of query instances with their parameters.

## Decision

- **Generator:** `pocbench.loadgen`. One OS process per session, each with one dedicated connection (pool size = sessions), so no session waits on another's Python work. Every query is recorded with phase, shape, start offset, latency, outcome and SQLSTATE. Server-side `statement_timeout` plus a client deadline (timeout + grace); a connection error reconnects after a backoff and counts as an error.
- **Probe:** a separate connection runs `SELECT 1` once a second through the measured window. Its distribution is reported beside the workload's so network and client jitter can be read apart from database time.
- **Location:** GitHub-hosted runners for every scored run, including the Phase 0 noise gate (`.github/workflows/benchmark.yml`), against Neon in `aws-us-east-1`. A laptop on residential Wi-Fi is allowed for development only. Runner metadata is recorded with each run.

## Consequences

- Runner region and hardware can vary between jobs. Interleaving baselines inside one job controls most of it, and the probe exposes the rest (R9). If the probe shows the network dominates the tail, the fix is a fixed-location runner, not more repetitions.
- A GitHub job is capped at 6 hours, so the held-out evaluation (about 11 hours of runs) is split across jobs with baselines interleaved inside each.
- The runner is free for a public repo, so it is not in the cost model.
