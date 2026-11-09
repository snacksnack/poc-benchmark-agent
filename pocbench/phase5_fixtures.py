"""Phase 5 fixture evidence (RC1-633): results documents with planted bad news.

A Phase 5 fixture (fixtures/phase5/*.json) names a Phase 4 fixture workload
and what to plant in the held-out evaluation. The builder runs the real loop,
validator, verdicts and decision code over that workload to a freeze, then
synthesizes the evaluation the freeze unlocks: the frozen candidate (or,
after a refusal, the two baselines) and today, on both slices, at the
evaluation protocol's levels, cache states and repetitions. Plants apply to
the held-out slice only, so they are exactly what a profiling slice cannot
show:

- `latency_factor`: a shape runs this many times slower for one config.
- `distinct_cap`: the distinct instances beyond a percentile are capped (a
  held-out slice with few distinct values in its tail).

Every cell is labeled platform `fixture-model`, so no fixture figure can be
read as a measurement.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from pocbench import candidate, config, criteria, fixtures, harness, split
from pocbench.results import Evidence

PHASE5_DIR = config.REPO_ROOT / "fixtures" / "phase5"
PLATFORM = "fixture-model"
NAMES = ("losing", "under-sampled", "refusal")


def load(name: str) -> dict:
    return json.loads((PHASE5_DIR / f"{name}.json").read_text())


def _plants(p5: dict, cfg_role: str) -> tuple[dict[str, float], dict[str, dict[int, int]]]:
    factor: dict[str, float] = {}
    cap: dict[str, dict[int, int]] = {}
    for pl in p5.get("held_out", []):
        if pl["config"] not in ("*", cfg_role):
            continue
        if "latency_factor" in pl:
            factor[pl["shape"]] = factor.get(pl["shape"], 1.0) * pl["latency_factor"]
        if "distinct_cap" in pl:
            cap.setdefault(pl["shape"], {})[pl["percentile"]] = pl["distinct_cap"]
    return factor, cap


def _eval_cells(fx: dict, p5: dict, sp: dict, configs: dict[str, tuple[str, float, set]]
                ) -> list[dict]:
    ev = sp["protocol"]["eval"]
    out = []
    for sl in (split.HELD_OUT, split.PROFILING):
        for name, (role, cu, ids) in configs.items():
            factor, cap = _plants(p5, role) if sl == split.HELD_OUT else ({}, {})
            for lvl in ev["levels"]:
                lat = (fixtures.naive_latency(fx, lvl) if role == "today"
                       else fixtures.shape_latency(fx, ids, cu, lvl))
                lat = {s: ms * factor.get(s, 1.0) for s, ms in lat.items()}
                for cs in ev["cache_states"]:
                    c = fixtures.cells_for(fx, name, lat, cu, lvl, cs, slice_=sl,
                                           reps=ev["repetitions"], distinct_cap=cap)
                    out.append(c | {"platform": PLATFORM})
    return out


def _builds(sp: dict, prof: dict, configs: dict[str, tuple[str, float, set]]) -> dict:
    """Index build seconds and bytes from Phase 1's measurements (the same
    indexes on the same corpus), so the cost lines have real inputs."""
    from_id = {ix["id"]: ix for ix in sp["index"]}
    secs = list(prof["candidate_build_seconds"].values())
    build_s = {ix["id"]: secs[n] for n, ix in enumerate(sp["index"])}
    db = sp["dataset"]["measured_gb"] * 1e9
    out = {}
    for name, (role, cu, ids) in configs.items():
        if role == "today":
            idx = {d: prof["naive_build_seconds"][d] for d in prof["naive_ddl"]}
            sizes = {d.split()[5]: prof["index_bytes"].get(d.split()[5], 0)
                     for d in prof["naive_ddl"]}
        else:
            idx = {from_id[i]["ddl"]: build_s[i] for i in sorted(ids)}
            sizes = {i: from_id[i]["measured_bytes"] for i in sorted(ids)}
        out[name] = {"cu": cu, "index_build_seconds": idx,
                     "sizes": {"secondary_index_bytes": sizes,
                               "database_bytes_excluding_filler": db + sum(sizes.values())}}
    return out


def evidence(name: str) -> Evidence:
    return build(load(name), name)


def build(p5: dict, name: str) -> Evidence:
    """Evidence from a Phase 5 fixture document (tests build controls inline)."""
    fx = fixtures.load(p5["phase4_fixture"])
    doc = criteria.load_verified()
    crit_sha = harness.criteria_sha256(doc)
    sp = config.config_space().data
    with tempfile.TemporaryDirectory() as tmp:
        _, decision, runner = fixtures.run_fixture(fx, doc, sp, Path(tmp) / "log.jsonl",
                                                   clock=lambda: "fixture")
    # The fixture loop ran under the real frozen criteria; its log header just
    # says "fixture". Pin the decision to the criteria it was made under.
    decision["criteria_sha256"] = crit_sha
    frozen_sha = hashlib.sha256(candidate._canonical(decision)).hexdigest()
    today = doc["goal"]["today"]
    base = doc["baselines"]
    configs: dict[str, tuple[str, float, set]] = {
        today: ("today", fx["base_cu"], set()),
        "default": ("default", base["default"]["cu"], set(base["default"]["indexes"]))}
    if decision["kind"] == "candidate":
        c = decision["config"]
        configs[c["name"]] = ("candidate", c["cu"], set(c["index_ids"]))
    prof = json.loads((config.REPO_ROOT / criteria.PINNED_INPUTS["profile"]).read_text())
    return Evidence(
        title=f"fixture `{name}`", doc=doc, criteria_sha256=crit_sha, frozen=decision,
        frozen_sha256=frozen_sha, cells=_eval_cells(fx, p5, sp, configs),
        dev_cells={k: [c | {"platform": PLATFORM} for c in v]
                   for k, v in runner.cells_by_name.items()},
        dev_naive=[c | {"platform": PLATFORM} for c in runner.naive_cells()],
        builds=_builds(sp, prof, configs), space=sp, load_model=config.load_model().data,
        profile=prof, execution=None, fixture=name,
        sources=[f"`fixtures/phase5/{name}.json`: {p5['about']}",
                 f"`fixtures/phase4/{p5['phase4_fixture']}.json` (the workload model)"])
