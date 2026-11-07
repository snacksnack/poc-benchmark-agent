# ADR 0012: Closed-loop results are stated as performance at fixed concurrency

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D12

## Context

An earlier draft of the epic had coordinated omission backwards. In a closed loop, a slow response delays that session's next request, so the generator offers less load exactly when the system is struggling, and the measured tail looks better than an arrival-rate workload would see. Open-loop generators are the correction, not the cause.

## Decision

Every v1 result is phrased as "at N concurrent sessions". No v1 result is phrased as a sustained request rate or a latency at a given QPS.

## Consequences

- Throughput is reported as an observed outcome of a run, never as an input.
- The results document names coordinated omission as a known limitation of v1.
