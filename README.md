# POC Benchmark Agent

Profiles a customer's database workload, recommends a configuration, and
proves it with an honest benchmark.

Point it at a workload somebody else wrote and it says what compute size and
indexes to run it on, then measures the old and new setups the same way under
the same declared load. Success criteria are written and hash-pinned before
any experiment runs, the recommendation is scored on a held-out slice it never
saw, and the results document reports losses and refusals as well as wins.

Status: **Phase 7, complete** (RC1-635). The engagement writeup is the front
page of the published documents, https://snacksnack.github.io/poc-benchmark-agent/
(`docs/engagement.md`, ADR 0027): the run's answer was a refusal, CRITERIA
UNMET, with the three query shapes that are why. The loop log is rendered in
full as `docs/loop-log.md`. Phase 6 (RC1-634) added an agent-evals suite that
runs the real Claude proposer through the real loop on modeled workloads, LLM
Obs traces per loop iteration with spend, and Datadog gauges (ADR 0026,
`docs/phase6-findings.md`).
Phase 5 (RC1-633) built the results document, generated from verified
evidence (`docs/results.md`, ADR 0025, `docs/phase5-findings.md`), with fixture
documents for a loss, an under-sampled key tail and a refusal
(`docs/fixtures/`). Phase 4 (RC1-632) ran the adaptive loop and the held-out
evaluation and froze a refusal, CRITERIA UNMET (ADR 0022-0024,
`docs/phase4-findings.md`). Phase 3 (RC1-631) built the benchmark harness (ADR
0020, ADR 0021). Phase 2 (RC1-630) froze and pinned the success criteria
(`docs/criteria.md`). Phase 1 (RC1-629) built the corpus, query log, profiler
and held-out split. Phase 0 (RC1-628) committed the decisions, risk log, load
model and cost model, and passed the noise gate (`docs/noise-gate.md`). Epic:
RC1-595.

## Layout

| Path | What |
| --- | --- |
| `config/load-model.toml` | The declared load. Pinned by hash into the Phase 2 criteria. |
| `config/config-space.toml` | Tiers, index candidates, baselines, held-out split, budgets, noise gate, rates. |
| `docs/decisions.md` | Phase 0 decisions in one readable page. |
| `docs/adr/` | One ADR per decision (0001-0013 from the epic's decision log, 0014+ from Phase 0). |
| `docs/risks.md` | Risk log, reviewed at every phase exit. |
| `docs/cost-estimate.md` | Generated build-cost estimate (`pocbench cost`). |
| `pocbench/loadgen.py` | Closed-loop load generator: one process per session, errors as results, null-query probe. |
| `pocbench/neon.py` | Neon API client: create and delete configuration-variant branches. |
| `pocbench/gate.py` | Phase 0 noise gate and the first variant-versus-default run. |
| `docs/nastiness.md` | The designed traps, committed before any experiment. |
| `pocbench/corpus.py` | Phase 1 dataset: seeded, resumable load onto the `corpus` branch. |
| `pocbench/workload.py` | The prospect's query log as literal SQL. |
| `pocbench/shapes.py`, `pocbench/profile.py` | Profiler: shapes from the SQL alone, capture, plans, selectivity. |
| `pocbench/split.py` | Account-level held-out split. |
| `results/phase1/` | Query log, split manifest, profile and tier probe. |
| `config/goal.toml` | The buyer's stated goal: the one human input to the criteria. |
| `criteria/` | The frozen criteria and their pin; `pocbench verify-criteria` checks both. |
| `pocbench/harness.py` | Phase 3 harness: network gate, cache-state procedures, interleaved schedule, net latency, pooling, entry points. |
| `pocbench/record.py` | Append-only, hash-chained results record; refuses rows a scored run could not produce. |
| `pocbench/slices.py`, `pocbench/candidate.py` | Slice rebuild with the leak guard; the frozen candidate or refusal the evaluation entry point requires. |
| `pocbench/phase3.py` | Phase 3 on Neon: baseline branches, filler, development protocol, cost lines, cleanup. |
| `pocbench/loop.py`, `pocbench/phase4.py` | Phase 4: the adaptive loop, the freeze, and the held-out evaluation across jobs. |
| `pocbench/results.py`, `pocbench/phase5_fixtures.py` | Phase 5: the results document from verified evidence, and fixture evidence with planted bad news. |
| `docs/results.md`, `docs/fixtures/` | Generated results documents (real run, fixtures). Do not edit by hand. |
| `evals/` | Phase 6 eval suite: six cases, three billed (`python -m evals`). |
| `pocbench/telemetry.py` | LLM Obs (one span per loop iteration) and `pocbench.*` gauges. |
| `docs/engagement.md` | The engagement writeup; every figure re-derived by `tests/test_engagement.py`. |
| `pocbench/loop_report.py`, `docs/loop-log.md` | The loop log as a readable page, verified against the frozen pin. |
| `publish/` | The Pages build (`uv run --group site python -m publish`). |

## Running

```bash
uv sync --all-groups
uv run pytest                         # unit tests; set POCBENCH_TEST_DSN for the Postgres ones
uv run pocbench fingerprint           # hashes of the pinned config files
uv run pocbench cost --write docs/cost-estimate.md
uv run pocbench spike-load --dsn "$DATABASE_URL"
uv run pocbench run --dsn "$DATABASE_URL" --label dev --sessions 8 --out runs/dev

# Phase 1 against a local Postgres 18 (test-scale corpus; needs pg_stat_statements preloaded)
uv run pocbench corpus-load --dsn "$LOCAL_DSN" --fresh
uv run pocbench profile --dsn "$LOCAL_DSN" --measure 20
uv run pocbench profile-report --results runs/phase1
```

On Neon, `corpus-load`, `profile`, `tier-probe` and `harness-baselines` run
from the benchmark workflow: add the `benchmark: <task>` label to a PR.

```bash
# Phase 3 harness
uv run pocbench harness-eval                     # refuses: no frozen candidate yet
uv run pocbench verify-record runs/phase3/record.jsonl
uv run pocbench harness-report --run-dir runs/phase3 --results results/phase3 \
  --write docs/phase3-baselines.md
uv run pocbench harness-try --dsn "$CORPUS_DSN" --measure 30   # dev aid; never writes the record
```

```bash
# Phase 5 results documents (offline; regenerate after any change, tests check freshness)
uv run pocbench results-doc --write docs/results.md
uv run pocbench results-fixture losing --write docs/fixtures/results-losing.md
# Phase 6 eval suite: BILLED (about $0.55; needs ANTHROPIC_API_KEY exported).
# EVAL_DATABASE_URL records to the shared store; DD_API_KEY traces the run.
uv run --group evals python -m evals --post-datadog
uv run --group evals python -m evals --case report-honesty   # a free case
```

Scored runs execute from a GitHub-hosted runner (`.github/workflows/benchmark.yml`),
not a laptop. See ADR 0014.
