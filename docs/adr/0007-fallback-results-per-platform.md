# ADR 0007: Fallback results are reported per platform, never combined

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D7

## Context

If Neon's tail noise swallows any plausible effect (the Phase 0 gate), latency could be measured on local containerized Postgres while cost stays on Neon.

## Decision

In the fallback, latency results (local Postgres) and cost results (Neon) are published separately with platform labels. They are never combined into one headline.

## Consequences

- Cost is not immune to latency noise either: Neon bills active CU-seconds, so a slower configuration costs more.
- The fallback is chosen by the Phase 0 gate (ADR 0009), recorded in docs/noise-gate.md.
