# ADR 0009: Free-plan noise is a feasibility gate only

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D9

## Context

Before building a harness on Neon, Phase 0 has to know whether run-to-run spread is small enough for any plausible effect to show through. The Free plan is enough to measure that, and costs nothing.

## Decision

Phase 0 runs the same workload on the same configuration N times at one concurrency level and compares the p95/p99 run-to-run spread with the smallest effect the criteria could call a pass. Spread below it: go. Otherwise: the fallback (ADR 0007). The gate's numbers are never reused as the uncertainty of a Phase 4 result; that uncertainty is measured alongside the real experiments, from interleaved baselines and randomized order.

## Consequences

- Gate parameters live in `[noise_gate]` in the config space. The smallest pass effect is set at 20% p99 improvement; Phase 2 may raise it, and lowering it re-opens the gate.
- The spike dataset is cache-resident at 1 CU, so the gate's spread is an optimistic floor. A failing gate is decisive; a passing gate is necessary, not sufficient (docs/decisions.md).
