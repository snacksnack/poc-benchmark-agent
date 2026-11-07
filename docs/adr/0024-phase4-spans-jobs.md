# ADR 0024: Phase 4 spans GitHub jobs through committed state and a guarded self-dispatch

Status: accepted
Date: 2026-11-07
Source: RC1-632 Phase 4

## Context

R15: an experiment takes about 1.7 hours (16 runs), so the loop needs about
7 jobs, and the evaluation protocol on both slices (3 configurations x 3
levels x 2 cache states x 5 repetitions x 2 slices = 180 runs, 120 after a
refusal) needs 3-4 more. A hosted job stops at 6 hours. ADR 0020 already
allows a record to be continued by committing it and reopening it under the
same pins. Scored runs must come from hosted runners (ADR 0014).

## Decision

- **State is in git.** After every Phase 4 job the workflow commits
  `results/phase4/` (loop log, development and evaluation records, gzipped
  naive raw logs, per-experiment cells, a line per job) and
  `candidates/frozen.*` to the story branch. Candidate and evaluation raw
  logs go to the job's artifact (90 days); their hashes are in the records.
- **One task, state-driven.** `phase4` runs `pocbench phase4-next`: `loop`
  until a freeze, `evaluate` until every planned cell is scored, then
  `eval-report`, which fetches every evaluation artifact, verifies each raw
  log against the record and pools. Evaluation runs both slices so the
  profiled-versus-held-out gap uses one protocol (epic rule 3); every
  evaluation row carries the frozen pin, and the record refuses evaluation
  rows without one.
- **Jobs stop between units of work.** The loop does not start an experiment
  that would not finish inside the job (estimate x 1.15); the evaluation does
  not start a cell that would not. A paused loop re-proposes from the same
  evidence next job.
- **Guarded self-dispatch.** The job dispatches the next one on the same
  branch only if its main step exited 0 or 2 (network rejection: a new runner
  may pass), work remains, the `chain_left` counter is positive (14 by
  default, one less each hop), and the repository variable `POCBENCH_CHAIN`
  is not `off`. A crash or timeout never chains; a person looks first.
- **Cleanup.** Branches are named `p4-*`, deleted in the job's `finally`
  block and again by an always-run workflow step.

## Consequences

- Phase 4 takes 2-3 days of wall-clock runner time. That matches the cost
  model's "about 58 hours of measured runs per engagement".
- The story branch receives bot commits while its PR is open. They touch only
  `results/phase4/` and `candidates/`.
- Artifacts expire after 90 days; the evaluation must be pooled within that
  window (it is, by the chain's last job).
