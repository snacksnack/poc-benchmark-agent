# ADR 0006: The treatment is the bundle

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D6

## Context

A candidate changes several things at once (a tier and a set of indexes). On Neon a tier change alone moves six settings (ADR 0001).

## Decision

The unit of treatment is the whole bundle. A per-change effect is claimed only where an isolated ablation actually ran.

## Consequences

- The cost model budgets two ablations at the candidate tier (docs/cost-estimate.md).
- The results document never decomposes a bundle's gain without an ablation behind each piece.
