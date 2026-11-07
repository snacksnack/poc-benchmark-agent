"""Render the Phase 3 baseline record as markdown (RC1-631).

Input: a run directory holding record.jsonl, job.json and the raw logs. The
record's chain is verified and every pooled number is recomputed from the
raw logs, whose hashes must match the record.
"""

import json
import shutil
from pathlib import Path

from pocbench import harness, record


def _ms(x) -> str:
    return "-" if x is None else f"{x:,.1f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def build(run_dir: Path) -> tuple[list[dict], list[dict], dict, harness.Method]:
    rows = record.verify(run_dir / "record.jsonl")
    job = json.loads((run_dir / "job.json").read_text()) if (run_dir / "job.json").exists() else {}
    s, _ = harness.open_session("development", run_dir / "raw", run_dir / "record.jsonl")
    pooled = harness.pool(rows, run_dir / "raw", s.method)
    return rows, pooled, job, s.method


def publish(run_dir: Path, results: Path) -> list[dict]:
    """Copy the durable outputs (record, job, pooled cells) into results/."""
    rows, pooled, job, _ = build(run_dir)
    results.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / "record.jsonl", results / "record.jsonl")
    if job:
        shutil.copyfile(run_dir / "job.json", results / "job.json")
    (results / "baselines.json").write_text(json.dumps(pooled, indent=2, default=str) + "\n")
    return pooled


def render(run_dir: Path, run_url: str | None = None) -> str:
    rows, pooled, job, m = build(run_dir)
    runs = [r for r in rows if r.get("kind") == "run"]
    scored = [r for r in runs if r["status"] == record.SCORED]
    rejected = [r for r in runs if r["status"] == record.REJECTED]
    head = rows[0]
    out = ["# Phase 3: both baselines through the harness", "",
           f"RC1-631. Development entry point, profiling slice. Criteria sha256 "
           f"`{head['criteria_sha256'][:16]}...`, load model `{head['load_model_sha256'][:16]}"
           f"...`, config space `{head['config_space_sha256'][:16]}...`."
           + (f" Run: {run_url}." if run_url else ""), "",
           "Every number is **performance at N concurrent sessions in a closed loop**, not a "
           "sustained arrival rate (ADR 0012). Latency is **net**: client latency minus the "
           "run's median probe round trip (criteria method, R9). Level 1 is diagnostic only. "
           "Mix-level numbers are reported, never scored (R12).", "",
           f"{len(scored)} scored runs, {len(rejected)} rejected by the network or server "
           f"checks, {len(pooled)} cells. Record chain verified: {len(rows)} lines.", ""]

    pf = job.get("preflight")
    out += ["## Network gate", ""]
    if pf:
        out.append(f"Preflight: probe p50 {_ms(pf['p50_ms'])} ms, p99 - p50 "
                   f"{_ms(pf['jitter_ms'])} ms over {pf['samples']} round trips: "
                   f"{'accepted' if pf['valid'] else 'REJECTED'}. Rule: p50 <= "
                   f"{m.probe_rule['probe_p50_ms_at_most']:g} ms and p99 - p50 <= "
                   f"{m.probe_rule['probe_p99_minus_p50_ms_at_most']:g} ms.")
    p50s = [r["probe"]["p50_ms"] for r in scored if r["probe"]["p50_ms"] is not None]
    jit = [r["probe"]["jitter_ms"] for r in scored if r["probe"]["jitter_ms"] is not None]
    if p50s:
        out.append(f"Scored runs: probe p50 {min(p50s):.1f}-{max(p50s):.1f} ms, p99 - p50 "
                   f"{min(jit):.1f}-{max(jit):.1f} ms.")
    if rejected:
        out += ["", "| Run | Reasons |", "| --- | --- |"]
        out += [f"| {r['run_id']} | {'; '.join(r['reasons'])} |" for r in rejected]
    out.append("")

    out += ["## Cells", "",
            "| Config | CU | Level | Cache state | Reps | Aborted | OK qps (median) | "
            "Mix p99 net (reported) | Cost per successful query |",
            "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for c in pooled:
        mix = c["overall_reported_never_scored"]
        cpq = c["cost_per_successful_query_usd"]
        out.append(f"| {c['config']} | {c['cu']:g} | {c['level']}"
                   f"{' (diag.)' if c['diagnostic_only'] else ''} | {c['cache_state']} "
                   f"| {c['repetitions']} | {c['aborted_runs']} | "
                   f"{c['successful_qps']['median']:,.1f} | {_ms(mix.get('p99', {}).get('ms'))} "
                   f"| {'-' if cpq is None else f'${cpq:.2e}'} |")
    out.append("")

    out += ["## Per shape", "",
            "Net ms. `*` = tail not supported (ADR 0018: 100 observations from 10 distinct "
            "instances beyond the percentile). Spread = (max - min) / median of the "
            "per-repetition p99.", "",
            "| Config | Level | Cache state | Shape | Obs | Errors + timeouts | p50 | p95 | "
            "p99 [95% CI] | p99 spread |",
            "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |"]
    for c in pooled:
        for sid, v in c["by_shape"].items():
            n = v["net"]

            def cell(p, n=n):
                x = n.get(f"p{p}")
                return "-" if not x else f"{_ms(x['ms'])}{'' if x['supported'] else '*'}"
            p99 = n.get("p99")
            ci = f" [{_ms(p99['ci95_ms'][0])}, {_ms(p99['ci95_ms'][1])}]" if p99 else ""
            flag = " **over**" if v["over_error_ceiling"] else ""
            out.append(f"| {c['config']} | {c['level']} | {c['cache_state']} | {sid} "
                       f"| {v['observations']:,} | {_pct(v['failure_fraction'])}{flag} "
                       f"| {cell(50)} | {cell(95)} | {cell(99)}{ci} "
                       f"| {_pct(p99['spread']) if p99 and p99['spread'] is not None else '-'} |")
    out.append("")

    out += ["## Cache evidence", "",
            "Named for the procedure (ADR 0013). Ratios are over the measured run; "
            "`post-restart-with-eviction` also shows the restart and the filler scan.", "",
            "| Config | Level | Cache state | File-cache hit ratio per rep | Restart to first "
            "query (s) | Filler scan (s) |", "| --- | ---: | --- | --- | --- | --- |"]
    for c in pooled:
        ratios = ", ".join(_pct(r.get("file_cache_hit_ratio")) for r in c["cache_ratios_per_rep"])
        starts = ", ".join(f"{s['restart_api_seconds'] + s['first_connect_seconds']:.1f}"
                           for s in c["startup_per_rep"]) or "-"
        scans = ", ".join(f"{x:.0f}" for x in c["eviction_scan_seconds_per_rep"]) or "-"
        out.append(f"| {c['config']} | {c['level']} | {c['cache_state']} | {ratios} "
                   f"| {starts} | {scans} |")
    out.append("")

    cost = job.get("cost")
    if cost:
        ex = cost["benchmark_execution"]
        out += ["## Cost", "", "Four lines, never summed (ticket).", "",
                f"- **Benchmark execution:** ${ex['usd']:.2f} for "
                f"{sum(ex['cu_hours_held'].values()):.2f} CU-h held by this job. "
                "Neon-reported compute seconds per branch: "
                f"{ex['neon_reported_compute_time_seconds']}.",
                "", "| Config | Index build | Index storage / month | Projected operating / month "
                "| Basis |", "| --- | ---: | ---: | ---: | --- |"]
        for name, c in cost["configs"].items():
            out.append(f"| {name} | ${c['index_build_usd']:.3f} "
                       f"({c['index_build_seconds']:.0f} s) "
                       f"| ${c['index_storage_usd_per_month']:.2f} "
                       f"| ${c['projected_operating_usd_per_month']:.2f} "
                       f"| {c['projected_basis']} |")
        out.append("")
    skipped = job.get("protocol", {}).get("skipped", [])
    if skipped:
        out += [f"Skipped cells: {len(skipped)} ({skipped[0]['why']}).", ""]
    return "\n".join(out) + "\n"
