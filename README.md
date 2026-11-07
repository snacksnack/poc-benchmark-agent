# POC Benchmark Agent

Profiles a customer's database workload, recommends a configuration, and
proves it with an honest benchmark.

Point it at a workload somebody else wrote and it says what compute size and
indexes to run it on, then measures the old and new setups the same way under
the same declared load. Success criteria are written and hash-pinned before
any experiment runs, the recommendation is scored on a held-out slice it never
saw, and the results document reports losses and refusals as well as wins.

Status: **Phase 1** (RC1-629): corpus, query log, profiler and held-out split
(`docs/phase1-profile.md`, `docs/phase1-findings.md`). Phase 0 (RC1-628) committed the decisions, risk
log, load model and cost model, and passed the noise gate
(`docs/noise-gate.md`). Epic: RC1-595.

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

On Neon, `corpus-load`, `profile` and `tier-probe` run from the benchmark
workflow: add the `benchmark: <task>` label to a PR.

Scored runs execute from a GitHub-hosted runner (`.github/workflows/benchmark.yml`),
not a laptop. See ADR 0014.
