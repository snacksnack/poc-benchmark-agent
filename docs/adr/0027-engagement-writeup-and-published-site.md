# ADR 0027: The engagement writeup leads with the refusal, and the documents are published as a site

Status: accepted
Date: 2026-11-09
Source: RC1-635 Phase 7

## Context

The epic's headline form was "recommended configuration cut p99 from X to Y
at Z percent of the cost, at C sessions". The real run produced a refusal,
CRITERIA UNMET, and Phase 5 found no supported X: today times out on 36 to 97
percent of the four target shapes at 32 sessions. The ticket asks for a /work
card that leads with "the delta number and its concurrency", an engagement
writeup framed as a POC a customer ran, and the criteria, results, loop log,
ADRs and risk log published with it.

Every document here is committed markdown. The loop log is hash-chained JSON
lines, which proves it was not edited and cannot be read by a customer.

## Decision

- **The refusal is the headline.** The writeup and the card lead with "no
  recommendation", the three shapes that are why, and the concurrency it
  holds at (32 sessions). There is no delta to lead with, and inventing one
  from the best tested bundle would launder a configuration that fails to
  qualify. The best bundle's cost per successful query appears once, as
  context, labeled as failing to qualify.
- **The modeled qualifying workload is not borrowed for the headline.** The
  eval suite's qualifying fixture shows the loop recommends when evidence
  allows (1 CU, seven indexes, 80 of 80). It is cited under "how far to trust
  it" and labeled as a modeled workload, never as a result on this one.
- **`docs/engagement.md` is hand-written and pinned.** `tests/test_engagement.py`
  re-derives every figure it quotes from the same verified evidence the
  results document reads (`results.load_real`), and checks every relative
  link resolves. Writing the test caught one wrong figure carried from the
  Phase 5 findings (76.5 percent rounds to 76, not 77).
- **The loop log gets a generated page.** `pocbench loop-log-doc` renders
  every row in order with Claude's words quoted in full. It verifies the hash
  chain and checks the pin both ways: the rows before the freeze hash to the
  `loop_log_sha256` the frozen output carries, and the last row names the
  frozen file. A freshness test keeps `docs/loop-log.md` current.
- **Published with GitHub Pages from Actions**, the same way and for the same
  reasons as api-compatibility-lab's ADR 0008: built from main, never
  committed, built on pull requests so a broken link fails review, and a link
  that resolves to nothing fails the build. The writeup is the index. The
  renderer is a `site` dependency group and the `publish` package, outside
  `pocbench`.

## Consequences

- The card's number is a count of shapes and a failure rate, not a speedup.
  That is weaker as a hook and is the honest reading of this run.
- Phase 4's findings named s08's timeouts the large-sort trap and pointed at
  `work_mem`. The nastiness list and Phase 1 put s08 at Q6 (correlated
  predicates), whose fix is extended statistics; the large sort is s09 (Q8).
  The findings doc carries a dated correction rather than a rewrite.
- R4 (synthetic workload) and R7 (reads as DBA tuning) close here: the
  disclosure is on the card and the writeup is framed as an engagement.
