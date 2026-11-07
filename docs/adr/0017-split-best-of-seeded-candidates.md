# ADR 0017: The held-out split is the best of 64 seeded candidates

Status: accepted
Date: 2026-11-06
Source: RC1-629 Phase 1

## Context

The split is at the account level, stratified by account-size decile
(config-space.toml [split]), so held-out results test tenants the loop
never saw. The ticket also asks for stratification by shape frequency. An
account-level split cannot give every shape exactly 30%: the largest tenant
owns about 8% of contacts and carries a large share of every account-scoped
shape at once.

## Decision

Draw 64 seeded account-level candidates (each stratified by decile; tenantless
queries split per instance) and keep the one whose worst per-shape held-out
fraction is nearest 30%. Ties go to the lowest candidate number. The
selection reads shape counts only, never a latency, so it cannot carry
performance into the split. The manifest records the seed, the candidate
chosen, its worst deviation, and a SHA-256 of the full log-row-to-slice
assignment, so a re-derived split can be checked against it.

## Consequences

- Every shape gets a held-out share near 30% without breaking the
  tenant boundary.
- A shape with few distinct instances in the held-out slice (one per held-out
  account, for example) cannot support a tail there. It is flagged by the
  distinct-instance floor (ADR 0018), not dropped.
