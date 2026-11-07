"""Phase 1 profiler (RC1-629).

Input: the query log (literal SQL) and a database holding the dataset. The
profiler never sees the generator's shape names. Steps:

1. prepare: normalize every log line to a shape, resolve each line's tenant
   from its parameters, split into profiling and held-out slices, derive the
   naive baseline's indexes from the profiling slice's predicates.
2. capture: the timing input, a closed-loop run of the PROFILING slice on
   the capture configuration (naive at 2 CU, ADR 0016), with
   pg_stat_statements reset first. The run is invalid unless the server was
   up and the statistics were not reset between the start and the end
   (Neon drops cumulative statistics on suspend, R6).
3. diagnose: EXPLAIN (ANALYZE, BUFFERS) on sampled instances per shape, and
   per-shape selectivity with the planner's row estimate against the actual
   count.
4. report: per shape frequency, total time, tail with its support flag,
   selectivity, estimate versus actual, and the shapes that dominate.

The held-out slice is never executed here.
"""

import csv
import gzip
import hashlib
import io
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pocbench import loadgen, shapes, split, workload
from pocbench.loadgen import Instance

DOMINANT_SHARE = 0.80  # the smallest set of shapes covering this much total time


@dataclass
class Prepared:
    entries: list[workload.LogEntry]
    norm: dict[int, shapes.Normalized]  # seq -> normalized
    shape_of: dict[int, str]  # seq -> profiler shape id (s01...)
    templates: dict[str, str]  # shape id -> template
    account_of: dict[int, int | None]
    account_sizes: dict[int, int]
    split: split.Split
    naive_ddl: list[str]
    log_sha256: str
    extra: dict = field(default_factory=dict)

    def instances(self, slice_name: str) -> list[Instance]:
        return [Instance(self.shape_of[e.seq], self.norm[e.seq].executable,
                         self.norm[e.seq].params)
                for e in self.entries if self.split.slice_of[e.seq] == slice_name]


def tail_floor(percentile: float, beyond: int) -> int:
    """Observations needed so that `beyond` of them lie past the percentile."""
    return math.ceil(beyond / (1 - percentile / 100) - 1e-9)


# ---------------------------------------------------------------- log files

def write_log(entries: list[workload.LogEntry], log_path: Path, truth_path: Path) -> str:
    """Gzipped JSON lines with mtime 0, so the bytes and the hash are
    reproducible. Ground truth goes to a separate file the profiler never
    opens."""
    def gz(lines: list[str]) -> bytes:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
            fh.write("".join(lines).encode())
        return buf.getvalue()

    raw = gz([json.dumps({"seq": e.seq, "sql": e.sql}) + "\n" for e in entries])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(raw)
    truth_path.write_bytes(gz([f"{e.seq},{e.shape}\n" for e in entries]))
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------------ prepare

def _resolve_accounts(conn: psycopg.Connection, norm: dict[int, shapes.Normalized]
                      ) -> dict[int, int | None]:
    """Map each log line to its tenant through the dataset (shapes.TENANT_KEYS)."""
    keys: dict[int, tuple[str, object]] = {}
    for seq, n in norm.items():
        p = shapes.tenant_key(n.template)
        if p is not None:
            value = n.params[p.param - 1]
            keys[seq] = (p.column, value.lower() if p.expression == "lower" else value)
    wanted: dict[str, set] = defaultdict(set)
    for col, v in keys.values():
        wanted[col].add(v)
    lookup: dict[str, dict] = {"account_id": {v: v for v in wanted["account_id"]}}
    resolvers = {
        "contact_id": "SELECT id, account_id FROM contacts WHERE id = ANY(%s)",
        "campaign_id": "SELECT id, account_id FROM campaigns WHERE id = ANY(%s)",
        "email": "SELECT lower(email), account_id FROM contacts WHERE lower(email) = ANY(%s)",
    }
    for col, sql in resolvers.items():
        if wanted[col]:
            lookup[col] = dict(conn.execute(sql, (sorted(wanted[col]),)).fetchall())
    # A key that matches no row (a lookup for a missing email) has no tenant.
    return {seq: (lookup[keys[seq][0]].get(keys[seq][1]) if seq in keys else None)
            for seq in norm}


def prepare(conn: psycopg.Connection, space: dict, log_dir: Path) -> Prepared:
    c, s = space["corpus"], space["split"]
    facts = workload.read_facts(conn)
    entries = workload.generate(facts, c["log_entries"], c["log_seed"])
    sha = write_log(entries, log_dir / "query-log.jsonl.gz",
                    log_dir / "query-log-truth.csv.gz")
    norm = {e.seq: shapes.normalize(e.sql) for e in entries}
    freq = Counter(n.template for n in norm.values())
    ids = {t: f"s{i:02d}" for i, (t, _) in
           enumerate(sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])), 1)}
    shape_of = {seq: ids[n.template] for seq, n in norm.items()}
    account_of = _resolve_accounts(conn, norm)
    sizes = dict(zip(facts.accounts, facts.account_weights, strict=True))
    sp = split.make([split.Item(e.seq, shape_of[e.seq], account_of[e.seq]) for e in entries],
                    sizes, held_frac=s["held_out_fraction"], seed=s["seed"],
                    candidates=s["candidates"])
    profiling_templates = sorted({norm[e.seq].template for e in entries
                                  if sp.slice_of[e.seq] == split.PROFILING})
    return Prepared(entries, norm, shape_of, {v: k for k, v in ids.items()}, account_of,
                    sizes, sp, shapes.naive_indexes(profiling_templates), sha)


def manifest(p: Prepared, space: dict, lm: dict) -> dict:
    """The committed split manifest: enough to rebuild both slices from the
    log and check the result."""
    r = lm["report"]
    floors = {f"p{q}": {"observations": tail_floor(q, r["tail_beyond"]),
                        "distinct_instances": tail_floor(q, r["tail_distinct_beyond"])}
              for q in (95, 99)}
    counts = split.per_shape([split.Item(e.seq, p.shape_of[e.seq], p.account_of[e.seq])
                              for e in p.entries], p.split.slice_of)
    distinct: dict[tuple[str, str], set] = defaultdict(set)
    no_tenant = Counter()
    for e in p.entries:
        sid, sl = p.shape_of[e.seq], p.split.slice_of[e.seq]
        distinct[(sid, sl)].add(p.norm[e.seq].params)
        no_tenant[sid] += p.account_of[e.seq] is None
    for sid, v in counts.items():
        v["template"] = p.templates[sid]
        v["label"] = shapes.label(p.templates[sid])
        v["no_tenant"] = no_tenant[sid]
        for sl in (split.PROFILING, split.HELD_OUT):
            d = len(distinct[(sid, sl)])
            v[f"{sl}_distinct"] = d
            v[f"{sl}_tail_supported"] = {q: d >= f["distinct_instances"]
                                         for q, f in floors.items()}
    deciles = split.size_deciles(p.account_sizes)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "log": {"path": "results/phase1/query-log.jsonl.gz", "sha256": p.log_sha256,
                "entries": len(p.entries), "seed": space["corpus"]["log_seed"]},
        "rule": {"unit": "account", "stratify_by": "account_size_decile",
                 "held_out_fraction": space["split"]["held_out_fraction"],
                 "seed": space["split"]["seed"], "candidates": space["split"]["candidates"],
                 "chosen_candidate": p.split.candidate,
                 "worst_shape_deviation": p.split.worst_shape_deviation,
                 "no_tenant_rule": "per instance, seeded draw in log order"},
        "tail_floors": floors,
        "assignment_sha256": split.assignment_sha256(p.split.slice_of),
        "slices": {
            split.PROFILING: {"entries": sum(v[split.PROFILING] for v in counts.values()),
                              "accounts": sorted(p.split.profiling_accounts)},
            split.HELD_OUT: {"entries": sum(v[split.HELD_OUT] for v in counts.values()),
                             "accounts": sorted(p.split.held_out_accounts)},
        },
        "held_out_accounts_by_decile": {
            d: sum(1 for a in p.split.held_out_accounts if deciles[a] == d) for d in range(10)},
        "shapes": counts,
    }


# ------------------------------------------------------------------ capture

STATS_SQL = {
    "postmaster_start": "SELECT pg_postmaster_start_time()",
    "pgss_reset": "SELECT stats_reset FROM pg_stat_statements_info",
    "db_stats_reset": ("SELECT stats_reset FROM pg_stat_database "
                       "WHERE datname = current_database()"),
    "now": "SELECT now()",
}
PGSS_SQL = """
SELECT queryid, query, calls, total_exec_time, mean_exec_time, rows,
       shared_blks_hit, shared_blks_read
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())"""


def _stats(conn: psycopg.Connection) -> dict:
    return {k: conn.execute(q).fetchone()[0] for k, q in STATS_SQL.items()}


def stats_validity(before: dict, after: dict) -> dict:
    """The capture's cumulative statistics are trusted only if the server
    and both statistics clocks are unchanged across the run, and the reset
    predates its start (R6)."""
    checks = {
        "server_not_restarted": before["postmaster_start"] == after["postmaster_start"],
        "pgss_not_reset_during_run": before["pgss_reset"] == after["pgss_reset"],
        "db_stats_not_reset_during_run": before["db_stats_reset"] == after["db_stats_reset"],
        "reset_predates_run": before["pgss_reset"] is not None
                              and before["pgss_reset"] <= before["now"],
    }
    return {"valid": all(checks.values()), "checks": checks,
            "before": {k: str(v) for k, v in before.items()},
            "after": {k: str(v) for k, v in after.items()}}


def capture(dsn: str, p: Prepared, lm, cap: dict, out_dir: Path) -> dict:
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute("SELECT pg_stat_statements_reset()")
        before = _stats(conn)
    spec = loadgen.RunSpec.from_load_model(
        lm.data, lm.sha256, dsn=dsn, label="capture", sessions=cap["sessions"],
        cache_state="warm", warmup_s=cap["warmup_seconds"], measure_s=cap["measure_seconds"])
    summary = loadgen.run(spec, p.instances(split.PROFILING), out_dir)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        after = _stats(conn)
        cur = conn.execute(PGSS_SQL)
        cols = [d.name for d in cur.description]
        pgss = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return {"summary": summary, "stats_validity": stats_validity(before, after),
            "pg_stat_statements": pgss_by_shape(pgss, p.templates),
            "pg_stat_statements_rows": [{k: (str(v) if k == "queryid" else v)
                                         for k, v in r.items()} for r in pgss]}


PGSS_SUMMED = ("calls", "total_exec_time", "rows", "shared_blks_hit", "shared_blks_read")


def pgss_by_shape(pgss: list[dict], templates: dict[str, str]) -> dict[str, dict]:
    """Sum pg_stat_statements entries per shape. One shape can own several
    entries: psycopg binds an int as int2, int4 or int8 by value, and the
    parameter types are part of the statement's identity."""
    by_template = {t: sid for sid, t in templates.items()}
    out: dict[str, dict] = {}
    for row in pgss:
        sid = by_template.get(shapes.normalize(row["query"]).template)
        if sid is None:
            continue
        acc = out.setdefault(sid, {k: 0 for k in PGSS_SUMMED} | {"entries": 0})
        acc["entries"] += 1
        for k in PGSS_SUMMED:
            acc[k] += row[k]
    for acc in out.values():
        acc["mean_exec_time"] = round(acc["total_exec_time"] / acc["calls"], 3)
        acc["total_exec_time"] = round(acc["total_exec_time"], 3)
    return out


def capture_totals(out_dir: Path, timeout_ms: int) -> dict[str, dict]:
    """Client-side totals per shape from the raw session logs. A timed-out
    query counts its full timeout: pg_stat_statements drops cancelled
    statements, so server-side totals understate exactly the slowest shapes."""
    acc: dict[str, dict] = defaultdict(lambda: {"observations": 0, "total_ms": 0.0})
    for path in sorted(out_dir.glob("session-*.csv")):
        with path.open() as fh:
            for row in csv.DictReader(fh):
                if row["phase"] != "measure":
                    continue
                a = acc[row["shape"]]
                a["observations"] += 1
                if row["outcome"] in loadgen.TIMEOUT_OUTCOMES:
                    a["total_ms"] += timeout_ms
                else:
                    a["total_ms"] += float(row["latency_ms"])
    return dict(acc)


# ----------------------------------------------------------------- diagnose

def _walk(node: dict, depth: int = 0):
    yield node, depth
    for child in node.get("Plans", []):
        yield from _walk(child, depth + 1)


def plan_facts(plan_json: list) -> dict:
    root = plan_json[0]
    nodes = list(_walk(root["Plan"]))
    removed = sum((n.get("Rows Removed by Filter", 0) + n.get("Rows Removed by Index Recheck", 0))
                  * n.get("Actual Loops", 1) for n, _ in nodes)
    scans = sorted({f"{n['Node Type']}:{n.get('Index Name') or n.get('Relation Name')}"
                    for n, _ in nodes if "Relation Name" in n or "Index Name" in n})
    sorts = [{"method": n.get("Sort Method"), "space_type": n.get("Sort Space Type"),
              "space_kb": n.get("Sort Space Used")} for n, _ in nodes if n["Node Type"] == "Sort"]
    spills = any(s["space_type"] == "Disk" for s in sorts) or any(
        n.get("Disk Usage", 0) > 0 for n, _ in nodes)
    return {
        "execution_ms": round(root.get("Execution Time", 0.0), 3),
        "planning_ms": round(root.get("Planning Time", 0.0), 3),
        "scans": scans,
        "rows_removed": int(removed),
        "workers_planned": max((n.get("Workers Planned", 0) for n, _ in nodes), default=0),
        "workers_launched": max((n.get("Workers Launched", 0) for n, _ in nodes), default=0),
        "shared_hit": root["Plan"].get("Shared Hit Blocks", 0),
        "shared_read": root["Plan"].get("Shared Read Blocks", 0),
        "spills": spills,
        "sorts": sorts,
    }


def predicate_query(template: str, params: tuple) -> tuple[str, str, tuple] | None:
    """The shape's WHERE clause alone, for counting matches: (table, sql, params)."""
    where = shapes.where_clause(template)
    if where is None:
        return None
    used = [int(m) for m in re.findall(r"\$(\d+)", where)]
    sql_where = re.sub(r"\$\d+", "%s", where.replace("%", "%%"))
    return shapes.table_of(template), sql_where, tuple(params[i - 1] for i in used)


def q_error(estimate: float, actual: float) -> float:
    e, a = max(1.0, estimate), max(1.0, actual)
    return round(max(e, a) / min(e, a), 2)


def diagnose(dsn: str, p: Prepared, cap: dict, seed: int) -> dict:
    rng = random.Random(f"diagnose:{seed}")
    pool: dict[str, list[int]] = defaultdict(list)
    for e in p.entries:
        if p.split.slice_of[e.seq] == split.PROFILING:
            pool[p.shape_of[e.seq]].append(e.seq)
    out: dict[str, dict] = {}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        reltuples = dict(conn.execute(
            "SELECT relname, reltuples::bigint FROM pg_class WHERE relname IN "
            "('accounts','campaigns','contacts','events')").fetchall())
        for sid in sorted(pool):
            seqs = rng.sample(pool[sid], min(len(pool[sid]), cap["explain_samples"]))
            plans, sel = [], []
            conn.execute(f"SET statement_timeout = {int(cap['explain_timeout_ms'])}")
            for seq in seqs:
                n = p.norm[seq]
                try:
                    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + n.executable,
                                        n.params).fetchone()[0]
                    plans.append(plan_facts(plan))
                except psycopg.errors.QueryCanceled:
                    plans.append({"timeout_ms": cap["explain_timeout_ms"]})
            conn.execute(f"SET statement_timeout = {int(cap['count_timeout_ms'])}")
            for seq in seqs[:cap["count_samples"]]:
                pq = predicate_query(p.norm[seq].template, p.norm[seq].params)
                if pq is None:
                    continue
                table, where, params = pq
                est = conn.execute(f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {table} WHERE {where}",
                                   params).fetchone()[0][0]["Plan"]["Plan Rows"]
                try:
                    actual = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}",
                                          params).fetchone()[0]
                except psycopg.errors.QueryCanceled:
                    sel.append({"estimate": est, "actual": None})
                    continue
                sel.append({"estimate": est, "actual": actual,
                            "selectivity": actual / max(1, reltuples[table]),
                            "q_error": q_error(est, actual)})
            out[sid] = {"plans": plans, "selectivity": sel}
    return out


# ------------------------------------------------------- candidate sizes

def candidate_ddl(space: dict) -> list[tuple[str, str]]:
    return [(c["id"], c["ddl"].replace("CREATE INDEX ON",
                                       f"CREATE INDEX IF NOT EXISTS cand_{c['id'].lower()} ON", 1))
            for c in space["index"]]


def build_indexes(dsn: str, ddl: list[str]) -> dict[str, float]:
    seconds = {}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        conn.execute("SET statement_timeout = 0")
        for sql in ddl:
            t0 = time.perf_counter()
            conn.execute(sql)
            seconds[sql] = round(time.perf_counter() - t0, 1)
        conn.execute("ANALYZE")
    return seconds


def index_sizes(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        return dict(conn.execute(
            "SELECT indexrelname, pg_relation_size(indexrelid) FROM pg_stat_user_indexes "
            "WHERE relname IN ('accounts','campaigns','contacts','events') "
            "ORDER BY indexrelname").fetchall())


# ------------------------------------------------------------------- report

def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def build_report(p: Prepared, man: dict, cap_result: dict, totals: dict, diag: dict,
                 lm: dict, dataset: dict) -> dict:
    r = lm["report"]
    floor_obs = {q: tail_floor(q, r["tail_beyond"]) for q in (95, 99)}
    summary = cap_result["summary"]
    grand = sum(t["total_ms"] for t in totals.values()) or 1.0
    rows = []
    for sid, tmpl in sorted(p.templates.items()):
        sh = summary["by_shape"].get(sid, {})
        obs = sh.get("sample_count", 0)
        d = diag.get(sid, {"plans": [], "selectivity": []})
        ok_plans = [x for x in d["plans"] if "execution_ms" in x]
        sel = [x for x in d["selectivity"] if x.get("actual") is not None]
        rows.append({
            "shape": sid, "label": shapes.label(tmpl), "template": tmpl,
            "log_count": man["shapes"][sid][split.PROFILING],
            "log_share": round(man["shapes"][sid][split.PROFILING]
                               / man["slices"][split.PROFILING]["entries"], 4),
            "capture": {k: sh.get(k) for k in ("sample_count", "ok_count", "error_count",
                                                "timeout_count", "p50_ms", "p95_ms", "p99_ms",
                                                "max_ms")},
            "tail_supported": {f"p{q}": obs >= f for q, f in floor_obs.items()},
            "total_ms": round(totals.get(sid, {}).get("total_ms", 0.0), 1),
            "time_share": round(totals.get(sid, {}).get("total_ms", 0.0) / grand, 4),
            "pg_stat_statements": cap_result["pg_stat_statements"].get(sid),
            "plan": {
                "samples": len(d["plans"]),
                "timeouts": sum(1 for x in d["plans"] if "timeout_ms" in x),
                "median_ms": _median([x["execution_ms"] for x in ok_plans]),
                "scans": sorted({s for x in ok_plans for s in x["scans"]}),
                "median_rows_removed": _median([x["rows_removed"] for x in ok_plans]),
                "max_workers_launched": max((x["workers_launched"] for x in ok_plans),
                                            default=0),
                "spills": sum(1 for x in ok_plans if x["spills"]),
            },
            "selectivity": {
                "samples": len(d["selectivity"]),
                "median": _median([x["selectivity"] for x in sel]),
                "max": max((x["selectivity"] for x in sel), default=None),
                "median_q_error": _median([x["q_error"] for x in sel]),
                "max_q_error": max((x["q_error"] for x in sel), default=None),
            },
        })
    rows.sort(key=lambda x: -x["total_ms"])
    cum, dominant = 0.0, []
    for x in rows:
        if cum >= DOMINANT_SHARE:
            break
        dominant.append(x["shape"])
        cum += x["time_share"]
    return {
        "headline": {
            "log_rows_profiled": man["slices"][split.PROFILING]["entries"],
            "log_rows_total": man["log"]["entries"],
            "shapes": len(rows),
            "dataset_rows": sum(t["rows"] for t in dataset["tables"].values()),
            "dataset_bytes": dataset["database_bytes"],
            "dominant_shapes": dominant,
            "dominant_time_share": round(cum, 4),
        },
        "capture": {
            "observations": summary["overall"]["sample_count"],
            "throughput_qps": round(summary["throughput_qps"], 1),
            "probe": summary["probe"],
            "aborted": summary["aborted"],
            "stats_validity": cap_result["stats_validity"],
            "server": summary["server"],
            "cache_evidence": summary["cache_evidence"],
        },
        "tail_floor_observations": {f"p{q}": f for q, f in floor_obs.items()},
        "shapes": rows,
    }
