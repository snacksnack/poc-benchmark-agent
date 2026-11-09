# Phase 6 findings

RC1-634, 2026-11-08. What the eval suite and the Datadog wiring showed.
Design: ADR 0026.

## 1. Exit

- **Subject listed and run once.** `poc-benchmark-agent` in agent-evals
  `docs/measuring.md` and the weekly sweep. First run
  `poc-benchmark-agent-20261109T030243.271359Z`, shared store: 6 of 6 cases
  pass, 2 advisory failures (below), $0.539254 of model spend, 6 Claude calls.
  Second run on the committed code (`poc-benchmark-agent-20261109T030850.599744Z`):
  same outcome and the same seven-index choice, $0.4539.
- **Loop traces with spend in the pane.** `ml_obs.trace{ml_app:poc-benchmark-agent}`
  = 6 traces under `service:evals`, `version:0.1.0`; 6 llm spans, 107,606
  input tokens; `ml_obs.span.llm.total.cost` = 539,254,000 nanodollars, the
  same figure the proposer's own price table recorded.
- **Entity in the catalog.** `datadog/entities/poc-benchmark-agent.yaml` in
  tpm-automation-platform, pushed with `catalog_sync`.
- **CI Test Optimization** was already wired in Phase 0 (`pytest --ddtrace`).

## 2. Claude's search on the modeled workloads

Both loops stopped by themselves after 2 experiments each.

| Workload | Experiments | Output | Spend |
| --- | --- | --- | --- |
| `qualifying` | 2 CU I1-I7, then 1 CU I1-I7 | candidate 1 CU I1-I7, 80 of 80 met on held-out | $0.255 |
| `refusal-criteria-unmet` | 2 CU I1-I7, then 4 CU I1-I7 | refusal CRITERIA UNMET, naming s05 and s07 | $0.284 |

On the refusal workload the model read s07 as compute-bound from the 2 CU
evidence, tested the largest tier with the only index that serves s07, and
stopped with the measured effect of doubling compute as its reason. It did
not keep testing to escape the refusal, which is what the system prompt asks
and what the experiment budget (20) would have allowed.

## 3. Seven indexes, not eight

On `qualifying` the model dropped I8 from its first proposal: s08 is 4 ms on
primary keys, and no criterion needs it faster. The fixture's script reaches
all eight at 1 CU. Under the output rule the eight-index bundle ranks first,
because s08 at 2 ms lifts closed-loop throughput enough to make it 0.17%
cheaper per successful query. That is far inside the 20% smallest pass effect,
and the seven-index bundle saves one index build and its storage. The
advisory `ranks-no-worse-than-the-reference-bundle` reports it red, which is
the honest reading of the rule as written. Whether the objective should treat
differences inside the noise floor as ties, and charge index storage, is a
question for the next engagement's criteria, not something to change after a
freeze.

## 4. Prediction calibration is now a tracked number

`pocbench.prediction.interval_coverage` = 0.4667 (14 of 30) and
`verdict_agreement` = 0.8875 (71 of 80) for the real run, published weekly.
The advisory floor is 90%. It stays red until the development interval
includes variation between jobs (R18).

## 5. This repo is invisible to CI Visibility

85 Actions runs since 2026-11-06 (the Phase 4 chain, PR CI) produced no
pipeline events, while the other active estate repos appear. The
estate's CI-failure monitor (318355169) queries pipeline events, so it cannot
see a red build here. Test Optimization still works (it is agentless from
pytest). The likely cause is the Datadog GitHub App's repository selection;
granting it this repository is an account setting. The entity carries
`gap:no-pipeline-events` and has no pipeline fingerprints until then.

## 6. Not done here

- The fleet LLM Obs dashboard (bwm-uny-qqs) does not list this ml_app, nor
  esp-migration-agent's. A fleet roster update is its own change.
- agent-evals prices Opus 5.5 cache reads at 0.1x input (list is 0.05x). This
  suite records its own cost; a harness release would fix every consumer.
- The Phase 4 loop job now traces when `DD_API_KEY` is set, but no real loop
  has run since; only the eval suite's spans have been seen.
