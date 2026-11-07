"""Benchmark harness (RC1-631, Phase 3; ADR 0010, ADR 0020, ADR 0021).

Everything a scored run needs, platform-agnostic: the frozen criteria (read
only through `criteria.load_verified`), the load model, a target per
configuration, and a slice. Neon orchestration lives in pocbench.phase3.

- **Network gate (R9).** Latency is scored net of the run's median probe
  round trip, so the probe must be stable. A job starts with a preflight
  probe and stops before spending compute if the network is slow or jittery.
  Every run is then checked against the frozen rule (probe p50, probe
  p99 - p50); a run that fails it, or whose probe could not measure, is
  recorded as rejected and re-run. A cell with no valid run after
  MAX_ATTEMPTS stops the job: per ADR 0014 the fix is a fixed-location
  runner, not more repetitions.
- **Cache states (ADR 0013, 0021).** `warm` runs the load model's warm-up on
  the run's own mix. `post-restart-with-eviction` restarts the compute
  (startup time recorded apart from the run), scans a filler table sized
  past the live file-cache limit, then measures with no warm-up. Evidence
  before and after is recorded; eviction is measured, never assumed.
- **Order.** Each repetition runs every (configuration, level, cache state)
  cell once in a seeded random order, so the baselines are re-run
  interleaved and drift lands in the spread, not in one configuration.
- **Entry points.** `development` takes the profiling slice only and
  re-checks every instance before running (planted-leak test). `evaluation`
  refuses without a verified frozen candidate or refusal record, before it
  creates or touches anything.
"""

import csv
import hashlib
import json
import multiprocessing
import os
import platform
import random
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psycopg

from pocbench import candidate, config, criteria, loadgen, slices, stats
from pocbench.record import REJECTED, SCORED, Record

POST_RESTART = "post-restart-with-eviction"
MAX_ATTEMPTS = 3
# A probe that measured less than this share of its expected round trips
# cannot vouch for the run's network.
PROBE_MIN_FRACTION = 0.9
PREFLIGHT_SECONDS = 60
FILLER_TABLE = "eviction_filler"
FILLER_FACTOR = 1.25  # filler bytes per byte of file cache
FILLER_CHUNK_ROWS = 1_000_000  # ~1 GB per insert at 8 rows per page
RESTART_CONNECT_TIMEOUT_S = 120
HOURS_PER_MONTH = 730


class NetworkRejected(RuntimeError):
    """The runner's network cannot support net-latency scoring (R9)."""


# ------------------------------------------------------------------ method

@dataclass(frozen=True)
class Method:
    """The parts of the frozen criteria the harness executes."""

    criteria_sha256: str
    probe_rule: dict
    order_seed: int
    eval_levels: tuple[int, ...]
    eval_repetitions: int
    cache_states: tuple[str, ...]
    error_ceiling: float
    tail_beyond: int
    tail_distinct_beyond: int
    z: float

    @classmethod
    def from_criteria(cls, doc: dict, sha: str, lm: dict) -> "Method":
        m = doc["method"]
        seed = re.search(r"seed (\d+)", m["order"])
        if seed is None:
            raise ValueError(f"criteria method order has no seed: {m['order']!r}")
        return cls(
            criteria_sha256=sha,
            probe_rule=m["latency_basis"]["run_valid_if"],
            order_seed=int(seed.group(1)),
            eval_levels=tuple(m["levels"]),
            eval_repetitions=m["repetitions"],
            cache_states=tuple(m["cache_states"]),
            error_ceiling=lm["errors"]["error_ceiling_fraction"],
            tail_beyond=m["under_sampled"]["tail_beyond"],
            tail_distinct_beyond=m["under_sampled"]["tail_distinct_beyond"],
            z=m["interval"]["z"],
        )


# --------------------------------------------------------------- the probe

def probe_verdict(latencies_ms: list[float], errors: int, expected: int, rule: dict) -> dict:
    """Apply the frozen run-validity rule to one probe series. A probe that
    lost round trips, or measured too few, makes the run invalid too: an
    unmeasured network is not a stable one."""
    reasons = []
    v = sorted(latencies_ms)
    p50 = stats.percentile(v, 50) if v else None
    p99 = stats.percentile(v, 99) if v else None
    if len(v) < PROBE_MIN_FRACTION * expected:
        reasons.append(f"probe measured {len(v)} of {expected} expected round trips")
    if errors:
        reasons.append(f"probe lost {errors} round trips")
    if p50 is not None and p50 > rule["probe_p50_ms_at_most"]:
        reasons.append(f"slow network: probe p50 {p50:.1f} ms > "
                       f"{rule['probe_p50_ms_at_most']:g} ms")
    if p50 is not None and p99 - p50 > rule["probe_p99_minus_p50_ms_at_most"]:
        reasons.append(f"jittery network: probe p99 - p50 {p99 - p50:.1f} ms > "
                       f"{rule['probe_p99_minus_p50_ms_at_most']:g} ms")
    return {"valid": not reasons, "p50_ms": p50, "p99_ms": p99,
            "jitter_ms": None if p50 is None else round(p99 - p50, 3),
            "samples": len(v), "expected": expected, "errors": errors, "reasons": reasons}


def preflight(dsn: str, lm: dict, rule: dict, seconds: float = PREFLIGHT_SECONDS) -> dict:
    """The job's first act: probe the target alone for `seconds`. A slow or
    jittery network stops the job before any run spends compute."""
    query, interval = lm["probe"]["query"], lm["probe"]["interval_ms"] / 1000
    lat, errors = [], 0
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        end = time.monotonic() + seconds
        while (t := time.monotonic()) < end:
            t0 = time.perf_counter()
            try:
                conn.execute(query)
                lat.append((time.perf_counter() - t0) * 1000)
            except psycopg.Error:
                errors += 1
            time.sleep(max(0.0, interval - (time.monotonic() - t)))
    return probe_verdict(lat, errors, int(seconds / interval), rule)


def require_network(verdict: dict) -> None:
    if not verdict["valid"]:
        raise NetworkRejected("preflight rejected the runner's network: "
                              + "; ".join(verdict["reasons"]))


# -------------------------------------------------------- configs, targets

@dataclass(frozen=True)
class Config:
    name: str
    cu: float
    ddl: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        body = json.dumps({"name": self.name, "cu": self.cu, "ddl": list(self.ddl)},
                          sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


def baselines(doc: dict, profile: dict) -> list[Config]:
    """The two baselines as the frozen criteria define them; the naive
    indexes are the ones Phase 1 derived (profile.json, pinned)."""
    b = doc["baselines"]
    return [Config("default", b["default"]["cu"], tuple(b["default"]["indexes"])),
            Config("naive", b["naive"]["cu"], tuple(profile["naive_ddl"]))]


class Target(Protocol):
    config: Config
    dsn: str
    platform: str
    shared_compute: bool
    can_restart: bool

    def restart(self) -> dict: ...
    def evict(self) -> dict: ...


@dataclass
class LocalTarget:
    """The fallback platform (ADR 0007): local Postgres, warm state only."""

    config: Config
    dsn: str
    platform: str = "local-postgres"
    shared_compute: bool = False
    can_restart: bool = False

    def restart(self) -> dict:
        raise NotImplementedError("local targets cannot restart; post-restart cells are skipped")

    def evict(self) -> dict:
        raise NotImplementedError("local targets have no file cache to evict")


def wait_for_connect(dsn: str, query: str, timeout_s: float = RESTART_CONNECT_TIMEOUT_S
                     ) -> dict:
    t0 = time.monotonic()
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
                connected = time.monotonic() - t0
                q0 = time.perf_counter()
                conn.execute(query)
                return {"first_connect_seconds": round(connected, 3),
                        "first_query_ms": round((time.perf_counter() - q0) * 1000, 3)}
        except psycopg.OperationalError:
            if time.monotonic() - t0 > timeout_s:
                raise
            time.sleep(1)


def file_cache_bytes(conn: psycopg.Connection) -> int | None:
    """The live file-cache limit (Neon only; None on stock Postgres)."""
    rows = conn.execute(
        "SELECT setting, coalesce(unit, '') FROM pg_settings WHERE name IN "
        "('neon.max_file_cache_size', 'neon.file_cache_size_limit')").fetchall()
    sizes = [parse_size(f"{s} {u}") for s, u in rows]
    sizes = [s for s in sizes if s]
    return max(sizes) if sizes else None


_UNITS = {"b": 1, "kb": 1024, "8kb": 8192, "mb": 1024 ** 2, "gb": 1024 ** 3}


def parse_size(text: str) -> int | None:
    """'6553 MB', '6553MB MB', '16384 8kB' -> bytes."""
    m = re.match(r"\s*(\d+)\s*([0-9]*[a-zA-Z]+)?", text)
    if not m:
        return None
    return int(m.group(1)) * _UNITS.get((m.group(2) or "b").lower(), 1)


def ensure_filler(dsn: str, target_bytes: int, chunk_rows: int = FILLER_CHUNK_ROWS) -> dict:
    """A table outside the mix, grown until it is at least `target_bytes`.
    Rows of ~930 bytes stay inline (below the TOAST threshold), so the heap
    size is what the scan reads."""
    t0 = time.monotonic()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute("SET statement_timeout = 0")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {FILLER_TABLE} (id bigint, pad text) "
                     "WITH (autovacuum_enabled = false)")
        size = conn.execute(f"SELECT pg_relation_size('{FILLER_TABLE}')").fetchone()[0]
        start = conn.execute(f"SELECT coalesce(max(id), 0) FROM {FILLER_TABLE}").fetchone()[0]
        while size < target_bytes:
            conn.execute(f"INSERT INTO {FILLER_TABLE} SELECT g, repeat(md5(g::text), 28) "
                         "FROM generate_series(%s::bigint, %s::bigint) g",
                         (start + 1, start + chunk_rows))
            start += chunk_rows
            size = conn.execute(f"SELECT pg_relation_size('{FILLER_TABLE}')").fetchone()[0]
    return {"table": FILLER_TABLE, "bytes": size, "target_bytes": target_bytes,
            "seconds": round(time.monotonic() - t0, 1)}


def evict_scan(dsn: str) -> dict:
    """Scan the filler with the file-cache evidence on either side."""
    before = loadgen.snapshot(dsn, loadgen.CACHE_EVIDENCE_SQL)
    t0 = time.monotonic()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute("SET statement_timeout = 0")
        conn.execute(f"SELECT count(*) FROM {FILLER_TABLE}")
    after = loadgen.snapshot(dsn, loadgen.CACHE_EVIDENCE_SQL)
    return {"scan_seconds": round(time.monotonic() - t0, 1), "before": before, "after": after}


# ---------------------------------------------------------------- schedule

@dataclass(frozen=True)
class Cell:
    repetition: int
    config: str
    level: int
    cache_state: str


def schedule(configs: list[str], levels: list[int], cache_states: list[str], reps: int,
             seed: int, tag: str) -> list[Cell]:
    """Every cell once per repetition, in a seeded random order per
    repetition: the baselines are re-run interleaved, never in a block."""
    rng = random.Random(f"{seed}:{tag}")
    out = []
    for r in range(1, reps + 1):
        block = [Cell(r, c, lvl, cs) for c in configs for lvl in levels for cs in cache_states]
        rng.shuffle(block)
        out += block
    return out


# ------------------------------------------------------ one run's summary

def read_raw(run_dir: Path) -> tuple[list[dict], list[dict]]:
    """Measured workload rows and probe rows of one run."""
    rows = []
    for path in sorted(run_dir.glob("session-*.csv")):
        with path.open() as fh:
            rows += [r for r in csv.DictReader(fh) if r["phase"] == "measure"]
    probe = []
    if (run_dir / "probe.csv").exists():
        with (run_dir / "probe.csv").open() as fh:
            probe = list(csv.DictReader(fh))
    return rows, probe


def raw_hashes(run_dir: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(run_dir.glob("*.csv"))}


def _pcts(values: list[float]) -> dict:
    if not values:
        return {"p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    v = sorted(values)
    return {f"p{p}_ms": stats.percentile(v, p) for p in (50, 95, 99)} | {"max_ms": v[-1]}


def measured_seconds(rows: list[dict], spec_measure_s: float, aborted: bool) -> float:
    """The window a run actually measured: the full window, or up to the
    last query's end when the run aborted early."""
    if not aborted or not rows:
        return spec_measure_s
    end = max(float(r["start_ms"]) + float(r["latency_ms"]) for r in rows) / 1000
    return min(spec_measure_s, max(end, 1e-3))


def summarize_run(rows: list[dict], probe_p50: float | None, measure_s: float) -> dict:
    """Per shape and overall: counts, failures, client and net latency.
    Net = client latency - the run's probe p50, floored at zero (a fast
    query can beat a median round trip by a little; the clamp is counted)."""
    base = probe_p50 or 0.0
    by: dict[str, dict] = defaultdict(lambda: {"client": [], "net": [], "errors": 0,
                                               "timeouts": 0, "instances": set()})
    clamped = 0
    for r in rows:
        b = by[r["shape"]]
        if r["outcome"] == loadgen.OK:
            lat = float(r["latency_ms"])
            net = lat - base
            clamped += net < 0
            b["client"].append(lat)
            b["net"].append(max(0.0, net))
            b["instances"].add(r["instance"])
        elif r["outcome"] in loadgen.TIMEOUT_OUTCOMES:
            b["timeouts"] += 1
        else:
            b["errors"] += 1

    def one(b: dict) -> dict:
        ok = len(b["client"])
        n = ok + b["errors"] + b["timeouts"]
        return {"observations": n, "ok": ok, "errors": b["errors"], "timeouts": b["timeouts"],
                "failure_fraction": round((n - ok) / n, 6) if n else 0.0,
                "distinct_instances_ok": len(b["instances"]),
                "client": _pcts(b["client"]), "net": _pcts(b["net"])}

    shapes_out = {s: one(b) for s, b in sorted(by.items())}
    allb = {"client": [x for b in by.values() for x in b["client"]],
            "net": [x for b in by.values() for x in b["net"]],
            "errors": sum(b["errors"] for b in by.values()),
            "timeouts": sum(b["timeouts"] for b in by.values()),
            "instances": {(s, i) for s, b in by.items() for i in b["instances"]}}
    overall = one(allb)
    return {"by_shape": shapes_out, "overall": overall, "net_clamped": clamped,
            "measured_seconds": round(measure_s, 3),
            "successful_qps": round(overall["ok"] / measure_s, 4)}


def _first(snap: dict, view: str) -> dict:
    v = snap.get(view)
    return v[0] if isinstance(v, list) and v else {}


def cache_ratios(before: dict, after: dict) -> dict:
    """Hit ratios over the run from the before/after counters."""
    out = {}
    b, a = _first(before, "pg_stat_database"), _first(after, "pg_stat_database")
    if b and a:
        hit, read = a["blks_hit"] - b["blks_hit"], a["blks_read"] - b["blks_read"]
        out["shared_buffers_hit_ratio"] = round(hit / (hit + read), 4) if hit + read else None
    b, a = _first(before, "neon_stat_file_cache"), _first(after, "neon_stat_file_cache")
    if b and a:
        hit = a["file_cache_hits"] - b["file_cache_hits"]
        miss = a["file_cache_misses"] - b["file_cache_misses"]
        out["file_cache_hit_ratio"] = round(hit / (hit + miss), 4) if hit + miss else None
        out["file_cache_used_before"] = b.get("file_cache_used")
        out["file_cache_used_after"] = a.get("file_cache_used")
    return out


def server_checks(summary: dict) -> list[str]:
    """R6: a run is void if the compute restarted or its statistics reset
    inside the window, or a session never connected."""
    ev = summary["cache_evidence"]
    reasons = []
    if _first(ev["before"], "server_start") != _first(ev["after"], "server_start"):
        reasons.append("compute restarted during the run")
    sb = _first(ev["before"], "pg_stat_database").get("stats_reset")
    sa = _first(ev["after"], "pg_stat_database").get("stats_reset")
    if sb != sa:
        reasons.append("statistics were reset during the run")
    if summary.get("failed_to_connect"):
        reasons.append("a session could not connect")
    return reasons


def runner_facts() -> dict:
    keys = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "RUNNER_NAME", "RUNNER_OS",
            "RUNNER_ARCH", "ImageOS", "ImageVersion")
    return {k: os.environ[k] for k in keys if k in os.environ} | {
        "python": platform.python_version(), "cpus": multiprocessing.cpu_count(),
        "platform": platform.platform()}


# -------------------------------------------------------------- execution

@dataclass
class Session:
    """What one entry-point invocation runs with."""

    entry_point: str
    method: Method
    lm: dict
    lm_sha256: str
    space: dict
    space_sha256: str
    record: Record
    raw_root: Path
    cu_hour_usd: float
    runner: dict = field(default_factory=runner_facts)
    rows: list[dict] = field(default_factory=list)


def criteria_sha256(doc: dict) -> str:
    """The frozen file is canonical(doc), so its pinned hash is derived from
    the verified document without reading the file a second time."""
    return hashlib.sha256(criteria.canonical(doc)).hexdigest()


def open_session(entry_point: str, raw_root: Path, record_path: Path,
                 root: Path = config.REPO_ROOT) -> tuple[Session, dict]:
    """Load the frozen criteria (the only way the harness reads them), the
    load model and config space they pin, and open the record."""
    doc = criteria.load_verified(root)
    sha = criteria_sha256(doc)
    lm = config.load_model(root / criteria.PINNED_INPUTS["load_model"])
    sp = config.config_space(root / criteria.PINNED_INPUTS["config_space"])
    for key, pinned in (("load_model", lm), ("config_space", sp)):
        if doc["pins"][key]["sha256"] != pinned.sha256:
            raise criteria.CriteriaPinError(f"{key} differs from the criteria pin")
    rec = Record(record_path, criteria_sha256=sha, load_model=lm.data,
                 load_model_sha256=lm.sha256, config_space_sha256=sp.sha256,
                 cache_states=sp.data["cache_states"])
    return Session(entry_point, Method.from_criteria(doc, sha, lm.data), lm.data, lm.sha256,
                   sp.data, sp.sha256, rec, raw_root,
                   sp.data["cost"]["cu_hour_usd"]), doc


def run_cell(s: Session, cell: Cell, target: Target, si: slices.SliceInstances) -> dict:
    uses_warmup = s.space["cache_states"][cell.cache_state]["uses_warmup"]
    warmup = s.lm["timing"]["warmup_seconds"] if uses_warmup else 0
    expected_probe = int(s.lm["timing"]["measure_seconds"] * 1000 / s.lm["probe"]["interval_ms"])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run_id = (f"{s.entry_point[:3]}-r{cell.repetition}-{cell.config}-s{cell.level:02d}-"
                  f"{'warm' if cell.cache_state == 'warm' else 'evict'}-a{attempt}")
        out = s.raw_root / run_id
        started = datetime.now(UTC).isoformat(timespec="seconds")
        startup = eviction = None
        if cell.cache_state == POST_RESTART:
            startup = target.restart()
            eviction = target.evict()
        spec = loadgen.RunSpec.from_load_model(
            s.lm, s.lm_sha256, dsn=target.dsn, label=run_id, sessions=cell.level,
            cache_state=cell.cache_state, warmup_s=warmup)
        summary = loadgen.run(spec, list(si.instances), out)
        rows, probe_rows = read_raw(out)
        verdict = probe_verdict([float(r["latency_ms"]) for r in probe_rows
                                 if r["outcome"] == loadgen.OK],
                                sum(r["outcome"] != loadgen.OK for r in probe_rows),
                                expected_probe, s.method.probe_rule)
        server = server_checks(summary)
        reasons = verdict["reasons"] + server
        m_s = measured_seconds(rows, spec.measure_s, summary["aborted"])
        run = summarize_run(rows, verdict["p50_ms"], m_s)
        row = {
            "status": REJECTED if reasons else SCORED,
            "reasons": reasons,
            "entry_point": s.entry_point,
            "slice": si.name,
            **s.record.pins,
            "config": {"name": target.config.name, "cu": target.config.cu,
                       "indexes": len(target.config.ddl),
                       "shared_compute": target.shared_compute},
            "config_sha256": target.config.sha256,
            "cache_state": cell.cache_state,
            "level": cell.level,
            "diagnostic_only": cell.level in s.lm["sessions"]["diagnostic_only"],
            "repetition": cell.repetition,
            "attempt": attempt,
            "platform": target.platform,
            "run_id": run_id,
            "started_at": started,
            "statement": f"performance at {cell.level} concurrent sessions, closed loop "
                         "(not a sustained arrival rate; ADR 0012)",
            "spec": {k: v for k, v in asdict(spec).items() if k != "dsn"},
            "probe": verdict,
            "aborted": summary["aborted"],
            "run": run,
            "cost_usd": round(target.config.cu * s.cu_hour_usd * m_s / 3600, 6),
            "cache_evidence": {"before": summary["cache_evidence"]["before"],
                               "after": summary["cache_evidence"]["after"],
                               "ratios": cache_ratios(summary["cache_evidence"]["before"],
                                                      summary["cache_evidence"]["after"])},
            "startup": startup,
            "eviction": eviction,
            "server": summary["server"],
            "runner": s.runner,
            "raw": {"dir": run_id, "files": raw_hashes(out)},
        }
        s.rows.append(s.record.append(row))
        if not reasons:
            return row
    raise NetworkRejected(
        f"no valid run for {cell} in {MAX_ATTEMPTS} attempts (last: {'; '.join(reasons)}). "
        "Per ADR 0014 the fix is a fixed-location runner, not more repetitions")


def run_protocol(s: Session, targets: dict[str, Target], si: slices.SliceInstances,
                 levels: list[int], reps: int, cache_states: list[str]) -> dict:
    cells = schedule(sorted(targets), levels, cache_states, reps, s.method.order_seed,
                     s.entry_point)
    skipped = [c for c in cells
               if c.cache_state == POST_RESTART and not targets[c.config].can_restart]
    done = []
    for cell in cells:
        if cell in skipped:
            continue
        run_cell(s, cell, targets[cell.config], si)
        done.append(asdict(cell))
    return {"cells_run": len(done), "order": done,
            "skipped": [asdict(c) | {"why": "platform cannot restart"} for c in skipped],
            "rejected_runs": sum(r["status"] == REJECTED for r in s.rows)}


def development(s: Session, targets: dict[str, Target], rebuilt: slices.Rebuilt,
                levels: list[int] | None = None) -> dict:
    """Profiling slice only (R3). Protocol from config-space [protocol.dev];
    `levels` may widen it to any load-model level (Phase 3 runs all three)."""
    si = rebuilt.profiling()
    slices.guard_profiling(si, rebuilt)
    dev = s.space["protocol"]["dev"]
    levels = levels or dev["levels"]
    if not set(levels) <= set(s.lm["sessions"]["levels"]):
        raise ValueError(f"levels {levels} are not load-model levels")
    return run_protocol(s, targets, si, levels, dev["repetitions"], dev["cache_states"])


def require_frozen(criteria_sha256: str, **paths) -> candidate.Frozen:
    """The evaluation gate. Call it before creating anything."""
    return candidate.load_frozen(criteria_sha256, **paths)


def evaluation(s: Session, frozen: candidate.Frozen,
               make_targets: Callable[[list[Config]], dict[str, Target]],
               load_rebuilt: Callable[[], slices.Rebuilt], base: list[Config]) -> dict:
    """Held-out evaluation of the frozen candidate (or, after a refusal, of
    the baselines alone), on the full protocol from the frozen criteria."""
    if not isinstance(frozen, candidate.Frozen) or not frozen.verified:
        raise candidate.NotFrozenError("evaluation needs a verified frozen record")
    configs = list(base)
    if frozen.kind == "candidate":
        c = frozen.doc["config"]
        configs.append(Config(c["name"], c["cu"], tuple(c["ddl"])))
    si = load_rebuilt().held_out(frozen)
    targets = make_targets(configs)
    m = s.method
    return run_protocol(s, targets, si, list(m.eval_levels), m.eval_repetitions,
                        list(m.cache_states)) | {"frozen": {"kind": frozen.kind,
                                                            "sha256": frozen.sha256}}


# ----------------------------------------------------------------- pooling

def tail_support(pairs: list[tuple[float, str]], p: int, beyond: int, distinct: int) -> dict:
    """ADR 0018: supported when `beyond` observations from `distinct`
    instances lie past the percentile."""
    v = stats.percentile([x for x, _ in pairs], p)
    past = [i for x, i in pairs if x > v]
    return {"observations_beyond": len(past), "distinct_beyond": len(set(past)),
            "supported": len(past) >= beyond and len(set(past)) >= distinct}


def _pool_series(per_rep: list[list[tuple[float, str]]], m: Method) -> dict:
    pairs = sorted((x for rep in per_rep for x in rep), key=lambda t: t[0])
    out: dict = {"ok": len(pairs)}
    if not pairs:
        return out
    values = [x for x, _ in pairs]
    for p in (50, 95, 99):
        reps = [stats.percentile(sorted(x for x, _ in r), p) for r in per_rep if r]
        lo, hi = stats.quantile_ci(values, p, m.z)
        med = statistics.median(reps) if reps else 0
        out[f"p{p}"] = {
            "ms": stats.percentile(values, p), "ci95_ms": [lo, hi],
            **tail_support(pairs, p, m.tail_beyond, m.tail_distinct_beyond),
            "per_rep_ms": reps,
            "spread": round(stats.spread(reps), 4) if len(reps) >= 2 and med > 0 else None,
        }
    out["max_ms"] = values[-1]
    return out


def pool(rows: list[dict], raw_root: Path, m: Method, verify_raw: bool = True) -> list[dict]:
    """Per (configuration, level, cache state): observations pooled across
    the scored repetitions. Rejected rows never enter (criteria method)."""
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("kind") == "run" and r["status"] == SCORED:
            cells[(r["config"]["name"], r["level"], r["cache_state"])].append(r)
    out = []
    for (cfg, level, cs), reps in sorted(cells.items()):
        shape_reps: dict[str, list] = defaultdict(list)
        overall_reps, fails, obs = [], defaultdict(int), defaultdict(int)
        ok_total = cost = 0.0
        for r in reps:
            run_dir = raw_root / r["raw"]["dir"]
            if verify_raw and raw_hashes(run_dir) != r["raw"]["files"]:
                raise ValueError(f"raw logs of {r['run_id']} do not match the record")
            measured, _ = read_raw(run_dir)
            base = r["probe"]["p50_ms"] or 0.0
            per_shape: dict[str, list] = defaultdict(list)
            for q in measured:
                obs[q["shape"]] += 1
                if q["outcome"] == loadgen.OK:
                    per_shape[q["shape"]].append(
                        (max(0.0, float(q["latency_ms"]) - base), q["instance"]))
                else:
                    fails[q["shape"]] += 1
            for sid in set(per_shape) | {q["shape"] for q in measured}:
                shape_reps[sid].append(per_shape.get(sid, []))
            overall_reps.append([(x, f"{sid}:{i}") for sid, v in per_shape.items()
                                 for x, i in v])
            ok_total += r["run"]["overall"]["ok"]
            cost += r["cost_usd"]
        by_shape = {}
        for sid in sorted(shape_reps):
            n = obs[sid]
            frac = fails[sid] / n if n else 0.0
            by_shape[sid] = {"observations": n, "failure_fraction": round(frac, 6),
                             "over_error_ceiling": frac > m.error_ceiling,
                             "net": _pool_series(shape_reps[sid], m)}
        qps = [r["run"]["successful_qps"] for r in reps]
        first = reps[0]
        out.append({
            "config": cfg, "cu": first["config"]["cu"],
            "shared_compute": first["config"]["shared_compute"], "level": level,
            "diagnostic_only": first["diagnostic_only"], "cache_state": cs,
            "platform": first["platform"], "repetitions": len(reps),
            "run_ids": [r["run_id"] for r in reps],
            "aborted_runs": sum(r["aborted"] for r in reps),
            "by_shape": by_shape,
            "overall_reported_never_scored": _pool_series(overall_reps, m),
            "successful_qps": {"median": statistics.median(qps), "per_rep": qps},
            "cost_per_successful_query_usd": cost / ok_total if ok_total else None,
            "probe_p50_ms_per_rep": [r["probe"]["p50_ms"] for r in reps],
            "cache_ratios_per_rep": [r["cache_evidence"]["ratios"] for r in reps],
            "startup_per_rep": [r["startup"] for r in reps if r["startup"]],
            "eviction_scan_seconds_per_rep": [r["eviction"]["scan_seconds"] for r in reps
                                              if r["eviction"]],
        })
    return out
