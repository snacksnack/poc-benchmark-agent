"""Phase 4 on Neon (RC1-632): the adaptive loop, the freeze, and the held-out
evaluation, each resumable across GitHub jobs (R15, ADR 0024).

State lives in `results/phase4/` and is committed by the workflow after every
job, so the next job continues from it:

- `loop-log.jsonl`: the loop's hash-chained log (pocbench.loop).
- `dev-record.jsonl`: the development results record (append-only, ADR 0020).
- `dev-raw-naive/`: the naive baseline's raw logs, gzipped. The loop pools
  naive over every development repetition so far (ADR 0022); candidate raw
  logs are large and go to the job's artifact, their hashes in the record.
- `experiments/<name>.json`: each experiment's pooled cells and prediction.
- `eval-record.jsonl`, `jobs.jsonl`: the evaluation record and a line per
  job (its run id and artifact, so the final pooling can fetch every raw
  log).

`candidates/frozen.json` is written once, by the decision after the loop
stops; evaluation refuses until it exists.
"""

import gzip
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import psycopg

from pocbench import (
    candidate,
    config,
    criteria,
    harness,
    loop,
    phase1,
    phase3,
    profile,
    record,
    slices,
    space,
    split,
    verdicts,
)
from pocbench.neon import Neon, Variant
from pocbench.proposer import MODEL, ClaudeProposer

OUT = Path("runs/phase4")
STATE = config.REPO_ROOT / "results" / "phase4"
LOOP_LOG = STATE / "loop-log.jsonl"
DEV_RECORD = STATE / "dev-record.jsonl"
NAIVE_RAW = STATE / "dev-raw-naive"
EXPERIMENTS = STATE / "experiments"
EVAL_RECORD = STATE / "eval-record.jsonl"
JOBS = STATE / "jobs.jsonl"
EVAL_CELLS = STATE / "eval-cells.json"
EVAL_VERDICTS = STATE / "eval-verdicts.json"
BRANCH_PREFIX = "p4-"
# Phase 3 measured 24 runs in 2.5 h including restarts, filler scans and
# probes: 375 s per run. Experiments and job time are planned from it.
SECONDS_PER_RUN = 375
SETUP_HOURS = 0.08  # branch, resize, filler
TIME_MARGIN = 1.15


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _profile() -> dict:
    return json.loads((config.REPO_ROOT / criteria.PINNED_INPUTS["profile"]).read_text())


def _rows(path: Path) -> list[dict]:
    return record.verify(path) if path.exists() else []


def _job_id() -> str:
    return os.environ.get("GITHUB_RUN_ID") or phase3._stamp()


# --------------------------------------------------------- static context

def build_seconds_by_id(prof: dict, sp: dict) -> dict[str, float]:
    """Phase 1 measured each candidate's build at the build tier; the profile
    keys them by DDL with a cand_iN name, in candidate order."""
    secs = list(prof["candidate_build_seconds"].values())
    return {ix["id"]: secs[n] for n, ix in enumerate(sp["index"])}


def static_context(doc: dict, sp: dict, prof: dict, phase3_cells: list[dict]) -> dict:
    """Everything the proposer needs that does not change during the loop."""
    goal = doc["goal"]
    ceilings = [{k: c[k] for k in ("shape", "shape_name", "level", "cache_state", "percentile",
                                   "ceiling_ms")}
                for c in doc["criteria"] if c["kind"] == "latency_ceiling"]
    kinds: dict[str, int] = {}
    for c in doc["criteria"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    builds = build_seconds_by_id(prof, sp)
    shapes = [{"id": s["shape"], "template": s["template"], "log_share": s["log_share"],
               "time_share": s["time_share"],
               "today_on_naive": {k: s["capture"][k] for k in ("p50_ms", "p99_ms")} | {
                   "timeout_fraction": round(s["capture"]["timeout_count"]
                                             / max(s["capture"]["sample_count"], 1), 3)},
               "naive_plan": s["plan"]["scans"], "selectivity": s["selectivity"]["median"]}
              for s in prof["shapes"]]
    lfc = {0.25: 0.75, 0.5: 1.5, 1: 3, 2: 6, 4: 12}
    return {
        "goal": goal["statement"], "today": goal["today"], "objective": goal["objective"],
        "criteria_counts": kinds, "latency_ceilings": ceilings,
        "no_regression_tolerance": next(c["tolerance"] for c in doc["criteria"]
                                        if c["kind"] == "no_regression"),
        "error_ceiling_fraction": next(c["max_fraction"] for c in doc["criteria"]
                                       if c["kind"] == "error_ceiling"),
        "verdict_rules": doc["method"]["verdicts"],
        "tail_support": doc["method"]["under_sampled"]["rule"],
        "prediction_note": ("development verdicts are predictions on the profiling slice with "
                            "2 repetitions; tail support is projected to the 5-repetition "
                            "evaluation (observations x 2.5, distinct instances unchanged)"),
        "tiers": [{"cu": cu, "shared_compute": cu in sp["tiers"]["shared_compute_units"],
                   "file_cache_gb_approx": lfc.get(cu), "usd_per_hour": cu * sp["cost"][
                       "cu_hour_usd"]} for cu in sp["tiers"]["compute_units"]],
        "dataset_gb": sp["dataset"]["measured_gb"],
        "index_candidates": [{"id": ix["id"], "ddl": ix["ddl"], "serves": ix["serves"],
                              "bytes": ix["measured_bytes"],
                              "build_seconds_at_4cu": builds[ix["id"]]} for ix in sp["index"]],
        "naive_baseline_indexes": prof["naive_ddl"],
        "shapes": shapes,
        "phase3_baselines_dev": [{"config": c["config"]} | s for c in phase3_cells
                                 if c["level"] in (8, 32)
                                 for s in loop.summarize_cells([c])],
        "experiment_cost": (f"each experiment runs {2 * 2 * 2 * 2} runs of about "
                            f"{SECONDS_PER_RUN / 60:.0f} minutes (candidate and naive, "
                            "interleaved); both branches stay on, so CU-hours are about "
                            "(candidate CU + 2) x 1.7 plus index builds at 4 CU"),
    }


# ----------------------------------------------------------------- the lab

class Lab:
    """Branches for one job: created from the corpus, held at a fixed size,
    deleted in a finally block (and by the workflow's cleanup step)."""

    def __init__(self, s: harness.Session, database: str, role: str):
        self.s, self.database, self.role = s, database, role
        self.neon, self.meter = Neon(), phase3.Meter()
        self.created: list[Variant] = []
        self.stamp = phase3._stamp()
        self.corpus_id = self.neon.branch_id(s.space["corpus"]["branch"])
        if self.corpus_id is None:
            raise RuntimeError("no corpus branch")
        self.preflight: dict | None = None
        self.rebuilt: slices.Rebuilt | None = None
        self.builds: dict[str, dict] = {}

    def cu_hours(self) -> float:
        return sum(self.meter.cu_hours().values())

    def target(self, cfg: harness.Config) -> phase3.NeonTarget:
        sp = self.s.space
        build_cu = sp["capture"]["build_cu"]
        safe = re.sub(r"[^a-z0-9-]+", "-", cfg.name.lower())[:40]
        v = self.neon.create_variant(f"{BRANCH_PREFIX}{safe}-{self.stamp}", build_cu,
                                     parent_id=self.corpus_id)
        self.created.append(v)
        self.meter.start(v.name, build_cu)
        self.neon.set_suspend_timeout(v.endpoint_id, -1)
        uri = self.neon.connection_uri(v.branch_id, self.database, self.role)
        if self.preflight is None:
            self.preflight = harness.preflight(uri, self.s.lm, self.s.method.probe_rule)
            harness.require_network(self.preflight)
            with psycopg.connect(uri, autocommit=True, connect_timeout=30) as conn:
                self.rebuilt = slices.rebuild(conn)
        from_profile = profile.build_indexes(uri, list(cfg.ddl)) if cfg.ddl else {}
        self.neon.resize(v.endpoint_id, cfg.cu)
        self.meter.resize(v.name, cfg.cu)
        sizes = phase3._sizes(uri)
        if not sizes["file_cache_bytes"]:
            raise RuntimeError(f"{v.name}: no file-cache limit; cannot size the filler")
        filler = harness.ensure_filler(uri, int(sizes["file_cache_bytes"]
                                                * harness.FILLER_FACTOR))
        self.builds[cfg.name] = {"branch": v.name, "cu": cfg.cu, "index_build_seconds":
                                 from_profile, "sizes": sizes, "filler": filler}
        return phase3.NeonTarget(cfg, uri, self.neon, v, self.s.lm["probe"]["query"],
                                 cfg.cu in sp["tiers"]["shared_compute_units"])

    def drop(self, t: phase3.NeonTarget) -> None:
        self.meter.stop(t.variant.name)
        try:
            self.neon.delete_variant(t.variant)
            self.created.remove(t.variant)
        except Exception as e:  # noqa: BLE001 - the finally block retries every branch
            self.builds.setdefault("_delete_failures", []).append(f"{t.variant.name}: {e}")

    def close(self, result: dict) -> None:
        for v in self.created:
            self.meter.stop(v.name)
        result["cu_hours_held"] = self.meter.cu_hours()
        phase1._cleanup(self.neon, list(self.created), result)


# ------------------------------------------------------ experiment runner

def _gzip_run(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.csv"):
        (dst / (f.name + ".gz")).write_bytes(gzip.compress(f.read_bytes(), mtime=0))


def naive_pool(s: harness.Session) -> list[dict]:
    rows = [r for r in _rows(DEV_RECORD) if r.get("kind") == "run"
            and r["config"]["name"] == "naive"]
    if not rows:
        return []
    return harness.pool(rows, [NAIVE_RAW, s.raw_root], s.method)


class NeonExperimentRunner:
    def __init__(self, s: harness.Session, lab: Lab, naive: phase3.NeonTarget, prof: dict):
        self.s, self.lab, self.naive = s, lab, naive
        self.builds = build_seconds_by_id(prof, s.space)
        dev = s.space["protocol"]["dev"]
        self.runs_per_config = len(dev["levels"]) * len(dev["cache_states"]) * dev["repetitions"]
        self.attempts = 0

    def estimate_cu_hours(self, bundle: space.Bundle) -> float:
        hours = 2 * self.runs_per_config * SECONDS_PER_RUN / 3600
        build_h = sum(self.builds[i] for i in bundle.index_ids) / 3600 + SETUP_HOURS
        return ((bundle.cu + self.naive.config.cu) * hours
                + self.s.space["capture"]["build_cu"] * build_h)

    def estimate_hours(self, bundle: space.Bundle) -> float:
        return (2 * self.runs_per_config * SECONDS_PER_RUN / 3600
                + sum(self.builds[i] for i in bundle.index_ids) / 3600 + SETUP_HOURS)

    def naive_cells(self) -> list[dict]:
        return naive_pool(self.s)

    def run(self, bundle: space.Bundle) -> loop.ExperimentResult:
        before = self.lab.cu_hours()
        self.attempts += 1
        # Unique across jobs (job id) and within one (attempt), so a re-run
        # after an aborted experiment never reuses a raw-log directory.
        self.s.run_prefix = f"j{_job_id()}-e{self.attempts:02d}-"
        first_row = len(self.s.rows)
        cfg = harness.Config(bundle.name, bundle.cu, space.ddl(bundle, self.s.space))
        target = None
        try:
            target = self.lab.target(cfg)
            harness.development(self.s, {cfg.name: target, "naive": self.naive},
                                self.lab.rebuilt)
        except harness.NetworkRejected as e:
            raise loop.ExperimentAborted(str(e), self.lab.cu_hours() - before) from e
        finally:
            if target is not None:
                self.lab.drop(target)
            # Scored naive rows are in the record even if the experiment
            # aborted; keep their logs so later jobs can pool them.
            for r in self.s.rows[first_row:]:
                if r["config"]["name"] == "naive" and r["status"] == record.SCORED:
                    _gzip_run(self.s.raw_root / r["raw"]["dir"], NAIVE_RAW / r["raw"]["dir"])
        rows = self.s.rows[first_row:]
        cells = harness.pool([r for r in rows if r["config"]["name"] == cfg.name],
                             self.s.raw_root, self.s.method)
        naive_cells = self.naive_cells()
        cu_hours = self.lab.cu_hours() - before
        run_ids = [r["run_id"] for r in rows]
        _write(EXPERIMENTS / f"{bundle.name}.json",
               {"bundle": bundle.key, "name": bundle.name, "cu": bundle.cu,
                "index_ids": list(bundle.index_ids), "cells": cells,
                "naive_cells_at_the_time": naive_cells, "cu_hours": cu_hours,
                "run_ids": run_ids, "build": self.lab.builds.get(cfg.name),
                "artifact_job": _job_id()})
        return loop.ExperimentResult(bundle, split.PROFILING, cells, naive_cells,
                                     cu_hours, run_ids,
                                     {"build": self.lab.builds.get(cfg.name)})


# ------------------------------------------------------------ loop job

def _header(s: harness.Session, budgets: loop.Budgets) -> dict:
    return {"criteria_sha256": s.method.criteria_sha256,
            "config_space_sha256": s.space_sha256, "load_model_sha256": s.lm_sha256,
            "model": MODEL, "budgets": budgets.__dict__}


def loop_config(s: harness.Session, doc: dict, static: dict,
                may_start=lambda b, est: True) -> loop.LoopConfig:
    sp = s.space
    return loop.LoopConfig(doc=doc, space=sp,
                           error_ceiling=s.lm["errors"]["error_ceiling_fraction"],
                           static_context=static, budgets=loop.Budgets.from_space(sp),
                           may_start=may_start,
                           support_scale=sp["protocol"]["eval"]["repetitions"]
                           / sp["protocol"]["dev"]["repetitions"])


def decide_and_freeze(cfg: loop.LoopConfig, log: loop.LoopLog, s: harness.Session) -> dict:
    def cells_of(name: str) -> list[dict]:
        return json.loads((EXPERIMENTS / f"{name}.json").read_text())["cells"]

    out = loop.decide(cfg, log, cells_of, naive_pool(s), _sha(DEV_RECORD))
    sha = candidate.freeze(out)
    log.append({"kind": "frozen", "output": out["kind"], "frozen_sha256": sha,
                "id": out.get("refusal_id") or out["config"]["name"],
                **({"class": out["class"]} if out["kind"] == "refusal" else {})})
    return {"kind": out["kind"], "sha256": sha}


def run_loop_job(database: str, role: str, deadline: float) -> dict:
    s, doc = harness.open_session("development", OUT / "raw", DEV_RECORD)
    result: dict = {"task": "loop", "job": _job_id(), "started_at": phase3._stamp()}
    if candidate.FROZEN_PATH.exists():
        return result | {"state": "frozen"}
    prof = _profile()
    p3 = json.loads((config.REPO_ROOT / "results/phase3/baselines.json").read_text())
    static = static_context(doc, s.space, prof, p3)
    budgets = loop.Budgets.from_space(s.space)
    log = loop.LoopLog(LOOP_LOG, _header(s, budgets))
    if loop.terminal(log) is not None:  # stopped in an earlier job, never decided
        return result | {"state": "stopped", "frozen": decide_and_freeze(
            loop_config(s, doc, static), log, s)}
    lab = Lab(s, database, role)
    try:
        base = {c.name: c for c in harness.baselines(doc, prof)}
        naive = lab.target(base["naive"])
        result["preflight"] = lab.preflight
        runner = NeonExperimentRunner(s, lab, naive, prof)

        def may_start(bundle: space.Bundle, _est: float) -> bool:
            return time.time() + runner.estimate_hours(bundle) * 3600 * TIME_MARGIN < deadline

        cfg = loop_config(s, doc, static, may_start)
        out = loop.run_loop(cfg, log, ClaudeProposer(static), runner)
        result["state"] = out["state"]
        if out["state"] == "stopped":
            result["frozen"] = decide_and_freeze(cfg, log, s)
    except harness.NetworkRejected as e:
        result["network_rejected"] = str(e)
        raise
    except loop.ExperimentAborted as e:
        # Logged by the loop with its compute; a network cause, so the job
        # exits like a rejected network and the next runner retries.
        result["network_rejected"] = str(e)
        raise harness.NetworkRejected(str(e)) from e
    finally:
        result["builds"] = lab.builds
        lab.close(result)
        result["finished_at"] = phase3._stamp()
        with JOBS.open("a") as fh:
            fh.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    return result


# ----------------------------------------------------------- evaluation

def eval_state(doc: dict, frozen: candidate.Frozen, m: harness.Method) -> tuple[int, int]:
    """(cells planned, cells scored so far)."""
    names = [c.name for c in harness.eval_configs(frozen, harness.baselines(doc, _profile()))]
    plan = harness.eval_schedule(sorted(names), m, ["held_out", "profiling"])
    done = harness.done_eval_cells(_rows(EVAL_RECORD))
    return len(plan), len(set(plan) & done)


def run_eval_job(database: str, role: str, deadline: float) -> dict:
    # The gate first: nothing is opened or created without a verified freeze.
    frozen = harness.require_frozen(harness.criteria_sha256(criteria.load_verified()))
    s, doc = harness.open_session("evaluation", OUT / "eval-raw", EVAL_RECORD)
    s.run_prefix = f"j{_job_id()}-"
    result: dict = {"task": "evaluate", "job": _job_id(), "started_at": phase3._stamp(),
                    "artifact": f"evaluate-{_job_id()}",
                    "frozen": {"kind": frozen.kind, "sha256": frozen.sha256}}
    done = harness.done_eval_cells(_rows(EVAL_RECORD))
    configs = harness.eval_configs(frozen, harness.baselines(doc, _profile()))
    lab = Lab(s, database, role)
    try:
        targets = {c.name: lab.target(c) for c in configs}
        result["preflight"] = lab.preflight
        per_cell = SECONDS_PER_RUN * TIME_MARGIN
        out = harness.evaluation(s, frozen, targets, lab.rebuilt, done,
                                 may_start=lambda c: time.time() + per_cell < deadline)
        result |= {"state": "paused" if out["cells_remaining"] else "complete",
                   "cells_run": out["cells_run_this_job"],
                   "cells_remaining": out["cells_remaining"],
                   "rejected_runs": out["rejected_runs"]}
    except harness.NetworkRejected as e:
        result["network_rejected"] = str(e)
        raise
    finally:
        result["builds"] = lab.builds
        lab.close(result)
        result["finished_at"] = phase3._stamp()
        with JOBS.open("a") as fh:
            fh.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    return result


def fetch_eval_raw(dest: Path, repo: str | None = None) -> list[Path]:
    """Every earlier evaluation job's raw logs, from its artifact."""
    roots = []
    for line in JOBS.read_text().splitlines() if JOBS.exists() else []:
        j = json.loads(line)
        if j.get("task") != "evaluate" or not j.get("cells_run"):
            continue
        d = dest / j["job"]
        if not d.exists() and j["job"] != os.environ.get("GITHUB_RUN_ID"):
            cmd = ["gh", "run", "download", j["job"], "-n", j["artifact"], "-D", str(d)]
            if repo:
                cmd += ["-R", repo]
            subprocess.run(cmd, check=True)
        # The workflow uploads runs/, so an artifact holds phase4/eval-raw/.
        roots.append(d / "phase4" / "eval-raw")
    return roots


def eval_report(fetch_dir: Path = OUT / "fetched") -> dict:
    """Pool every scored evaluation run, both slices, and apply the frozen
    criteria: held-out verdicts are the scored result, profiling verdicts sit
    beside them for the gap (epic rule 3)."""
    doc = criteria.load_verified()
    frozen = harness.require_frozen(harness.criteria_sha256(doc))
    s, _ = harness.open_session("evaluation", OUT / "eval-raw", EVAL_RECORD)
    planned, scored = eval_state(doc, frozen, s.method)
    if scored < planned:
        raise RuntimeError(f"evaluation incomplete: {scored} of {planned} cells scored")
    roots = [s.raw_root] + fetch_eval_raw(fetch_dir)
    cells = harness.pool(_rows(EVAL_RECORD), roots, s.method)
    _write(EVAL_CELLS, cells)
    err = s.lm["errors"]["error_ceiling_fraction"]
    names = ([frozen.doc["config"]["name"]] if frozen.kind == "candidate"
             else ["default", "naive"])
    out = {"frozen": {"kind": frozen.kind, "sha256": frozen.sha256}, "verdicts": {}}
    for sl in ("held_out", "profiling"):
        sub = [c for c in cells if c["slice"] == sl]
        out["verdicts"][sl] = {n: verdicts.evaluate(doc, sub, n, error_ceiling_fraction=err,
                                                    basis=f"{sl} slice, evaluation protocol")
                               for n in names}
    _write(EVAL_VERDICTS, out)
    return {"cells": len(cells), "planned": planned,
            "held_out_all_met": {n: v["all_met"] for n, v in out["verdicts"]["held_out"].items()}}


# ------------------------------------------------------------- next task

def next_task() -> str:
    """What the next job should do, from committed state alone."""
    if not candidate.FROZEN_PATH.exists():
        return "loop"
    doc = criteria.load_verified()
    frozen = harness.require_frozen(harness.criteria_sha256(doc))
    lm = config.load_model().data
    m = harness.Method.from_criteria(doc, frozen.doc["criteria_sha256"], lm)
    planned, scored = eval_state(doc, frozen, m)
    if scored < planned:
        return "evaluate"
    if not EVAL_VERDICTS.exists():
        return "eval-report"
    return "done"
