"""Phase 2 success criteria (RC1-630, ADR 0002, ADR 0019).

`generate` turns the customer goal (config/goal.toml), the Phase 1 profile
and split manifest, the load model and the config space into one criteria
document. It also fixes the uncertainty method, so Phase 5 cannot choose one
after seeing numbers. `freeze` writes it once as canonical JSON and records
its SHA-256 in a pin file. `load_verified` is what every later stage calls:
it fails if the criteria file, or any file the criteria embed by hash (load
model, config space, split manifest, profile, goal, nastiness list), has
changed since the freeze. Python enforces this; nothing asks the model.
"""

import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from pocbench import config, shapes

CRITERIA_PATH = config.REPO_ROOT / "criteria" / "criteria.json"
PIN_PATH = config.REPO_ROOT / "criteria" / "criteria.sha256"
GOAL_PATH = config.REPO_ROOT / "config" / "goal.toml"

# Files the criteria embed by hash. A change to any of them after the freeze
# fails every run that loads the criteria.
PINNED_INPUTS = {
    "load_model": "config/load-model.toml",
    "config_space": "config/config-space.toml",
    "goal": "config/goal.toml",
    "split_manifest": "results/phase1/split-manifest.json",
    "profile": "results/phase1/profile.json",
    "nastiness": "docs/nastiness.md",
}
PERCENTILE_LADDER = (99, 95, 50)
ORDER_SEED = 630
# Run validity for the net-latency basis (R9): the runner's round trip is
# subtracted, so it must be stable within the run and not dwarf the queries.
PROBE_P50_MAX_MS = 100.0
PROBE_JITTER_MAX_MS = 15.0
CI_Z = 1.96


class CriteriaPinError(RuntimeError):
    """The criteria, or a file they embed, changed after the freeze."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _percentile_for(shape: dict, requested: int) -> tuple[int, str | None]:
    """Highest percentile at or below the requested one that the held-out
    slice can support (ADR 0018). Returns (percentile, note)."""
    supported = shape["held_out_tail_supported"]
    for q in PERCENTILE_LADDER:
        if q > requested:
            continue
        if q == 50 or supported.get(f"p{q}"):
            if q == requested:
                return q, None
            return q, (f"p{requested} requested; the held-out slice has "
                       f"{shape['held_out_distinct']:,} distinct instances, below the "
                       f"p{requested} floor, so the criterion is p{q}")
    raise AssertionError("unreachable: p50 is always available")


def method(lm: dict, space: dict) -> dict:
    r, ev = lm["report"], space["protocol"]["eval"]
    return {
        "latency_basis": {
            "name": "net latency",
            "definition": ("client-measured latency of each successful query minus the "
                           "median probe (SELECT 1) round trip of the same run"),
            "why": "runner-to-Neon round trip varied 3.5-66 ms across jobs (R9)",
            "run_valid_if": {"probe_p50_ms_at_most": PROBE_P50_MAX_MS,
                             "probe_p99_minus_p50_ms_at_most": PROBE_JITTER_MAX_MS},
            "invalid_run": "discarded and re-run; never scored, never averaged in",
        },
        "repetitions": ev["repetitions"],
        "levels": ev["levels"],
        "cache_states": ev["cache_states"],
        "interleave_baselines": ev["interleave_baseline"],
        "order": f"randomized within each repetition, seed {ORDER_SEED}",
        "slice": "held_out (the loop never sees it; ADR 0002, R3)",
        "pooling": ("percentiles are computed on observations pooled across the "
                    "repetitions of one configuration, level and cache state"),
        "interval": {"method": "distribution-free binomial order-statistic interval "
                               "(normal approximation), pocbench.stats.quantile_ci",
                     "confidence": 0.95, "z": CI_Z},
        "spread": ("every criterion also reports the per-repetition values and their "
                   "spread, (max - min) / median"),
        "noise_rule": {
            "baseline_spread_at_least": space["noise_gate"]["smallest_pass_effect"],
            "then": "comparisons against that baseline are inconclusive (not met)",
        },
        "under_sampled": {
            "tail_beyond": r["tail_beyond"],
            "tail_distinct_beyond": r["tail_distinct_beyond"],
            "rule": ("a percentile is supported when the pooled observations and the "
                     "distinct instances beyond it reach the floors (load model, ADR 0018). "
                     "An unsupported percentile in a ceiling criterion makes it "
                     "unsupported (not met). A no-regression comparison uses the highest "
                     "percentile supported on both sides, falling back p99, p95, p50"),
        },
        "verdicts": {
            "ceiling": "met if the interval's upper bound <= ceiling; failed if its lower "
                       "bound > ceiling; otherwise inconclusive (not met)",
            "no_regression": "met if candidate upper bound <= (1 + tolerance) x baseline "
                             "upper bound; failed if candidate lower bound > (1 + tolerance) "
                             "x baseline upper bound; otherwise inconclusive (not met)",
            "error_ceiling": "pooled (errors + timeouts) / observations",
            "throughput_floor": "median across repetitions of successful queries per second",
            "cost_ceiling": "pooled CU x cu_hour_usd x measured seconds / 3600 / successful "
                            "queries",
            "recommendation": "every criterion met; otherwise the candidate fails and the "
                              "output may be a refusal (ADR 0011)",
            "mix_level": "mix-level numbers are reported, never scored (R12)",
        },
    }


def generate(goal: dict, manifest: dict, profile: dict, lm: dict, space: dict,
             input_hashes: dict[str, str], frozen_at: str) -> dict:
    lm_levels = set(lm["sessions"]["levels"]) - set(lm["sessions"]["diagnostic_only"])
    if not set(goal["levels"]) <= lm_levels:
        raise ValueError(f"goal levels {goal['levels']} must be non-diagnostic load-model "
                         f"levels {sorted(lm_levels)}")
    if goal["today"] not in space["baselines"]:
        raise ValueError(f"goal.today {goal['today']!r} is not a baseline")
    cache_states = space["protocol"]["eval"]["cache_states"]
    ceiling_x = {"warm": 1.0, "post-restart-with-eviction":
                 goal["ceilings"]["post_restart_multiplier"]}
    known = manifest["shapes"]
    goal_ids = []
    for g in goal["shape"]:
        if g["id"] not in known:
            raise ValueError(f"goal shape {g['id']} is not in the profile")
        fp = shapes.Normalized(known[g["id"]]["template"], ()).fingerprint
        if fp != g["fingerprint"]:
            raise ValueError(f"goal shape {g['id']} fingerprint {g['fingerprint']} does not "
                             f"match the profiled template ({fp})")
        goal_ids.append(g["id"])
    err_ceiling = lm["errors"]["error_ceiling_fraction"]
    tol = goal["no_regression"]["tolerance"]
    out, n = [], 0

    def add(**c) -> None:
        nonlocal n
        n += 1
        out.append({"id": f"C{n:03d}"} | c)

    for level in goal["levels"]:
        for cs in cache_states:
            for g in goal["shape"]:
                q, note = _percentile_for(known[g["id"]], 99)
                add(kind="latency_ceiling", shape=g["id"], shape_name=g["name"],
                    template=known[g["id"]]["template"], level=level, cache_state=cs,
                    percentile=q, ceiling_ms=g["ceiling_ms"] * ceiling_x[cs],
                    **({"note": note} if note else {}))
            for sid in sorted(known):
                if sid in goal_ids:
                    continue
                add(kind="no_regression", shape=sid, template=known[sid]["template"],
                    level=level, cache_state=cs, against=goal["today"], tolerance=tol,
                    percentile="highest supported on both sides (p99, p95, p50)")
            for sid in sorted(known):
                add(kind="error_ceiling", shape=sid, level=level, cache_state=cs,
                    max_fraction=err_ceiling)
            add(kind="throughput_floor", level=level, cache_state=cs, against=goal["today"],
                at_least_ratio=1.0)
            add(kind="cost_ceiling", level=level, cache_state=cs, against=goal["today"],
                at_most_ratio=1.0, metric="cost per successful query")
    return {
        "schema_version": 1,
        "frozen_at": frozen_at,
        "pins": {k: {"path": PINNED_INPUTS[k], "sha256": v} for k, v in input_hashes.items()},
        "goal": {
            "use_case": goal["use_case"],
            "statement": ("Hold p99 on the four shapes that dominate time (the daily report, "
                          "email lookup, campaign funnel and account timeline) at 8 and 32 "
                          "sessions, with no other shape worse than today, at the lowest-cost "
                          "tier that does it."),
            "today": goal["today"],
            "objective": goal["objective"],
            "shapes": goal_ids,
        },
        "baselines": space["baselines"],
        "profile_reference": {
            "log_rows_profiled": profile["headline"]["log_rows_profiled"],
            "shapes": profile["headline"]["shapes"],
            "note": "profile latencies are the naive capture's client latency; they "
                    "describe today, they are not the criteria",
        },
        "method": method(lm, space),
        "criteria": out,
    }


def input_hashes(root: Path = config.REPO_ROOT) -> dict[str, str]:
    return {k: sha256_file(root / p) for k, p in PINNED_INPUTS.items()}


def build(root: Path = config.REPO_ROOT, frozen_at: str | None = None) -> dict:
    goal = tomllib.loads((root / "config" / "goal.toml").read_text())
    lm = config.load_model(root / PINNED_INPUTS["load_model"]).data
    space = config.config_space(root / PINNED_INPUTS["config_space"]).data
    manifest = json.loads((root / PINNED_INPUTS["split_manifest"]).read_text())
    profile = json.loads((root / PINNED_INPUTS["profile"]).read_text())
    return generate(goal, manifest, profile, lm, space, input_hashes(root),
                    frozen_at or datetime.now(UTC).date().isoformat())


def freeze(doc: dict, criteria_path: Path = CRITERIA_PATH, pin_path: Path = PIN_PATH) -> str:
    """Write the criteria once. Re-freezing is refused: changing frozen
    criteria is a deliberate, visible act (delete both files in a commit)."""
    if criteria_path.exists() or pin_path.exists():
        raise CriteriaPinError(f"{criteria_path.name} is already frozen; refusing to overwrite")
    raw = canonical(doc)
    criteria_path.parent.mkdir(parents=True, exist_ok=True)
    criteria_path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    pin_path.write_text(f"{sha}  {criteria_path.name}\n")
    return sha


def verify(root: Path = config.REPO_ROOT, criteria_path: Path | None = None,
           pin_path: Path | None = None) -> dict:
    """Load the criteria read-only and check every pin. Raises CriteriaPinError
    naming each mismatch; returns the parsed criteria when all hold."""
    criteria_path = criteria_path or root / "criteria" / "criteria.json"
    pin_path = pin_path or root / "criteria" / "criteria.sha256"
    if not criteria_path.exists() or not pin_path.exists():
        raise CriteriaPinError("criteria are not frozen (criteria.json or its pin is missing)")
    raw = criteria_path.read_bytes()
    problems = []
    pinned = pin_path.read_text().split()[0]
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pinned:
        problems.append(f"criteria.json sha256 {actual[:12]} != pinned {pinned[:12]}")
    doc = json.loads(raw)
    for pin in doc["pins"].values():
        path = root / pin["path"]
        if not path.exists():
            problems.append(f"{pin['path']} is missing")
            continue
        now = sha256_file(path)
        if now != pin["sha256"]:
            problems.append(f"{pin['path']} changed after the freeze "
                            f"({now[:12]} != {pin['sha256'][:12]})")
    if problems:
        raise CriteriaPinError("criteria pin check failed: " + "; ".join(problems))
    return doc


def load_verified(root: Path = config.REPO_ROOT) -> dict:
    """The only way later stages read the criteria."""
    return verify(root)


def render(doc: dict, sha: str) -> str:
    g, m = doc["goal"], doc["method"]
    lines = ["# Success criteria", "",
             f"RC1-630. Frozen {doc['frozen_at']}. `criteria/criteria.json`, sha256 `{sha}`.",
             "Generated by `pocbench criteria-freeze`; every later stage loads it through "
             "`pocbench.criteria.load_verified`, which fails if it or any pinned input "
             "changed. This page is a rendering; the JSON is the contract.", "",
             "## Goal", "", f"> {g['statement']}", "",
             f"Use case: {g['use_case']}. Today: the `{g['today']}` baseline (ADR 0016). "
             f"Objective: minimize {g['objective']['minimize'].replace('_', ' ')} at "
             f"{g['objective']['at_level']} sessions among candidates that meet every "
             "criterion.", "", "## Pinned inputs", "", "| Input | Path | sha256 |",
             "| --- | --- | --- |"]
    for k, p in sorted(doc["pins"].items()):
        lines.append(f"| {k} | `{p['path']}` | `{p['sha256'][:16]}...` |")
    lines += ["", "## Method", "",
              f"- **Latency basis:** {m['latency_basis']['definition']}. A run is valid only "
              f"if probe p50 <= {m['latency_basis']['run_valid_if']['probe_p50_ms_at_most']:g} "
              f"ms and probe p99 - p50 <= "
              f"{m['latency_basis']['run_valid_if']['probe_p99_minus_p50_ms_at_most']:g} ms; "
              f"an invalid run is {m['latency_basis']['invalid_run']}.",
              f"- **Repetitions:** {m['repetitions']} per configuration, level and cache state, "
              f"baselines interleaved, {m['order']}, on the {m['slice']}.",
              f"- **Pooling:** {m['pooling']}.",
              f"- **Interval:** {m['interval']['method']}, {m['interval']['confidence']:.0%}.",
              f"- **Spread:** {m['spread']}.",
              f"- **Noise:** if a baseline's spread is at least "
              f"{m['noise_rule']['baseline_spread_at_least']:.0%}, {m['noise_rule']['then']}.",
              f"- **Under-sampled:** {m['under_sampled']['rule']}.",
              "", "Verdicts:", ""]
    lines += [f"- **{k.replace('_', ' ')}:** {v}" for k, v in m["verdicts"].items()]
    crit = doc["criteria"]
    lines += ["", f"## Criteria ({len(crit)})", "", "### Latency ceilings", "",
              "| ID | Shape | Level | Cache state | Percentile | Ceiling (net ms) | Note |",
              "| --- | --- | ---: | --- | ---: | ---: | --- |"]
    for c in crit:
        if c["kind"] == "latency_ceiling":
            lines.append(f"| {c['id']} | {c['shape']} {c['shape_name']} | {c['level']} "
                         f"| {c['cache_state']} | p{c['percentile']} | {c['ceiling_ms']:,.0f} "
                         f"| {c.get('note', '')} |")
    lines += ["", "### No regression against today", "",
              "| ID | Shape | Level | Cache state | Tolerance |",
              "| --- | --- | ---: | --- | ---: |"]
    for c in crit:
        if c["kind"] == "no_regression":
            lines.append(f"| {c['id']} | {c['shape']} `{shapes.label(c['template'])}` "
                         f"| {c['level']} | {c['cache_state']} | {c['tolerance']:.0%} |")
    lines += ["", "### Errors, throughput and cost", "",
              "| ID | Kind | Shape | Level | Cache state | Bound |",
              "| --- | --- | --- | ---: | --- | --- |"]
    for c in crit:
        if c["kind"] == "error_ceiling":
            bound = f"<= {c['max_fraction']:.0%} errors + timeouts"
        elif c["kind"] == "throughput_floor":
            bound = f">= {c['at_least_ratio']:g} x {c['against']}"
        elif c["kind"] == "cost_ceiling":
            bound = f"<= {c['at_most_ratio']:g} x {c['against']}"
        else:
            continue
        lines.append(f"| {c['id']} | {c['kind'].replace('_', ' ')} | {c.get('shape', 'all')} "
                     f"| {c['level']} | {c['cache_state']} | {bound} |")
    return "\n".join(lines) + "\n"
