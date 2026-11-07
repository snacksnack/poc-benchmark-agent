# ADR 0005: The recommendation step is an adaptive loop with experiment, compute and model-spend budgets

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D5

## Context

A one-shot recommendation from a profile is a guess. An unbounded search is unaffordable and invites overfitting to the profiling slice.

## Decision

Phase 4 runs an adaptive loop: propose a bundle from the config space, measure it on the profiling slice, update, repeat. It stops at whichever budget runs out first: experiments, compute CU-hours, or model spend (`[budgets.phase4]` in the config space: 20 experiments, 120 CU-hours, 10 USD at Phase 0).

## Consequences

- The loop sees only profiling-slice results (rule 4).
- Exhausting a budget is a named refusal class (ADR 0008), not an error.
