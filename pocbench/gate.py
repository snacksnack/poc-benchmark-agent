"""Phase 0 noise gate plus the one end-to-end variant comparison.

Sequence, all on child branches of the spike dataset so every configuration
reads identical pages:

1. Create the indexed variant (I1, I2, I5, I7) at the gate's CU and build its
   indexes. Create the default variant (primary keys only) at the same CU.
2. Gate: N warm runs of the indexed variant at the gate's session count.
3. Variant vs default: one warm run of the default branch at the same level,
   compared with the indexed variant's runs.
4. Supplementary (non-gating): the indexed variant's data on shared compute.
5. Delete every branch created here, in a finally block. Deleting is part of
   the exit criterion: a variant must be destroyable, not just creatable.
"""

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pocbench import config, loadgen, spike, stats
from pocbench.neon import Neon, Variant


def _apply(dsn: str, statements: list[str]) -> float:
    t0 = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
        for sql in statements:
            conn.execute(sql)
    return time.perf_counter() - t0


def _summary(d: dict) -> stats.Summary:
    return stats.Summary(**d["overall"])


def run_gate(out_dir: Path, database: str, role: str, measure_s: float | None = None) -> dict:
    lm = config.load_model()
    space = config.config_space()
    g = space.data["noise_gate"]
    neon = Neon()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    instances = spike.instances(seed=lm.data["mix"]["seed"])
    created: list[Variant] = []
    result: dict = {
        "started_at": stamp,
        "load_model_sha256": lm.sha256,
        "config_space_sha256": space.sha256,
        "gate": g,
    }

    def spec(uri: str, label: str, sessions: int) -> loadgen.RunSpec:
        return loadgen.RunSpec.from_load_model(lm.data, lm.sha256, dsn=uri, label=label,
                                               sessions=sessions, cache_state=g["cache_state"],
                                               measure_s=measure_s)

    try:
        indexed = neon.create_variant(f"p0-indexed-{stamp}", g["cu"])
        created.append(indexed)
        default = neon.create_variant(f"p0-default-{stamp}", g["cu"])
        created.append(default)
        indexed_uri = neon.connection_uri(indexed.branch_id, database, role)
        default_uri = neon.connection_uri(default.branch_id, database, role)
        result["variant_build_seconds"] = round(_apply(indexed_uri, spike.VARIANT_INDEXES), 1)

        gate_runs = []
        for i in range(1, g["runs"] + 1):
            label = f"gate-{i}"
            gate_runs.append(loadgen.run(spec(indexed_uri, label, g["sessions"]), instances,
                                         out_dir / label))
        verdict = stats.noise_gate([_summary(r) for r in gate_runs], g["go_if_spread_below"],
                                   lm.data["errors"]["error_ceiling_fraction"])
        result["gate_runs"] = [r["overall"] | {"probe": r["probe"],
                                               "throughput_qps": r["throughput_qps"]}
                               for r in gate_runs]
        result["verdict"] = {"go": verdict.go, "p95_spread": verdict.p95_spread,
                             "p99_spread": verdict.p99_spread, "threshold": verdict.threshold,
                             "reasons": list(verdict.reasons)}

        base = loadgen.run(spec(default_uri, "default", g["sessions"]), instances,
                           out_dir / "default")
        result["variant_vs_default"] = {
            "sessions": g["sessions"],
            "default": base["overall"] | {"throughput_qps": base["throughput_qps"],
                                          "by_shape": base["by_shape"]},
            "indexed_median_of_gate_runs": {
                k: statistics.median(r["overall"][k] for r in gate_runs)
                for k in ("p50_ms", "p95_ms", "p99_ms")
            } | {"throughput_qps": statistics.median(r["throughput_qps"] for r in gate_runs)},
        }

        sup = g.get("supplementary")
        if sup:
            shared = neon.create_variant(f"p0-shared-{stamp}", sup["cu"],
                                         parent_id=indexed.branch_id)
            created.append(shared)
            shared_uri = neon.connection_uri(shared.branch_id, database, role)
            sup_runs = [loadgen.run(spec(shared_uri, f"shared-{i}", g["sessions"]), instances,
                                    out_dir / f"shared-{i}")
                        for i in range(1, sup["runs"] + 1)]
            p99s = [r["overall"]["p99_ms"] for r in sup_runs if r["overall"]["p99_ms"]]
            result["supplementary"] = {
                "cu": sup["cu"], "gating": False,
                "runs": [r["overall"] | {"probe": r["probe"]} for r in sup_runs],
                "p99_spread": stats.spread(p99s) if len(p99s) > 1 else None,
            }
    finally:
        deleted = []
        for v in reversed(created):
            try:
                neon.delete_variant(v)
                deleted.append(v.name)
            except Exception as e:  # noqa: BLE001 - report every branch we could not delete
                result.setdefault("delete_failures", []).append(f"{v.name}: {e}")
        result["branches_created"] = [v.name for v in created]
        result["branches_deleted"] = deleted
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gate.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    return result


def _fmt(v, unit="") -> str:
    return "n/a" if v is None else f"{v:,.1f}{unit}"


def _row(label: str, r: dict) -> str:
    probe = r.get("probe") or {}
    return (f"| {label} | {r['sample_count']:,} | {_fmt(r['p50_ms'])} | {_fmt(r['p95_ms'])} | "
            f"{_fmt(r['p99_ms'])} | {_fmt(r['max_ms'])} | "
            f"{r['error_count'] + r['timeout_count']} | "
            f"{'yes' if r['p99_stable'] else 'no'} | {_fmt(probe.get('p50_ms'))} / "
            f"{_fmt(probe.get('p99_ms'))} |")


def render(result: dict, run_url: str | None = None) -> str:
    g, v = result["gate"], result["verdict"]
    head = ("| Run | Samples | p50 ms | p95 ms | p99 ms | max ms | Errors + timeouts | "
            "Stable p99 | Probe p50 / p99 ms |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: "
            "| --- | --- |")
    out = [
        "# Noise gate (Phase 0)",
        "",
        f"Run {result['started_at']}"
        + (f", [workflow run]({run_url})" if run_url else "")
        + f". Load model sha256 `{result['load_model_sha256'][:12]}`, config space "
        f"`{result['config_space_sha256'][:12]}`. Generated by `pocbench gate-report` from "
        "`results/phase0/gate.json`.",
        "",
        f"## Verdict: {'GO' if v['go'] else 'FALLBACK'}",
        "",
        f"Spread is (max - min) / median of the per-run percentile across {g['runs']} runs. "
        f"Go requires both below {v['threshold']:.0%}, the smallest p99 improvement the "
        "criteria may call a pass (ADR 0009).",
        "",
        "| Percentile | Run-to-run spread | Threshold |",
        "| --- | ---: | ---: |",
        f"| p95 | {v['p95_spread']:.1%} | {v['threshold']:.0%} |",
        f"| p99 | {v['p99_spread']:.1%} | {v['threshold']:.0%} |",
        "",
    ]
    if v["reasons"]:
        out += ["Reasons:", ""] + [f"- {r}" for r in v["reasons"]] + [""]
    out += [
        f"## Gate runs: indexed variant, {g['cu']} CU, {g['sessions']} sessions, "
        f"{g['cache_state']}",
        "",
        head,
    ]
    out += [_row(f"gate-{i}", r) for i, r in enumerate(result["gate_runs"], 1)]
    vd = result["variant_vs_default"]
    d, ix = vd["default"], vd["indexed_median_of_gate_runs"]
    out += [
        "",
        f"## Variant versus default, {vd['sessions']} sessions",
        "",
        "The one end-to-end comparison the Phase 0 exit asks for. Indexed = I1, I2, I5, I7 "
        "(median of the gate runs); default = primary keys only. Same CU, same data, same "
        "load model. One default run, so this shows the pipeline works, not an effect size.",
        "",
        "| Config | Throughput qps | p50 ms | p95 ms | p99 ms |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| default | {d['throughput_qps']:,.1f} | {_fmt(d['p50_ms'])} | {_fmt(d['p95_ms'])} | "
        f"{_fmt(d['p99_ms'])} |",
        f"| indexed | {ix['throughput_qps']:,.1f} | {_fmt(ix['p50_ms'])} | "
        f"{_fmt(ix['p95_ms'])} | {_fmt(ix['p99_ms'])} |",
        "",
        f"Default run detail: {d['sample_count']:,} samples, "
        f"{d['error_count'] + d['timeout_count']} errors + timeouts, p99 "
        f"{'stable' if d['p99_stable'] else 'flagged unstable (too few samples)'}.",
        "",
    ]
    sup = result.get("supplementary")
    if sup:
        spread = sup["p99_spread"]
        out += [
            f"## Supplementary (not gating): {sup['cu']} CU shared compute",
            "",
            "Same indexed data on shared compute, for the record behind ADR 0015. p99 spread "
            + (f"{spread:.1%}." if spread is not None else "n/a."),
            "",
            head,
        ]
        out += [_row(f"shared-{i}", r) for i, r in enumerate(sup["runs"], 1)]
        out.append("")
    out += [
        "## Housekeeping",
        "",
        f"- Indexed variant build: {result.get('variant_build_seconds')} s.",
        f"- Branches created: {', '.join(result['branches_created'])}.",
        f"- Branches deleted: {', '.join(result['branches_deleted'])}.",
    ]
    if result.get("delete_failures"):
        out += [f"- **Delete failures:** {'; '.join(result['delete_failures'])}"]
    out += [
        "",
        "## Limits of this result",
        "",
        "- The spike dataset is cache-resident at 1 CU, so this spread is an optimistic "
        "floor for the Phase 1 dataset (R10). A fallback here is decisive; a go is "
        "necessary, not sufficient.",
        "- Real uncertainty for Phase 4 results comes from interleaved baselines in those "
        "runs, never from this gate (ADR 0009).",
        "",
    ]
    return "\n".join(out)
