# ADR 0010: Harness is Phase 3, loop is Phase 4

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D10

## Context

The harness must exist and be trusted before any candidate is measured, and nothing may touch the held-out slice before the candidate is frozen.

## Decision

Phase 3 builds the harness and exercises it against the two baselines only. Its development entry point reads the profiling slice only; its evaluation entry point refuses to run without a frozen candidate hash or a refusal record. Phase 4 runs the loop, freezes or refuses, then evaluates on held-out.

## Consequences

- Load-bearing order: criteria freeze (2), harness against baselines (3), development experiments, candidate freeze, held-out evaluation (4).
- A planted-leak test proves the development entry point cannot read held-out data (R3).
