# ADR 0003: v1 space is compute tier plus bounded index candidates; autoscaling and memory settings deferred

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D3

## Context

An adaptive loop is only affordable over a small space, and every dimension in the space needs a load protocol that can measure it honestly.

## Decision

The v1 space is fixed compute tiers crossed with a bounded list of index candidates (`config/config-space.toml`). Deferred, with reasons:

- **Autoscaling.** Its behavior is time-dependent and needs its own load protocol (ramp shape, scale-up lag). A fixed-concurrency closed loop cannot characterize it.
- **work_mem and maintenance_work_mem.** Consumption is per sort or hash operation, per session, per parallel worker, times `hash_mem_multiplier`. A per-setting cap is unsafe; an aggregate memory budget needs the concurrency model first.
- **shared_buffers and effective_cache_size** derive from compute size on Neon and are never in play (ADR 0001).

## Consequences

- The tier list was narrowed in Phase 0 to dedicated-compute sizes only (ADR 0015).
- A recommendation outside this space is not a valid output; the loop can only propose bundles drawn from the config file.
