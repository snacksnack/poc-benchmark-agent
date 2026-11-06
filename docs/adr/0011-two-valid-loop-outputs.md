# ADR 0011: Two valid loop outputs: recommendation or refusal

Status: accepted
Date: 2026-11-06
Source: RC1-595 decision log D11

## Context

If the loop could only succeed, it would be pressured to find something.

## Decision

The loop terminates in exactly one of: a frozen recommendation (hash-pinned before any held-out run) or a refusal record (ADR 0008). Both are valid, and both proceed to held-out evaluation and a results document.

## Consequences

- The results document always exists, so a refusal is visible rather than an absent artifact.
