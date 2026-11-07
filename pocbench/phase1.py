"""Phase 1 runs on Neon (RC1-629): corpus load, profile, tier probe.

Each creates the branches it needs and deletes them in a finally block, as
the Phase 0 gate does. The corpus branch itself is kept: it is the dataset.
Scale-to-zero is disabled per endpoint for the length of a run and restored
in the same finally (R2, R6).
"""

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pocbench import config, corpus, profile, spike
from pocbench.neon import Neon, Variant

RESTORE_SUSPEND_SECONDS = 0  # 0 = the project default (five minutes)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def corpus_branch(neon: Neon, space: dict) -> tuple[str, str]:
    """The corpus branch and its endpoint, created on first use."""
    name, cu = space["corpus"]["branch"], space["corpus"]["load_cu"]
    bid = neon.branch_id(name)
    if bid is None:
        v = neon.create_variant(name, cu, parent_id=neon.default_branch_id())
        return v.branch_id, v.endpoint_id
    return bid, neon.endpoint_id(bid)


def run_corpus_load(out: Path, database: str, role: str, fresh: bool) -> dict:
    space = config.config_space().data
    neon = Neon()
    bid, eid = corpus_branch(neon, space)
    uri = neon.connection_uri(bid, database, role)
    neon.set_suspend_timeout(eid, -1)
    try:
        record = corpus.load(uri, corpus.CorpusSpec.from_config(space), fresh=fresh)
    finally:
        neon.set_suspend_timeout(eid, RESTORE_SUSPEND_SECONDS)
    record["branch"] = space["corpus"]["branch"]
    _write(out / "corpus.json", record)
    return record


def profile_core(uri: str, out: Path, lm, sp, cap: dict, resize=None) -> dict:
    """The profile sequence against one database. `resize(cu)` switches the
    compute size (Neon only); without it the run stays on one size."""
    space = sp.data
    with psycopg.connect(uri, autocommit=True, connect_timeout=30) as conn:
        prepared = profile.prepare(conn, space, out)
        dataset = corpus.measure(conn)
    man = profile.manifest(prepared, space, lm.data)
    _write(out / "split-manifest.json", man)
    naive_seconds = profile.build_indexes(uri, prepared.naive_ddl)
    if resize:
        resize(cap["cu"])
    cap_result = profile.capture(uri, prepared, lm, cap, out / "capture")
    totals = profile.capture_totals(out / "capture", lm.data["timing"]["per_query_timeout_ms"])
    diag = profile.diagnose(uri, prepared, cap, space["split"]["seed"])
    _write(out / "diagnose.json", diag)
    report = profile.build_report(prepared, man, cap_result, totals, diag, lm.data, dataset)
    if resize:
        resize(cap["build_cu"])
    cand = profile.candidate_ddl(space)
    report.update(
        dataset=dataset, naive_ddl=prepared.naive_ddl, naive_build_seconds=naive_seconds,
        candidate_build_seconds=profile.build_indexes(uri, [d for _, d in cand]),
        index_bytes=profile.index_sizes(uri), capture_config=cap,
        load_model_sha256=lm.sha256, config_space_sha256=sp.sha256)
    _write(out / "profile.json", report)
    return report


def run_profile(out: Path, database: str, role: str, measure_s: float | None = None) -> dict:
    lm, sp = config.load_model(), config.config_space()
    cap = dict(sp.data["capture"])
    if measure_s is not None:
        cap["measure_seconds"] = measure_s
    neon = Neon()
    stamp = _stamp()
    corpus_id = neon.branch_id(sp.data["corpus"]["branch"])
    if corpus_id is None:
        raise RuntimeError("no corpus branch: run corpus-load first")
    created: list[Variant] = []
    result: dict = {"started_at": stamp}
    try:
        v = neon.create_variant(f"p1-capture-{stamp}", cap["build_cu"], parent_id=corpus_id)
        created.append(v)
        neon.set_suspend_timeout(v.endpoint_id, -1)
        uri = neon.connection_uri(v.branch_id, database, role)
        report = profile_core(uri, out, lm, sp, cap,
                              resize=lambda cu: neon.resize(v.endpoint_id, cu))
        result["stats_valid"] = report["capture"]["stats_validity"]["valid"]
    finally:
        _cleanup(neon, created, result)
        _write(out / "run.json", result)
    return result


def _cleanup(neon: Neon, created: list[Variant], result: dict) -> None:
    deleted = []
    for v in reversed(created):
        try:
            neon.delete_variant(v)
            deleted.append(v.name)
        except Exception as e:  # noqa: BLE001 - report every branch we could not delete
            result.setdefault("delete_failures", []).append(f"{v.name}: {e}")
    result["branches_created"] = [v.name for v in created]
    result["branches_deleted"] = deleted


TIER_SETTINGS = ("max_connections", "max_worker_processes", "max_parallel_workers",
                 "max_parallel_workers_per_gather", "work_mem", "maintenance_work_mem",
                 "shared_buffers", "effective_cache_size")


def _settings(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s) "
        "OR name LIKE 'neon.%%file_cache%%' ORDER BY name", (list(TIER_SETTINGS),)).fetchall()
    return {n: f"{s}{' ' + u if u else ''}" for n, s, u in rows}


def _explain(uri: str, sql: str, params: tuple) -> dict:
    with psycopg.connect(uri, autocommit=True, connect_timeout=30) as conn:
        plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0]
    return profile.plan_facts(plan)


def run_tier_probe(out: Path, database: str, role: str, sessions: int = 8) -> dict:
    """Phase 0 open question (docs/phase0-findings.md, finding 3): why the
    funnel scan ran slower at 1 CU than at 0.25 CU. Per tier, on the spike
    dataset where it was seen: server settings, the funnel plan alone, then
    the same plan from N concurrent sessions, recording workers launched."""
    space = config.config_space().data
    neon = Neon()
    stamp = _stamp()
    _, funnel_sql = spike.SHAPES["campaign_funnel"]
    hot = (1,)  # campaign 1 is the largest (u^2 skew)
    created: list[Variant] = []
    result: dict = {"started_at": stamp, "sessions": sessions, "tiers": {}}
    try:
        for cu in space["tiers"]["compute_units"]:
            v = neon.create_variant(f"p1-tier-{cu}-{stamp}".replace(".", "_"), cu)
            created.append(v)
            uri = neon.connection_uri(v.branch_id, database, role)
            with psycopg.connect(uri, autocommit=True, connect_timeout=30) as conn:
                settings = _settings(conn)
            alone = [_explain(uri, funnel_sql, hot) for _ in range(3)]
            with ThreadPoolExecutor(sessions) as pool:
                concurrent = list(pool.map(_explain, [uri] * (sessions * 2),
                                           [funnel_sql] * (sessions * 2),
                                           [hot] * (sessions * 2)))
            result["tiers"][str(cu)] = {
                "settings": settings,
                "alone": [{k: x[k] for k in ("execution_ms", "workers_planned",
                                              "workers_launched")} for x in alone],
                "concurrent": {
                    "median_ms": statistics.median(x["execution_ms"] for x in concurrent),
                    "workers_planned": sorted({x["workers_planned"] for x in concurrent}),
                    "workers_launched": [x["workers_launched"] for x in concurrent],
                },
            }
            neon.delete_variant(v)
            created.remove(v)
            result.setdefault("branches_deleted_inline", []).append(v.name)
    finally:
        _cleanup(neon, created, result)
        _write(out / "tier-probe.json", result)
    return result
