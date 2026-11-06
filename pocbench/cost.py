"""Build-cost estimate from the full experiment matrix.

Everything is derived from config/config-space.toml and config/load-model.toml
plus the ASSUMPTIONS below, so a changed protocol re-prices itself. Two cases:
"expected" puts Phase 4 candidates at the mean tier, "worst" puts every
candidate at the largest tier and spends every budget to the cap.
"""

from dataclasses import dataclass

from pocbench import sizing

# Work the matrix does not describe. Each is a guess until the phase that
# owns it measures it; the estimate is re-run at each phase exit.
ASSUMPTIONS = {
    "build_months": 0.5,  # ~35-40 h of benchmark runs spread over about two weeks
    "phase1_load_cu_hours": 8.0,  # generate ~10.5 GB on 4 CU, ~2 h
    "phase1_index_measure_cu_hours": 4.0,  # build all 8 candidates once on 4 CU
    "phase1_profile_cu_hours": 2.0,  # capture the workload profile, 1 h at 2 CU
    "index_build_hours_per_experiment": 0.25,  # bundle build on the experiment's branch
    "setup_overhead": 0.10,  # restarts, connection setup, gaps between runs
    "phase3_shakedown_repeats": 2,  # baselines through the dev protocol, twice
    "ablations": 2,  # per-change claims need an executed ablation (ADR 0006)
    "phase6_model_spend_usd": 5.0,
    "history_window_days": 1,
}


@dataclass(frozen=True)
class Line:
    item: str
    cu_hours: float
    usd: float
    basis: str


def run_minutes(load_model: dict, space: dict, protocol: dict) -> float:
    """Wall minutes for one configuration through one protocol."""
    timing = load_model["timing"]
    per_cell = 0.0
    for state in protocol["cache_states"]:
        spec = space["cache_states"][state]
        warm = timing["warmup_seconds"] / 60 if spec["uses_warmup"] else 0.0
        per_cell += warm + spec["extra_minutes"] + timing["measure_seconds"] / 60
    cells = len(protocol["levels"]) * protocol["repetitions"]
    return per_cell * cells * (1 + ASSUMPTIONS["setup_overhead"])


def estimate(load_model: dict, space: dict, case: str) -> list[Line]:
    if case not in ("expected", "worst"):
        raise ValueError(case)
    rate = space["cost"]["cu_hour_usd"]
    tiers = space["tiers"]["compute_units"]
    default_cu = space["tiers"]["default_cu"]
    candidate_cu = max(tiers) if case == "worst" else sum(tiers) / len(tiers)
    dev, ev = space["protocol"]["dev"], space["protocol"]["eval"]
    dev_h = run_minutes(load_model, space, dev) / 60
    eval_h = run_minutes(load_model, space, ev) / 60
    budget = space["budgets"]["phase4"]

    lines = []

    def compute(item: str, cu_hours: float, basis: str) -> None:
        lines.append(Line(item, cu_hours, cu_hours * rate, basis))

    compute(
        "Phase 1 dataset, index sizing, profile",
        ASSUMPTIONS["phase1_load_cu_hours"]
        + ASSUMPTIONS["phase1_index_measure_cu_hours"]
        + ASSUMPTIONS["phase1_profile_cu_hours"],
        "assumed: load on 4 CU, build all candidates once, 1 h profile capture",
    )
    compute(
        "Phase 3 harness shakedown",
        2 * default_cu * dev_h * ASSUMPTIONS["phase3_shakedown_repeats"],
        f"both baselines at {default_cu} CU through the dev protocol "
        f"x{ASSUMPTIONS['phase3_shakedown_repeats']}",
    )
    per_exp = candidate_cu * (dev_h + ASSUMPTIONS["index_build_hours_per_experiment"])
    if dev["interleave_baseline"]:
        per_exp += default_cu * dev_h
    phase4 = budget["max_experiments"] * per_exp
    capped = min(phase4, budget["max_compute_cu_hours"])
    compute(
        "Phase 4 adaptive loop (profiling slice)",
        capped,
        f"{budget['max_experiments']} experiments x {per_exp:.1f} CU-h "
        f"(candidate at {candidate_cu:.2f} CU + interleaved default baseline)"
        + (f", capped by the {budget['max_compute_cu_hours']} CU-h budget"
           if capped < phase4 else ""),
    )
    compute(
        "Phase 4 held-out evaluation",
        (candidate_cu + 2 * default_cu) * eval_h,
        f"frozen candidate + 2 baselines, eval protocol ({eval_h:.1f} h each)",
    )
    compute(
        "Phase 4 ablations",
        ASSUMPTIONS["ablations"] * candidate_cu * eval_h,
        f"{ASSUMPTIONS['ablations']} ablations at the candidate tier, eval protocol",
    )

    ds = space["dataset"]
    months = ASSUMPTIONS["build_months"]
    storage = space["cost"]["storage_gb_month_usd"]
    root_gb = ds["target_gb"]
    lines.append(Line("Root branch storage", 0, root_gb * storage * months,
                      f"{root_gb} GB x ${storage}/GB-month x {months:g} months"))
    restore = space["cost"]["instant_restore_gb_month_usd"]
    lines.append(Line("Instant-restore history", 0, root_gb * restore * months,
                      f"upper bound: the whole root rewritten inside a "
                      f"{ASSUMPTIONS['history_window_days']}-day window"))
    index_gb = sum(sizing.candidate_bytes(c, ds) for c in space["index"]) / 1e9
    index_months = months if case == "worst" else months / 2
    lines.append(Line("Index storage on child branches", 0, index_gb * storage * index_months,
                      f"all 8 candidates ({index_gb:.1f} GB upper bound) live for "
                      f"{index_months:g} month(s)"))
    lines.append(Line("Model spend, Phase 4 loop", 0, budget["max_model_spend_usd"],
                      "budget cap"))
    lines.append(Line("Model spend, Phase 6 evals", 0, ASSUMPTIONS["phase6_model_spend_usd"],
                      "assumed"))
    return lines


def monthly_after(space: dict) -> float:
    return space["dataset"]["target_gb"] * space["cost"]["storage_gb_month_usd"]


def render(load_model: dict, space: dict, load_model_sha: str, space_sha: str) -> str:
    out = [
        "# Build cost estimate",
        "",
        "Generated by `pocbench cost` from `config/config-space.toml` "
        f"(sha256 `{space_sha[:12]}`) and `config/load-model.toml` "
        f"(sha256 `{load_model_sha[:12]}`). Do not edit by hand; change the "
        "config or the assumptions in `pocbench/cost.py` and regenerate.",
        "",
        f"Rates: Neon Launch, ${space['cost']['cu_hour_usd']}/CU-hour, "
        f"${space['cost']['storage_gb_month_usd']}/GB-month storage, "
        f"${space['cost']['instant_restore_gb_month_usd']}/GB-month instant restore "
        "(neon.com/pricing, last updated 2026-11-04, read 2026-11-06). Phase 0 runs on "
        "the Free plan and costs nothing.",
        "",
    ]
    for case in ("expected", "worst"):
        lines = estimate(load_model, space, case)
        total_cu = sum(x.cu_hours for x in lines)
        total = sum(x.usd for x in lines)
        out += [
            f"## {case.capitalize()} case",
            "",
            "| Item | CU-hours | USD | Basis |",
            "| --- | ---: | ---: | --- |",
        ]
        for x in lines:
            cu = f"{x.cu_hours:.1f}" if x.cu_hours else ""
            out.append(f"| {x.item} | {cu} | {x.usd:.2f} | {x.basis} |")
        out += [f"| **Total** | **{total_cu:.1f}** | **{total:.2f}** | |", ""]
    out += [
        f"After the build: about ${monthly_after(space):.2f}/month to keep the root "
        "branch (compute scales to zero; delete the project to stop it entirely).",
        "",
        "## Assumptions",
        "",
    ]
    out += [f"- `{k}` = {v}" for k, v in ASSUMPTIONS.items()]
    out += [
        "",
        "Not priced: the GitHub-hosted runner that generates load (free for a public "
        "repo) and egress (well inside the 500 GB/project Launch allowance; result "
        "sets are bounded by LIMIT).",
        "",
    ]
    return "\n".join(out)
