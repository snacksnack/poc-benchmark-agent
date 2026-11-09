"""The adaptive loop's log as a page a customer can read (RC1-635; ADR 0027).

`results/phase4/loop-log.jsonl` is the audit trail of the recommendation step:
every proposal Claude made, what Python measured, and why the loop stopped.
It is hash-chained JSON, which proves it was not edited and is unreadable.
This renders it in order, in full, without summarizing the model's words.

The log is verified before it is read: the chain through `record.verify`, and
the file's sha256 against the one the frozen output pinned, so the page cannot
describe a log other than the one the refusal was frozen from.
"""

import hashlib
from pathlib import Path

from pocbench import candidate, criteria, harness, phase4, record

LOG_PATH = phase4.STATE / "loop-log.jsonl"
DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "loop-log.md"


def load(path: Path = LOG_PATH) -> tuple[list[dict], dict, str]:
    """Rows, the frozen output, and the log's sha256, all verified.

    The freeze pins the log as it stood when the loop stopped, then appends one
    last row naming the frozen file's hash. So the check runs both ways: the
    rows before the last hash to the pin, and the last row names the frozen file.
    """
    rows = record.verify(path)
    frozen = candidate.load_frozen(harness.criteria_sha256(criteria.load_verified()))
    lines = path.read_bytes().splitlines(keepends=True)
    before = hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    if before != frozen.doc["loop_log_sha256"]:
        raise record.RecordError(f"{path.name} up to the freeze hashes to {before[:12]}, not "
                                 f"the {frozen.doc['loop_log_sha256'][:12]} the frozen output "
                                 "pinned")
    last = rows[-1]
    if last.get("kind") != "frozen" or last.get("frozen_sha256") != frozen.sha256:
        raise record.RecordError(f"{path.name} does not end with the freeze of "
                                 f"{frozen.sha256[:12]}")
    return rows, frozen.doc, hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(p: dict | None) -> str:
    if not p:
        return "none"
    cu = p["cu"]
    return f"{cu:g} CU, indexes {', '.join(p['index_ids'])}"


def _counts(pred: dict) -> str:
    c = pred["counts"]
    return (f"{c.get('met', 0)} met, {c.get('failed', 0)} failed, "
            f"{c.get('inconclusive', 0)} inconclusive, {c.get('unsupported', 0)} unsupported "
            f"of {pred['criteria_total']}")


def _quote(text: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in text.splitlines()]


def _proposal(r: dict) -> list[str]:
    head = (f"### {r['at']} · iteration {r['iteration']}: Claude proposes "
            + ("an experiment" if r["action"] == "experiment" else "to stop"))
    out = [head, ""]
    if r["action"] == "experiment":
        v = r["validator"]
        out += [f"**Bundle:** {_bundle(r['proposal'])}. Python's validator: "
                f"{'accepted' if v['valid'] else 'rejected'}, estimated "
                f"{v['estimated_cu_hours']:g} CU-h.", ""]
    out += ["**Why:**", "", *_quote(r["why"]), "",
            "**Hypothesis:**", "", *_quote(r["hypothesis"]), "",
            "**Expected:**", "", *_quote(r["expected"]), ""]
    if r.get("stop_reason"):
        out += ["**Stop reason:**", "", *_quote(r["stop_reason"]), ""]
    out += [f"Model spend for this call: ${r['model_usd']:.4f}.", ""]
    return out


# What a rejection's own message got wrong, keyed by the text that identifies it.
# The log is append-only, so a later diagnosis is printed beside the row.
LATER_DIAGNOSIS = {
    "probe measured": "Diagnosed after the run: this was the load generator, not the "
                      "network. It opened the measured window before every session had "
                      "connected to a just-restarted compute. Fixed before the next "
                      "attempt (docs/phase4-findings.md, section 4).",
}


def _aborted(r: dict) -> list[str]:
    out = [f"### {r['at']} · iteration {r['iteration']}: experiment aborted", "",
           f"`{r['bundle']}` held {r['cu_hours']:.2f} CU-h and produced no valid result. "
           "Python's reason:", "", *_quote(r["why"]), ""]
    for key, note in LATER_DIAGNOSIS.items():
        if key in r["why"]:
            out += [note, ""]
    return out


def _experiment(r: dict) -> list[str]:
    b = r["bundle"]
    build = r["detail"]["build"]
    idx = sum(build["sizes"]["secondary_index_bytes"].values())
    return [f"### {r['at']} · iteration {r['iteration']}: Python measures `{b['key']}`", "",
            f"- Compute held: {r['cu_hours']:.2f} CU-h over {len(r['run_ids'])} runs "
            f"(the candidate and today's baseline, interleaved).",
            f"- Index builds: {len(build['index_build_seconds'])} indexes, "
            f"{sum(build['index_build_seconds'].values()):.0f} s, {idx / 1e9:.2f} GB.",
            f"- Compute file cache: {build['sizes']['file_cache_bytes'] / 1e9:.1f} GB.",
            f"- Predicted against the criteria at the time: {_counts(r['prediction'])} "
            "(profiling slice, development protocol).",
            ""]


def _stop(r: dict) -> list[str]:
    who = "the budget" if r["budget"] else f"the {r['by']}"
    return [f"### {r['at']} · loop stops", "",
            f"Stopped by {who}. Python records the reason as given:", "",
            *_quote(r["reason"]), ""]


def _frozen(r: dict) -> list[str]:
    return [f"### {r['at']} · output frozen", "",
            f"**{r['output'].title()} `{r['id']}`, class {r['class']}.** Frozen as "
            f"`candidates/frozen.json`, sha256 `{r['frozen_sha256']}`. The held-out "
            "evaluation that followed refused to start without this pin.", ""]


RENDER = {"proposal": _proposal, "aborted": _aborted, "experiment": _experiment,
          "stop": _stop, "frozen": _frozen}


def render(rows: list[dict], frozen: dict, sha: str) -> str:
    h = rows[0]
    b = h["budgets"]
    s = frozen["spent"]
    lines = [
        "# Loop log: the recommendation step, in order",
        "",
        "Generated by `pocbench loop-log-doc` from `results/phase4/loop-log.jsonl`. "
        "Do not edit by hand.",
        "",
        "Claude proposes; Python validates, measures and decides (ADR 0022). Every "
        "row below is a line of the hash-chained log, rendered in full. The model's "
        "words are quoted as written, including predictions that turned out wrong.",
        "",
        "## Pins and budgets",
        "",
        f"- Log sha256 `{sha}`, chain verified over {len(rows)} rows. Every row "
        f"before the freeze hashes to `{frozen['loop_log_sha256']}`, the pin the "
        "frozen output carries; the last row names the frozen file in return.",
        f"- Criteria sha256 `{h['criteria_sha256']}`.",
        f"- Config space sha256 `{h['config_space_sha256']}`.",
        f"- Load model sha256 `{h['load_model_sha256']}`.",
        f"- Model `{h['model']}`.",
        f"- Budgets: {b['max_experiments']} experiments, {b['max_compute_cu_hours']:g} CU-h "
        f"of compute, ${b['max_model_spend_usd']:.2f} of model spend.",
        f"- Spent: {s['experiments']} experiments, {s['compute_cu_hours']:.1f} CU-h, "
        f"${s['model_usd']:.2f}.",
        "",
        "The development predictions below are what the loop saw at the time. "
        "`docs/results.md` re-predicts both bundles against the final pool of "
        "today's baseline, so its counts differ slightly for the first bundle.",
        "",
        "## The log",
        "",
    ]
    for r in rows[1:]:
        lines += RENDER[r["kind"]](r)
    return "\n".join(lines).rstrip() + "\n"


def render_committed() -> str:
    return render(*load())

