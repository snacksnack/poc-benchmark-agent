# ADR 0001: Neon Postgres, for sizing and cost, not GUC tuning

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D1

## Context

The agent recommends a configuration for a workload someone else wrote. It needs a platform where a configuration variant over an identical dataset is cheap to stand up, and where cost per query is metered finely enough that a "percent of the cost" claim is not a rounding artifact.

On Neon, instance-level Postgres parameters are not user-configurable. `shared_buffers` and `effective_cache_size` derive from compute size, and changing compute size moves `max_connections`, `max_worker_processes`, `shared_buffers`, `effective_cache_size`, `maintenance_work_mem` and the compute cache at once (neon.com/docs/reference/compatibility, read 2026-11-06).

## Decision

Run on Neon, and frame the recommendation as sizing and cost: which compute tier and which indexes. Never present a result as tuning one internal setting.

A compute tier is a legitimate treatment. Nothing is attributed to an individual setting inside it.

## Consequences

- Copy-on-write branches give every variant the same pages in seconds, and CU-second billing gives a true cost per query.
- `fsync` is off on Neon (durability lives in the safekeepers), so write-path numbers are not comparable to standalone Postgres. v1 reports read-path results; any write shape is labeled.
- Three cache tiers (shared_buffers, local file cache up to 75% of RAM, pageserver over the network) plus cold start. The pageserver tier is unobservable from the client (R5, ADR 0013).
- Cumulative statistics are discarded when a compute suspends (R6).
- Sizing is a purchasing decision; shared_buffers is a DBA decision. The first is the FDE lane (R7).
