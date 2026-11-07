# ADR 0002: Criteria hash-pinned before the recommendation; held-out scoring; two baselines

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D2

## Context

A benchmark run by the party that made the recommendation is only credible if the definition of success could not move after the results were known, and if the recommendation is scored on work it was not fitted to.

## Decision

1. Success criteria, including the load model, are written and hash-pinned in Phase 2, before any experiment runs. A changed criteria or load-model file at report time fails the run. Enforced in Python, not asked of the model.
2. The recommendation is scored on a held-out slice the agent never saw, with profiled and held-out gains printed side by side.
3. Two baselines: the untuned default, and a deliberately naive recommendation. Beating naive is the claim.

## Consequences

- The Phase 2 to Phase 4 order is load-bearing and must not be rearranged.
- A large profiled-versus-held-out gap is reported, not hidden: it is the overfitting signal.
- Baseline definitions are fixed in `config/config-space.toml` (see ADR 0015 and docs/decisions.md).
