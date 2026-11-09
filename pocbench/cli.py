"""pocbench command line."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from pocbench import (
    candidate,
    config,
    corpus,
    cost,
    criteria,
    fixtures,
    gate,
    harness,
    loadgen,
    phase1,
    phase1_report,
    phase3,
    phase3_report,
    phase4,
    phase5_fixtures,
    record,
    results,
    slices,
    spike,
)
from pocbench.neon import Neon, Variant


def _cmd_fingerprint(_args) -> int:
    for pinned in (config.load_model(), config.config_space()):
        print(f"{pinned.sha256}  {pinned.path.relative_to(config.REPO_ROOT)}")
    return 0


def _cmd_cost(args) -> int:
    lm, space = config.load_model(), config.config_space()
    text = cost.render(lm.data, space.data, lm.sha256, space.sha256)
    if args.write:
        Path(args.write).write_text(text)
    else:
        print(text)
    return 0


def _cmd_spike_load(args) -> int:
    with psycopg.connect(args.dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute(spike.SCHEMA)
        # Neon's file-cache view (cache evidence, ADR 0013) needs the `neon`
        # extension. Stock Postgres has no such extension; evidence is then
        # recorded as unavailable.
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS neon")
        except psycopg.errors.FeatureNotSupported:
            print("neon extension unavailable (not Neon); file-cache evidence will be null")
        for sql in spike.LOAD:
            conn.execute(sql)
        size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print(f"spike dataset loaded: {size.fetchone()[0]}")
    return 0


def _cmd_run(args) -> int:
    lm = config.load_model()
    spec = loadgen.RunSpec.from_load_model(
        lm.data, lm.sha256, dsn=args.dsn, label=args.label, sessions=args.sessions,
        cache_state=args.cache_state, warmup_s=args.warmup, measure_s=args.measure)
    summary = loadgen.run(spec, spike.instances(seed=lm.data["mix"]["seed"]), Path(args.out))
    print(json.dumps({k: summary[k] for k in ("overall", "throughput_qps", "probe", "aborted")},
                     indent=2))
    return 1 if summary["aborted"] or summary["failed_to_connect"] else 0


def _cmd_gate(args) -> int:
    result = gate.run_gate(Path(args.out), args.database, args.role, measure_s=args.measure)
    print(json.dumps(result.get("verdict"), indent=2))
    return 0 if "delete_failures" not in result else 1


def _cmd_gate_report(args) -> int:
    result = json.loads(Path(args.json).read_text())
    text = gate.render(result, args.run_url)
    if args.write:
        Path(args.write).write_text(text)
    else:
        print(text)
    return 0


def _cmd_corpus_load(args) -> int:
    out = Path(args.out)
    if args.dsn:  # local Postgres: test scale unless --full
        space = config.config_space().data
        spec = corpus.CorpusSpec.from_config(space) if args.full else corpus.TEST_SPEC
        record = corpus.load(args.dsn, spec, fresh=args.fresh)
        phase1._write(out / "corpus.json", record)
    else:
        record = phase1.run_corpus_load(out, args.database, args.role, args.fresh)
    print(json.dumps({k: record[k] for k in ("database_bytes", "load_seconds")}, indent=2))
    return 0


def _cmd_profile(args) -> int:
    out = Path(args.out)
    if args.dsn:  # local Postgres: same sequence, one compute size
        lm, sp = config.load_model(), config.config_space()
        cap = dict(sp.data["capture"])
        if args.measure:
            cap["measure_seconds"] = args.measure
        report = phase1.profile_core(args.dsn, out, lm, sp, cap)
        valid = report["capture"]["stats_validity"]["valid"]
    else:
        result = phase1.run_profile(out, args.database, args.role, measure_s=args.measure)
        valid = result.get("stats_valid", False) and "delete_failures" not in result
    print(f"profile written to {out}; capture statistics valid: {valid}")
    return 0 if valid else 1


def _cmd_tier_probe(args) -> int:
    result = phase1.run_tier_probe(Path(args.out), args.database, args.role)
    return 0 if "delete_failures" not in result else 1


def _cmd_profile_report(args) -> int:
    text = phase1_report.render(Path(args.results), args.run_url)
    if args.write:
        Path(args.write).write_text(text)
    else:
        print(text)
    return 0


def _cmd_criteria_freeze(args) -> int:
    doc = criteria.build()
    sha = criteria.freeze(doc)
    Path(args.write).write_text(criteria.render(doc, sha))
    print(f"criteria frozen: {len(doc['criteria'])} criteria, sha256 {sha}")
    return 0


def _cmd_verify_criteria(_args) -> int:
    try:
        doc = criteria.load_verified()
    except criteria.CriteriaPinError as e:
        print(e, file=sys.stderr)
        return 1
    sha = criteria.sha256_file(criteria.CRITERIA_PATH)
    print(f"criteria pin OK: {len(doc['criteria'])} criteria, sha256 {sha}, "
          f"{len(doc['pins'])} pinned inputs unchanged")
    return 0


def _cmd_harness_baselines(args) -> int:
    levels = [int(x) for x in args.levels.split(",")] if args.levels else None
    try:
        result = phase3.run_baselines(Path(args.out), args.database, args.role, levels)
    except harness.NetworkRejected as e:
        print(f"network rejected: {e}", file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("preflight", "pooled_cells")}, indent=2,
                     default=str))
    return 0 if "delete_failures" not in result else 1


def _cmd_harness_eval(_args) -> int:
    try:
        frozen = phase3.run_evaluation()
    except candidate.NotFrozenError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"frozen {frozen['kind']} {frozen['sha256'][:12]} verified; `pocbench eval-run` "
          "runs the held-out evaluation")
    return 0


def _cmd_harness_try(args) -> int:
    """Development aid: one short run on the profiling slice, printed and
    written to a scratch directory. It never opens the record, and the
    record would refuse its rows anyway (shortened window)."""
    lm = config.load_model()
    if args.measure >= lm.data["timing"]["measure_seconds"]:
        print("harness-try is for short looks; scored runs go through the entry points",
              file=sys.stderr)
        return 2
    doc = criteria.load_verified()
    with psycopg.connect(args.dsn, autocommit=True, connect_timeout=30) as conn:
        rebuilt = slices.rebuild(conn)
    si = rebuilt.profiling()
    slices.guard_profiling(si, rebuilt)
    spec = loadgen.RunSpec.from_load_model(
        lm.data, lm.sha256, dsn=args.dsn, label="dev-aid", sessions=args.sessions,
        cache_state="warm", warmup_s=args.warmup, measure_s=args.measure)
    out = Path(args.out)
    summary = loadgen.run(spec, list(si.instances), out)
    rows, probe = harness.read_raw(out)
    verdict = harness.probe_verdict(
        [float(r["latency_ms"]) for r in probe if r["outcome"] == loadgen.OK],
        sum(r["outcome"] != loadgen.OK for r in probe),
        int(args.measure * 1000 / lm.data["probe"]["interval_ms"]),
        doc["method"]["latency_basis"]["run_valid_if"])
    run = harness.summarize_run(rows, verdict["p50_ms"], args.measure)
    print(json.dumps({"probe": verdict, "overall": run["overall"],
                      "aborted": summary["aborted"]}, indent=2, default=str))
    return 0


def _cmd_harness_report(args) -> int:
    run_dir = Path(args.run_dir)
    if args.results:
        phase3_report.publish(run_dir, Path(args.results))
    text = phase3_report.render(run_dir, args.run_url)
    if args.write:
        Path(args.write).write_text(text)
    else:
        print(text)
    return 0


def _cmd_cleanup_branches(args) -> int:
    """Safety net after a cancelled job: delete branches named with a run
    prefix. The corpus and default branches never match a prefix."""
    if not re.fullmatch(r"p\d+-", args.prefix):
        print("prefix must look like p3-", file=sys.stderr)
        return 2
    neon = Neon()
    gone = []
    for name, bid in neon.branch_names().items():
        if name.startswith(args.prefix):
            neon.delete_variant(Variant(bid, "", 0, name))
            gone.append(name)
    print(f"deleted {len(gone)} branches: {gone}")
    return 0


def _deadline(args) -> float:
    return time.time() + args.minutes * 60


def _cmd_loop_run(args) -> int:
    try:
        result = phase4.run_loop_job(args.database, args.role, _deadline(args))
    except harness.NetworkRejected as e:
        print(f"network rejected: {e}", file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("state", "frozen", "preflight")}, indent=2,
                     default=str))
    return 0 if "delete_failures" not in result else 1


def _cmd_eval_run(args) -> int:
    try:
        result = phase4.run_eval_job(args.database, args.role, _deadline(args))
    except candidate.NotFrozenError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except harness.NetworkRejected as e:
        print(f"network rejected: {e}", file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("state", "cells_run", "cells_remaining")},
                     indent=2))
    return 0 if "delete_failures" not in result else 1


def _cmd_eval_report(_args) -> int:
    print(json.dumps(phase4.eval_report(), indent=2))
    return 0


def _cmd_phase4_next(_args) -> int:
    print(phase4.next_task())
    return 0


def _emit(md: str, write: str | None) -> int:
    if write:
        Path(write).parent.mkdir(parents=True, exist_ok=True)
        Path(write).write_text(md)
        print(f"wrote {write}")
    else:
        print(md)
    return 0


def _cmd_results_doc(args) -> int:
    """Phase 5: the results document from the committed Phase 4 run."""
    return _emit(results.render(results.load_real()), args.write)


def _cmd_results_fixture(args) -> int:
    """Phase 5: a results document from a fixture with planted bad news."""
    return _emit(results.render(phase5_fixtures.evidence(args.name)), args.write)


def _cmd_loop_fixture(args) -> int:
    """Run the loop over a fixture workload (no Neon, no model) and print its
    output and whether it matches the fixture's expectation."""
    fx = fixtures.load(args.name)
    out = Path(args.out) / f"{args.name}-loop-log.jsonl"
    out.unlink(missing_ok=True)
    _, decision, runner = fixtures.run_fixture(fx, criteria.load_verified(),
                                               config.config_space().data, out)
    exp = fx["expected"]
    got = {"kind": decision["kind"], "experiments": runner.ran,
           **({"class": decision["class"], "shapes": decision["shapes"]}
              if decision["kind"] == "refusal" else {"bundle": decision["config"]["name"]})}
    ok = decision["kind"] == exp["kind"] and (
        decision.get("class") == exp.get("class") if exp["kind"] == "refusal"
        else decision["config"]["name"] == "x-" + exp["bundle"].replace(".", "_"))
    print(json.dumps({"fixture": args.name, "expected": exp, "got": got, "match": ok,
                      "loop_log": str(out)}, indent=2))
    return 0 if ok else 1


def _cmd_verify_record(args) -> int:
    try:
        rows = record.verify(Path(args.path))
    except record.RecordError as e:
        print(e, file=sys.stderr)
        return 1
    runs = [r for r in rows if r.get("kind") == "run"]
    print(f"record OK: {len(runs)} runs ({sum(r['status'] == record.SCORED for r in runs)} "
          f"scored), chain intact")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="pocbench")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fingerprint", help="print the sha256 of the pinned config files")

    c = sub.add_parser("cost", help="render the build-cost estimate")
    c.add_argument("--write", help="write markdown here instead of stdout")

    s = sub.add_parser("spike-load", help="create and load the Phase 0 spike dataset")
    s.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))

    r = sub.add_parser("run", help="one closed-loop run of the spike workload")
    r.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    r.add_argument("--label", required=True)
    r.add_argument("--sessions", type=int, required=True)
    r.add_argument("--cache-state", default="warm")
    r.add_argument("--warmup", type=float, help="override (dev only; never in a scored run)")
    r.add_argument("--measure", type=float, help="override (dev only; never in a scored run)")
    r.add_argument("--out", required=True)

    g = sub.add_parser("gate", help="Phase 0 noise gate on Neon (creates and deletes branches)")
    g.add_argument("--out", default="runs/phase0")
    g.add_argument("--database", default=os.environ.get("NEON_DATABASE", "neondb"))
    g.add_argument("--role", default=os.environ.get("NEON_ROLE", "neondb_owner"))
    g.add_argument("--measure", type=float, help="override (dev only; never in a scored run)")

    gr = sub.add_parser("gate-report", help="render gate.json as markdown")
    gr.add_argument("--json", default="results/phase0/gate.json")
    gr.add_argument("--run-url")
    gr.add_argument("--write")

    neon_args = {"database": os.environ.get("NEON_DATABASE", "neondb"),
                 "role": os.environ.get("NEON_ROLE", "neondb_owner")}

    cl = sub.add_parser("corpus-load", help="Phase 1 dataset: Neon corpus branch, or --dsn")
    cl.add_argument("--dsn", help="local Postgres instead of the Neon corpus branch")
    cl.add_argument("--full", action="store_true", help="with --dsn: full size, not test size")
    cl.add_argument("--fresh", action="store_true", help="drop and reload instead of resuming")
    cl.add_argument("--out", default="runs/phase1")
    cl.set_defaults(**neon_args)

    pr = sub.add_parser("profile", help="Phase 1 split, capture and profile")
    pr.add_argument("--dsn", help="local Postgres (adds indexes to it) instead of Neon")
    pr.add_argument("--out", default="runs/phase1")
    pr.add_argument("--measure", type=float, help="override the capture window (dev only)")
    pr.set_defaults(**neon_args)

    tp = sub.add_parser("tier-probe", help="settings and parallel workers per compute tier")
    tp.add_argument("--out", default="runs/phase1")
    tp.set_defaults(**neon_args)

    rp = sub.add_parser("profile-report", help="render Phase 1 results as markdown")
    rp.add_argument("--results", default="results/phase1")
    rp.add_argument("--run-url")
    rp.add_argument("--write")

    cf = sub.add_parser("criteria-freeze", help="Phase 2: generate and pin the success criteria")
    cf.add_argument("--write", default="docs/criteria.md")
    sub.add_parser("verify-criteria", help="fail if the criteria or a pinned input changed")

    hb = sub.add_parser("harness-baselines",
                        help="Phase 3: both baselines through the development entry point")
    hb.add_argument("--out", default="runs/phase3")
    hb.add_argument("--levels", help="comma-separated load-model levels (default: protocol.dev)")
    hb.set_defaults(**neon_args)

    sub.add_parser("harness-eval",
                   help="evaluation entry point: refuses without a frozen candidate or refusal")

    ht = sub.add_parser("harness-try", help="dev aid: one short profiling-slice run, no record")
    ht.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ht.add_argument("--sessions", type=int, default=8)
    ht.add_argument("--warmup", type=float, default=5)
    ht.add_argument("--measure", type=float, default=30)
    ht.add_argument("--out", default="runs/scratch")

    hr = sub.add_parser("harness-report", help="verify, pool and render a Phase 3 run")
    hr.add_argument("--run-dir", default="runs/phase3")
    hr.add_argument("--results", help="also copy record, job and pooled cells here")
    hr.add_argument("--run-url")
    hr.add_argument("--write")

    cb = sub.add_parser("cleanup-branches", help="delete leftover run branches by prefix")
    cb.add_argument("--prefix", required=True)

    vr = sub.add_parser("verify-record", help="check a results record's hash chain")
    vr.add_argument("path")

    lr = sub.add_parser("loop-run", help="Phase 4: run the adaptive loop until stop or deadline")
    lr.add_argument("--minutes", type=float, default=320, help="job time available")
    lr.set_defaults(**neon_args)
    er = sub.add_parser("eval-run", help="Phase 4: held-out evaluation, resumable; refuses "
                                         "without a frozen candidate or refusal")
    er.add_argument("--minutes", type=float, default=320, help="job time available")
    er.set_defaults(**neon_args)
    sub.add_parser("eval-report", help="Phase 4: pool every evaluation run and apply criteria")
    sub.add_parser("phase4-next", help="print the next Phase 4 task from committed state")
    rd = sub.add_parser("results-doc", help="Phase 5: the results document from the committed "
                        "run (verifies every input)")
    rd.add_argument("--write", help="write markdown here (docs/results.md)")
    rf = sub.add_parser("results-fixture", help="Phase 5: a results document from a fixture")
    rf.add_argument("name", choices=list(phase5_fixtures.NAMES))
    rf.add_argument("--write", help="write markdown here (docs/fixtures/results-<name>.md)")
    lf = sub.add_parser("loop-fixture", help="run the loop over a fixture workload, offline")
    lf.add_argument("name", choices=["qualifying", "refusal-criteria-unmet"])
    lf.add_argument("--out", default="runs/fixtures")

    args = p.parse_args(argv)
    if args.cmd in ("spike-load", "run", "harness-try") and args.dsn is None:
        p.error("--dsn or DATABASE_URL is required")
    handlers = {"fingerprint": _cmd_fingerprint, "cost": _cmd_cost,
                "spike-load": _cmd_spike_load, "run": _cmd_run, "gate": _cmd_gate,
                "gate-report": _cmd_gate_report, "corpus-load": _cmd_corpus_load,
                "profile": _cmd_profile, "tier-probe": _cmd_tier_probe,
                "profile-report": _cmd_profile_report,
                "criteria-freeze": _cmd_criteria_freeze, "verify-criteria": _cmd_verify_criteria,
                "harness-baselines": _cmd_harness_baselines, "harness-eval": _cmd_harness_eval,
                "harness-try": _cmd_harness_try, "harness-report": _cmd_harness_report,
                "verify-record": _cmd_verify_record, "cleanup-branches": _cmd_cleanup_branches,
                "loop-run": _cmd_loop_run, "eval-run": _cmd_eval_run,
                "eval-report": _cmd_eval_report, "phase4-next": _cmd_phase4_next,
                "loop-fixture": _cmd_loop_fixture, "results-doc": _cmd_results_doc,
                "results-fixture": _cmd_results_fixture}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
