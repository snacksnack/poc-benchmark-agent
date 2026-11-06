"""pocbench command line."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from pocbench import config, cost, gate, loadgen, spike


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

    args = p.parse_args(argv)
    if getattr(args, "dsn", "unset") is None:
        p.error("--dsn or DATABASE_URL is required")
    handlers = {"fingerprint": _cmd_fingerprint, "cost": _cmd_cost,
                "spike-load": _cmd_spike_load, "run": _cmd_run, "gate": _cmd_gate}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
