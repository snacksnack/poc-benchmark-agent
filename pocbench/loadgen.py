"""Closed-loop load generator (ADR 0004, ADR 0014).

One OS process per session, each holding one dedicated connection, so a busy
client never queues one session behind another's Python work. Each session
sends its next query only after the previous one returns. Every query is a
result: ok, server timeout (statement_timeout), client timeout, connection
error, or error, with its SQLSTATE.

A separate probe process runs the load model's null query on its own
connection at a fixed interval, so network and client jitter can be read apart
from database time. Probe latencies never enter the workload percentiles.
"""

import asyncio
import csv
import json
import multiprocessing
import platform
import random
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg

from pocbench import stats

OK, TIMEOUT, CLIENT_TIMEOUT, CONN_ERROR, ERROR = (
    "ok", "timeout", "client_timeout", "connection_error", "error",
)
TIMEOUT_OUTCOMES = {TIMEOUT, CLIENT_TIMEOUT}
ERROR_OUTCOMES = {CONN_ERROR, ERROR}
# `instance` is the index into the run's instance list, so a tail can be
# checked for how many distinct instances it was drawn from (ADR 0018).
FIELDS = ["phase", "shape", "instance", "start_ms", "latency_ms", "outcome", "sqlstate"]
ABORT_MIN_SAMPLES = 200
EXIT_ABORTED = 3
EXIT_CONNECT_FAILED = 4

CACHE_EVIDENCE_SQL = {
    "pg_stat_database": (
        "SELECT blks_hit, blks_read, stats_reset FROM pg_stat_database "
        "WHERE datname = current_database()"
    ),
    # A changed start time means the compute restarted (or suspended and
    # woke) inside the run: its statistics and cache state are void (R6).
    "server_start": "SELECT pg_postmaster_start_time() AS started_at",
    # Neon's local file cache; absent on stock Postgres, recorded as null.
    "neon_stat_file_cache": "SELECT * FROM neon_stat_file_cache",
}
SERVER_FACTS_SQL = {
    "version": "SELECT version()",
    "shared_buffers": "SHOW shared_buffers",
    "effective_cache_size": "SHOW effective_cache_size",
    "max_connections": "SHOW max_connections",
}


@dataclass(frozen=True)
class Instance:
    shape: str
    sql: str
    params: tuple


@dataclass(frozen=True)
class RunSpec:
    dsn: str
    label: str
    sessions: int
    warmup_s: float
    measure_s: float
    timeout_ms: int
    grace_ms: int
    backoff_ms: int
    seed: int
    abort_fraction: float
    error_ceiling: float
    min_samples_for_p99: int
    probe_query: str
    probe_interval_ms: int
    cache_state: str
    load_model_sha256: str

    @classmethod
    def from_load_model(cls, lm: dict, sha: str, *, dsn: str, label: str, sessions: int,
                        cache_state: str, warmup_s: float | None = None,
                        measure_s: float | None = None) -> "RunSpec":
        t = lm["timing"]
        return cls(
            dsn=dsn, label=label, sessions=sessions,
            warmup_s=t["warmup_seconds"] if warmup_s is None else warmup_s,
            measure_s=t["measure_seconds"] if measure_s is None else measure_s,
            timeout_ms=t["per_query_timeout_ms"], grace_ms=t["client_grace_ms"],
            backoff_ms=lm["connections"]["reconnect_backoff_ms"], seed=lm["mix"]["seed"],
            abort_fraction=lm["errors"]["abort_run_fraction"],
            error_ceiling=lm["errors"]["error_ceiling_fraction"],
            min_samples_for_p99=lm["report"]["min_samples_for_p99"],
            probe_query=lm["probe"]["query"], probe_interval_ms=lm["probe"]["interval_ms"],
            cache_state=cache_state, load_model_sha256=sha,
        )


async def _connect(spec: RunSpec) -> psycopg.AsyncConnection:
    conn = await psycopg.AsyncConnection.connect(spec.dsn, autocommit=True, connect_timeout=15)
    await conn.execute(f"SET statement_timeout = {int(spec.timeout_ms)}")
    return conn


async def _close_quietly(conn: psycopg.AsyncConnection | None) -> None:
    if conn is None:
        return
    try:
        await asyncio.wait_for(conn.close(), timeout=2)
    except Exception:  # noqa: BLE001 - the connection is being discarded either way
        pass


async def _execute(conn: psycopg.AsyncConnection, inst: Instance) -> None:
    async with conn.cursor() as cur:
        await cur.execute(inst.sql, inst.params)
        if cur.description is not None:
            await cur.fetchall()


async def _session(spec: RunSpec, idx: int, instances: list[Instance], start_at: float,
                   out: Path) -> int:
    try:
        conn = await _connect(spec)
    except psycopg.Error:
        return EXIT_CONNECT_FAILED
    rng = random.Random(f"{spec.seed}:{idx}")
    measure_at = start_at + spec.warmup_s
    end_at = measure_at + spec.measure_s
    deadline = (spec.timeout_ms + spec.grace_ms) / 1000
    measured = failed = 0
    aborted = False
    await asyncio.sleep(max(0.0, start_at - time.time()))
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        while (now := time.time()) < end_at:
            phase = "warmup" if now < measure_at else "measure"
            # randrange draws exactly as choice() did, so runs stay reproducible.
            idx = rng.randrange(len(instances))
            inst = instances[idx]
            t0 = time.perf_counter()
            outcome, sqlstate, reconnect = OK, "", False
            try:
                await asyncio.wait_for(_execute(conn, inst), timeout=deadline)
            except psycopg.errors.QueryCanceled as e:
                outcome, sqlstate = TIMEOUT, e.sqlstate or ""
            except TimeoutError:
                outcome, reconnect = CLIENT_TIMEOUT, True
            except psycopg.OperationalError as e:
                outcome, sqlstate, reconnect = CONN_ERROR, e.sqlstate or "", True
            except psycopg.Error as e:
                outcome, sqlstate = ERROR, e.sqlstate or ""
            latency_ms = (time.perf_counter() - t0) * 1000
            writer.writerow([phase, inst.shape, idx, round((now - measure_at) * 1000, 3),
                             round(latency_ms, 3), outcome, sqlstate])
            if phase == "measure":
                measured += 1
                failed += outcome != OK
                if measured >= ABORT_MIN_SAMPLES and failed / measured > spec.abort_fraction:
                    aborted = True
                    break
            if reconnect:
                await _close_quietly(conn)
                conn = None
                while conn is None and time.time() < end_at:
                    await asyncio.sleep(spec.backoff_ms / 1000)
                    try:
                        conn = await _connect(spec)
                    except psycopg.Error:
                        conn = None
                if conn is None:
                    break
    await _close_quietly(conn)
    return EXIT_ABORTED if aborted else 0


async def _probe(spec: RunSpec, start_at: float, out: Path) -> int:
    try:
        conn = await _connect(spec)
    except psycopg.Error:
        return EXIT_CONNECT_FAILED
    measure_at = start_at + spec.warmup_s
    end_at = measure_at + spec.measure_s
    await asyncio.sleep(max(0.0, measure_at - time.time()))
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        while (now := time.time()) < end_at:
            t0 = time.perf_counter()
            outcome = OK
            try:
                await asyncio.wait_for(conn.execute(spec.probe_query), timeout=5)
            except (psycopg.Error, TimeoutError):
                outcome = ERROR
            writer.writerow(["measure", "probe", "", round((now - measure_at) * 1000, 3),
                             round((time.perf_counter() - t0) * 1000, 3), outcome, ""])
            await asyncio.sleep(max(0.0, spec.probe_interval_ms / 1000 - (time.time() - now)))
    await _close_quietly(conn)
    return 0


def _session_entry(spec, idx, instances, start_at, out):
    sys.exit(asyncio.run(_session(spec, idx, instances, start_at, out)))


def _probe_entry(spec, start_at, out):
    sys.exit(asyncio.run(_probe(spec, start_at, out)))


def snapshot(dsn: str, queries: dict[str, str]) -> dict:
    snap: dict = {}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        for name, sql in queries.items():
            try:
                cur = conn.execute(sql)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
                snap[name] = [dict(zip(cols, map(_jsonable, r), strict=True)) for r in rows]
            except psycopg.Error as e:
                snap[name] = {"unavailable": e.sqlstate}
    return snap


def _jsonable(v):
    return v if isinstance(v, (int, float, str, type(None))) else str(v)


def run(spec: RunSpec, instances: list[Instance], out_dir: Path) -> dict:
    """Execute one measured run and write raw logs plus summary.json."""
    if not instances:
        raise ValueError("no query instances to run")
    out_dir.mkdir(parents=True, exist_ok=True)
    server = snapshot(spec.dsn, SERVER_FACTS_SQL)
    cache_before = snapshot(spec.dsn, CACHE_EVIDENCE_SQL)
    ctx = multiprocessing.get_context("spawn")
    start_at = time.time() + 3 + 0.1 * spec.sessions
    procs = [
        ctx.Process(target=_session_entry,
                    args=(spec, i, instances, start_at, out_dir / f"session-{i:03d}.csv"))
        for i in range(spec.sessions)
    ]
    procs.append(ctx.Process(target=_probe_entry, args=(spec, start_at, out_dir / "probe.csv")))
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    codes = [p.exitcode for p in procs]
    cache_after = snapshot(spec.dsn, CACHE_EVIDENCE_SQL)
    summary = summarize_dir(out_dir, spec)
    summary.update(
        exit_codes=codes,
        aborted=any(c == EXIT_ABORTED for c in codes),
        failed_to_connect=any(c == EXIT_CONNECT_FAILED for c in codes),
        server=server,
        cache_evidence={"procedure": spec.cache_state, "before": cache_before,
                        "after": cache_after},
        client={"python": platform.python_version(), "platform": platform.platform(),
                "cpus": multiprocessing.cpu_count()},
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return summary


def summarize_dir(out_dir: Path, spec: RunSpec) -> dict:
    by_shape: dict[str, dict] = defaultdict(lambda: {"ok": [], "errors": 0, "timeouts": 0})
    for path in sorted(out_dir.glob("session-*.csv")):
        with path.open() as fh:
            for row in csv.DictReader(fh):
                if row["phase"] != "measure":
                    continue
                bucket = by_shape[row["shape"]]
                if row["outcome"] == OK:
                    bucket["ok"].append(float(row["latency_ms"]))
                elif row["outcome"] in TIMEOUT_OUTCOMES:
                    bucket["timeouts"] += 1
                else:
                    bucket["errors"] += 1
    floor = spec.min_samples_for_p99

    def summ(ok, errors, timeouts):
        return asdict(stats.summarize(ok, errors, timeouts, floor))

    overall = summ([x for b in by_shape.values() for x in b["ok"]],
                   sum(b["errors"] for b in by_shape.values()),
                   sum(b["timeouts"] for b in by_shape.values()))
    probe_ok, probe_err = [], 0
    probe_path = out_dir / "probe.csv"
    if probe_path.exists():
        with probe_path.open() as fh:
            for row in csv.DictReader(fh):
                if row["outcome"] == OK:
                    probe_ok.append(float(row["latency_ms"]))
                else:
                    probe_err += 1
    spec_public = {k: v for k, v in asdict(spec).items() if k != "dsn"}
    return {
        "spec": spec_public,
        "overall": overall,
        "throughput_qps": overall["sample_count"] / spec.measure_s,
        "by_shape": {s: summ(b["ok"], b["errors"], b["timeouts"])
                     for s, b in sorted(by_shape.items())},
        "probe": asdict(stats.summarize(probe_ok, probe_err, 0, 0)),
    }
