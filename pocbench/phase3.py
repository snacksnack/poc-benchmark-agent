"""Phase 3 on Neon (RC1-631): both baselines through the development entry
point, plus the evaluation entry point's gate.

Sequence for `run_baselines`:

1. Open the session: frozen criteria via load_verified, record opened.
2. Per baseline, a child branch of the corpus at the build tier. The first
   one carries the preflight probe (a slow or jittery network stops the job
   here, before index builds or runs spend compute, R9) and the slice
   rebuild (a split that differs from the manifest stops it too).
3. Build the configuration's indexes, resize to its tier (the resize
   restarts), read the live file-cache limit, grow the eviction filler past
   it (ADR 0021).
4. Run the development protocol; pool.
5. In a finally block: Neon's consumption counters, branch deletion, the
   execution-cost lines, job.json.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pocbench import config, criteria, harness, phase1, profile, slices
from pocbench.neon import Neon, Variant

OUT = Path("runs/phase3")


@dataclass
class NeonTarget:
    config: harness.Config
    dsn: str
    neon: Neon
    variant: Variant
    probe_query: str
    shared_compute: bool
    platform: str = "neon"
    can_restart: bool = True

    def restart(self) -> dict:
        t0 = time.monotonic()
        self.neon.restart(self.variant.endpoint_id)
        api = time.monotonic() - t0
        return {"restart_api_seconds": round(api, 3)} | harness.wait_for_connect(
            self.dsn, self.probe_query)

    def evict(self) -> dict:
        return harness.evict_scan(self.dsn)


@dataclass
class Meter:
    """Compute held per endpoint, by tier: the benchmark's own execution
    cost, kept apart from any projected operating cost."""

    segments: dict[str, list] = field(default_factory=dict)

    def start(self, name: str, cu: float) -> None:
        self.segments[name] = [[cu, time.monotonic(), None]]

    def resize(self, name: str, cu: float) -> None:
        now = time.monotonic()
        self.segments[name][-1][2] = now
        self.segments[name].append([cu, now, None])

    def stop(self, name: str) -> None:
        if name in self.segments and self.segments[name][-1][2] is None:
            self.segments[name][-1][2] = time.monotonic()

    def cu_hours(self) -> dict[str, float]:
        return {n: round(sum(cu * ((end or time.monotonic()) - st) for cu, st, end in segs)
                         / 3600, 4) for n, segs in self.segments.items()}


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sizes(dsn: str) -> dict:
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS neon")
        except psycopg.Error:
            pass  # stock Postgres: file-cache evidence is recorded as unavailable
        db = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        filler = conn.execute(
            "SELECT coalesce(pg_total_relation_size(to_regclass(%s)), 0)",
            (harness.FILLER_TABLE,)).fetchone()[0]
        lfc = harness.file_cache_bytes(conn)
    idx = {k: v for k, v in profile.index_sizes(dsn).items() if not k.endswith("_pkey")}
    return {"database_bytes_excluding_filler": db - filler, "secondary_index_bytes": idx,
            "file_cache_bytes": lfc}


def cost_lines(result: dict, space: dict) -> dict:
    """Four separate lines (ticket): benchmark execution, index build, index
    storage, projected operating cost. Never summed into one number."""
    c = space["cost"]
    price, storage = c["cu_hour_usd"], c["storage_gb_month_usd"]
    held = result.get("cu_hours_held", {})
    reported = {n: b.get("compute_time_seconds") for n, b in
                result.get("neon_branch_consumption", {}).items()}
    lines = {
        "benchmark_execution": {
            "cu_hours_held": held, "usd": round(sum(held.values()) * price, 4),
            "neon_reported_compute_time_seconds": reported,
            "note": "compute held by this job (fixed size, scale-to-zero off); Neon's own "
                    "counters are beside it and may lag"},
        "configs": {}}
    build_cu = space["capture"]["build_cu"]
    for name, cfg in result.get("configs", {}).items():
        build_s = sum(cfg.get("index_build_seconds", {}).values())
        idx_gb = sum(cfg["sizes"]["secondary_index_bytes"].values()) / 1e9
        db_gb = cfg["sizes"]["database_bytes_excluding_filler"] / 1e9
        lines["configs"][name] = {
            "index_build_usd": round(build_s / 3600 * build_cu * price, 4),
            "index_build_seconds": build_s,
            "index_storage_usd_per_month": round(idx_gb * storage, 4),
            "projected_operating_usd_per_month": round(
                cfg["cu"] * harness.HOURS_PER_MONTH * price + db_gb * storage, 2),
            "projected_basis": f"{cfg['cu']:g} CU always on x {harness.HOURS_PER_MONTH} h + "
                               f"{db_gb:.2f} GB storage (indexes included, filler excluded)",
        }
    return lines


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def run_baselines(out: Path, database: str, role: str, levels: list[int] | None = None) -> dict:
    s, doc = harness.open_session("development", out / "raw", out / "record.jsonl")
    prof = json.loads((config.REPO_ROOT / "results/phase1/profile.json").read_text())
    configs = harness.baselines(doc, prof)
    space = s.space
    neon, meter, stamp = Neon(), Meter(), _stamp()
    corpus_id = neon.branch_id(space["corpus"]["branch"])
    if corpus_id is None:
        raise RuntimeError("no corpus branch")
    created: list[Variant] = []
    result: dict = {"started_at": stamp, "entry_point": "development", "configs": {},
                    "criteria_sha256": s.method.criteria_sha256, "runner": s.runner}
    try:
        targets = {}
        for cfg in configs:
            build_cu = space["capture"]["build_cu"]
            v = neon.create_variant(f"p3-{cfg.name}-{stamp}", build_cu, parent_id=corpus_id)
            created.append(v)
            meter.start(v.name, build_cu)
            neon.set_suspend_timeout(v.endpoint_id, -1)
            uri = neon.connection_uri(v.branch_id, database, role)
            if "preflight" not in result:
                result["preflight"] = harness.preflight(uri, s.lm, s.method.probe_rule)
                harness.require_network(result["preflight"])
                # Fail in seconds, not after the index builds, if the split
                # cannot be rebuilt exactly.
                with psycopg.connect(uri, autocommit=True, connect_timeout=30) as conn:
                    rebuilt = slices.rebuild(conn)
            build = profile.build_indexes(uri, list(cfg.ddl)) if cfg.ddl else {}
            neon.resize(v.endpoint_id, cfg.cu)
            meter.resize(v.name, cfg.cu)
            sizes = _sizes(uri)
            if not sizes["file_cache_bytes"]:
                raise RuntimeError(f"{v.name}: no file-cache limit in pg_settings; the "
                                   "eviction filler cannot be sized (ADR 0021)")
            filler = harness.ensure_filler(
                uri, int(sizes["file_cache_bytes"] * harness.FILLER_FACTOR))
            targets[cfg.name] = NeonTarget(
                cfg, uri, neon, v, s.lm["probe"]["query"],
                cfg.cu in space["tiers"]["shared_compute_units"])
            result["configs"][cfg.name] = {
                "cu": cfg.cu, "config_sha256": cfg.sha256, "branch": v.name,
                "index_build_seconds": build, "sizes": sizes, "filler": filler}
        result["protocol"] = harness.development(s, targets, rebuilt, levels)
        result["pooled_cells"] = len(harness.pool(s.rows, s.raw_root, s.method))
    except harness.NetworkRejected as e:
        result["network_rejected"] = str(e)
        raise
    finally:
        consumption = {}
        for v in created:
            try:
                b = neon.branch(v.branch_id)
                consumption[v.name] = {k: b.get(k) for k in
                                       ("compute_time_seconds", "active_time_seconds",
                                        "written_data_bytes", "data_transfer_bytes")}
            except Exception as e:  # noqa: BLE001 - a missing counter must not block cleanup
                consumption[v.name] = {"unavailable": str(e)[:200]}
            meter.stop(v.name)
        result["neon_branch_consumption"] = consumption
        result["cu_hours_held"] = meter.cu_hours()
        phase1._cleanup(neon, created, result)
        result["cost"] = cost_lines(result, space)
        result["finished_at"] = _stamp()
        _write(out / "job.json", result)
    return result


def run_evaluation() -> dict:
    """The evaluation gate on Neon. It reads the criteria hash and refuses
    before opening the record or creating a branch unless a frozen candidate
    or refusal record verifies. Phase 4 (RC1-632) owns candidate branches and
    wires the held-out run itself through harness.evaluation."""
    frozen = harness.require_frozen(harness.criteria_sha256(criteria.load_verified()))
    raise NotImplementedError(
        f"frozen {frozen.kind} {frozen.sha256[:12]} verified; the held-out run on Neon is "
        "wired in Phase 4 (RC1-632)")
