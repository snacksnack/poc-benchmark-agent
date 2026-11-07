"""Cost estimates from the full experiment matrix: the one-time build, and
one client engagement.

Everything is derived from config/config-space.toml and config/load-model.toml
plus the ASSUMPTIONS below, so a changed protocol re-prices itself. Two cases:
"expected" puts Phase 4 candidates at the mean tier, "worst" puts every
candidate at the largest tier and spends every budget to the cap.

An engagement reuses the matrix but drops build-only work (harness shakedown,
evals, measuring every candidate index up front) and scales with the client:
storage and load time with dataset size, CU-hours with the tier ladder.
"""

from dataclasses import dataclass

from pocbench import sizing

# Work the matrix does not describe. Each is a guess until the phase that
# owns it measures it; the estimate is re-run at each phase exit.
ASSUMPTIONS = {
    "build_months": 0.5,  # ~62 h of benchmark runs (6 h per job) over about two weeks
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

# Client scenarios. tier_scale multiplies every tier, the default tier and the
# Phase 4 compute budget, which keeps the dataset-to-cache ratio of the demo
# (ADR 0015): 100 GB against 1-16 CU has the same cache physics as 10.5 GB
# against 0.25-4 CU. Launch allows fixed sizes up to 16 CU.
ENGAGEMENTS = [
    {"name": "Demo scale", "dataset_gb": 10.5, "tier_scale": 1, "weeks": 2},
    {"name": "100 GB prospect", "dataset_gb": 100.0, "tier_scale": 4, "weeks": 2},
]
WEEKS_PER_MONTH = 4.345


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
    root_gb = dataset_gb(ds)
    lines.append(Line("Root branch storage", 0, root_gb * storage * months,
                      f"{root_gb} GB x ${storage}/GB-month x {months:g} months"))
    restore = space["cost"]["instant_restore_gb_month_usd"]
    lines.append(Line("Instant-restore history", 0, root_gb * restore * months,
                      f"upper bound: the whole root rewritten inside a "
                      f"{ASSUMPTIONS['history_window_days']}-day window"))
    index_gb = sum(sizing.candidate_bytes(c, ds) for c in space["index"]) / 1e9
    index_months = months if case == "worst" else months / 2
    bound = "measured" if all("measured_bytes" in c for c in space["index"]) else "upper bound"
    lines.append(Line("Index storage on child branches", 0, index_gb * storage * index_months,
                      f"all 8 candidates ({index_gb:.1f} GB {bound}) live for "
                      f"{index_months:g} month(s)"))
    lines.append(Line("Model spend, Phase 4 loop", 0, budget["max_model_spend_usd"],
                      "budget cap"))
    lines.append(Line("Model spend, Phase 6 evals", 0, ASSUMPTIONS["phase6_model_spend_usd"],
                      "assumed"))
    return lines


def engagement(load_model: dict, space: dict, scenario: dict, case: str) -> list[Line]:
    """One client engagement: load and profile their data, run the adaptive
    loop, evaluate on held-out, ablate. No build-only work."""
    if case not in ("expected", "worst"):
        raise ValueError(case)
    rate = space["cost"]["cu_hour_usd"]
    k, gb = scenario["tier_scale"], scenario["dataset_gb"]
    data_ratio = gb / dataset_gb(space["dataset"])
    tiers = [t * k for t in space["tiers"]["compute_units"]]
    default_cu = space["tiers"]["default_cu"] * k
    candidate_cu = max(tiers) if case == "worst" else sum(tiers) / len(tiers)
    dev_h = run_minutes(load_model, space, space["protocol"]["dev"]) / 60
    eval_h = run_minutes(load_model, space, space["protocol"]["eval"]) / 60
    budget = space["budgets"]["phase4"]
    months = scenario["weeks"] / WEEKS_PER_MONTH
    storage = space["cost"]["storage_gb_month_usd"]
    lines = []

    def compute(item: str, cu_hours: float, basis: str) -> None:
        lines.append(Line(item, cu_hours, cu_hours * rate, basis))

    compute("Load and profile the client's data",
            ASSUMPTIONS["phase1_load_cu_hours"] * data_ratio
            + ASSUMPTIONS["phase1_profile_cu_hours"] * k,
            f"load scales with data ({gb:g} GB), profile with the default tier ({default_cu:g} CU)")
    index_h = ASSUMPTIONS["index_build_hours_per_experiment"] * data_ratio / k
    per_exp = candidate_cu * (dev_h + index_h) + default_cu * dev_h
    loop = min(budget["max_experiments"] * per_exp, budget["max_compute_cu_hours"] * k)
    compute("Adaptive loop (profiling slice)", loop,
            f"up to {budget['max_experiments']} experiments, candidate at {candidate_cu:.2f} CU, "
            f"budget {budget['max_compute_cu_hours'] * k:g} CU-h")
    compute("Held-out evaluation", (candidate_cu + 2 * default_cu) * eval_h,
            "frozen candidate + 2 baselines, eval protocol")
    compute("Ablations", ASSUMPTIONS["ablations"] * candidate_cu * eval_h,
            f"{ASSUMPTIONS['ablations']} at the candidate tier")
    lines.append(Line("Dataset storage", 0, gb * storage * months,
                      f"{gb:g} GB for {scenario['weeks']} weeks"))
    restore = space["cost"]["instant_restore_gb_month_usd"]
    lines.append(Line("Instant-restore history", 0, gb * restore * months,
                      "upper bound, as in the build estimate"))
    idx_share = sum(sizing.candidate_bytes(c, space["dataset"]) for c in space["index"]) / (
        dataset_gb(space["dataset"]) * 1e9)
    idx_months = months if case == "worst" else months / 2
    lines.append(Line("Index storage on child branches", 0, idx_share * gb * storage * idx_months,
                      f"candidates at {idx_share:.0%} of data size"))
    lines.append(Line("Model spend, adaptive loop", 0, budget["max_model_spend_usd"],
                      "budget cap"))
    return lines


def engagement_run_hours(load_model: dict, space: dict) -> float:
    """Wall-clock hours of measured runs in one engagement (loop at the
    experiment cap, held-out evaluation, ablations)."""
    dev_h = run_minutes(load_model, space, space["protocol"]["dev"]) / 60
    eval_h = run_minutes(load_model, space, space["protocol"]["eval"]) / 60
    loop = space["budgets"]["phase4"]["max_experiments"] * 2 * dev_h  # candidate + baseline
    return loop + (3 + ASSUMPTIONS["ablations"]) * eval_h


def dataset_gb(ds: dict) -> float:
    """The measured size once Phase 1 has loaded the corpus, else the target."""
    return ds.get("measured_gb", ds["target_gb"])


def monthly_after(space: dict) -> float:
    return dataset_gb(space["dataset"]) * space["cost"]["storage_gb_month_usd"]


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
        "## Per client engagement",
        "",
        "What one engagement costs once the tool exists: load and profile the client's "
        "data, run the adaptive loop, evaluate on held-out, ablate. Build-only work "
        "(harness shakedown, evals, measuring every index up front) is excluded. The "
        "larger scenario multiplies every tier and the loop's compute budget by "
        "`tier_scale`, which keeps the demo's dataset-to-cache ratio.",
        "",
        "| Scenario | Data | Tiers (CU) | Expected | Worst case |",
        "| --- | ---: | --- | ---: | ---: |",
    ]
    for sc in ENGAGEMENTS:
        tiers = ", ".join(f"{t * sc['tier_scale']:g}" for t in space["tiers"]["compute_units"])
        exp = sum(x.usd for x in engagement(load_model, space, sc, "expected"))
        worst = sum(x.usd for x in engagement(load_model, space, sc, "worst"))
        out.append(f"| {sc['name']} | {sc['dataset_gb']:g} GB | {tiers} | "
                   f"${exp:.2f} | ${worst:.2f} |")
    for sc in ENGAGEMENTS:
        lines = engagement(load_model, space, sc, "expected")
        out += ["", f"### {sc['name']}, expected case", "",
                "| Item | CU-hours | USD | Basis |", "| --- | ---: | ---: | --- |"]
        for x in lines:
            cu = f"{x.cu_hours:.1f}" if x.cu_hours else ""
            out.append(f"| {x.item} | {cu} | {x.usd:.2f} | {x.basis} |")
        total = sum(x.usd for x in lines)
        out.append(f"| **Total** | **{sum(x.cu_hours for x in lines):.1f}** | "
                   f"**{total:.2f}** | |")
    out += [
        "",
        f"Elapsed time is the larger cost: about {engagement_run_hours(load_model, space):.0f} "
        "hours of measured runs per engagement, run in sequence so they do not "
        "disturb each other. Not priced: engineer time to agree the criteria with the "
        "buyer and present the results. Neon Scale bills compute at $0.222/CU-hour, "
        "about double the compute lines.",
    ]
    out += [
        "",
        "Not priced: the GitHub-hosted runner that generates load (free for a public "
        "repo) and egress (well inside the 500 GB/project Launch allowance; result "
        "sets are bounded by LIMIT).",
        "",
    ]
    return "\n".join(out)
