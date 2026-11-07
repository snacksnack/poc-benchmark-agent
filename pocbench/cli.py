"""pocbench command line."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from pocbench import config, corpus, cost, criteria, gate, loadgen, phase1, phase1_report, spike


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

    args = p.parse_args(argv)
    if args.cmd in ("spike-load", "run") and args.dsn is None:
        p.error("--dsn or DATABASE_URL is required")
    handlers = {"fingerprint": _cmd_fingerprint, "cost": _cmd_cost,
                "spike-load": _cmd_spike_load, "run": _cmd_run, "gate": _cmd_gate,
                "gate-report": _cmd_gate_report, "corpus-load": _cmd_corpus_load,
                "profile": _cmd_profile, "tier-probe": _cmd_tier_probe,
                "profile-report": _cmd_profile_report,
                "criteria-freeze": _cmd_criteria_freeze, "verify-criteria": _cmd_verify_criteria}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
