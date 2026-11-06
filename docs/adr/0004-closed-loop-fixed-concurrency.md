# ADR 0004: Closed-loop, fixed-concurrency load model; open loop deferred

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D4

## Context

A sizing claim needs a declared load. The two families are closed loop (N sessions, each sending its next query when the last returns) and open loop (a target arrival rate independent of response time).

## Decision

v1 is closed loop at 1, 8 and 32 sessions, fixed duration, per-query timeout, errors and timeouts counted as results. The exact numbers live in `config/load-model.toml`, which Phase 2 embeds by hash in the criteria. Concurrency 1 is diagnostic and can never alone support a sizing claim.

## Consequences

- Results mean performance at N sessions, not a sustained arrival rate (ADR 0012).
- The open-loop generator is v2. The Phase 0 load generator (ADR 0014) records per-query start offsets so a v2 can reuse its output format.
