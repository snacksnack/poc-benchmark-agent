# ADR 0008: Refusals are scoped to the tested space and budget, in four classes

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D8

## Context

"Nothing helps" is an overclaim. An honest refusal says what was tested and why it stopped.

## Decision

A refusal states that no tested candidate within the permitted space and budget met the criteria, and names one of four classes: insufficient evidence, budget exhausted, no improvement, criteria unmet.

## Consequences

- A refusal still gets a held-out evaluation of the baselines, and Phase 5 still emits a results document with the best-tested candidate labeled as failing to qualify (ADR 0011).
