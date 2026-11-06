# POC Benchmark Agent

Profiles a customer's database workload, recommends a configuration, and
proves it with an honest benchmark.

Point it at a workload somebody else wrote and it says what compute size and
indexes to run it on, then measures the old and new setups the same way under
the same declared load. Success criteria are written and hash-pinned before
any experiment runs, the recommendation is scored on a held-out slice it never
saw, and the results document reports losses and refusals as well as wins.

Status: **Phase 0** (RC1-628). Decisions, risk log, load model and cost model
are committed; the noise gate is next. Epic: RC1-595.

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

## Running

```bash
uv sync --all-groups
uv run pytest                         # unit tests; set POCBENCH_TEST_DSN for the Postgres ones
uv run pocbench fingerprint           # hashes of the pinned config files
uv run pocbench cost --write docs/cost-estimate.md
uv run pocbench spike-load --dsn "$DATABASE_URL"
uv run pocbench run --dsn "$DATABASE_URL" --label dev --sessions 8 --out runs/dev
```

Scored runs execute from a GitHub-hosted runner (`.github/workflows/benchmark.yml`),
not a laptop. See ADR 0014.
