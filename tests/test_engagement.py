"""The engagement writeup, as tests (RC1-635).

`docs/engagement.md` is the one hand-written document that quotes the run's
figures, and it is the page a reader reaches first. Every other results page is
regenerated and diffed in CI; this one is prose, so each number in it is
re-derived here from the same verified evidence the results document uses.
A figure that drifts fails the suite instead of aging into a wrong claim on a
portfolio page.
"""

import json
import re

import pytest

from pocbench import config, loop_report, results, split

DOC = config.REPO_ROOT / "docs" / "engagement.md"
BEST = "4cu-I1+I2+I3+I4+I5+I6+I7+I8"
LEVEL = 32
SHAPE_NAMES = {"s01": "latest events per contact", "s02": "email lookup",
               "s03": "account timeline", "s04": "account contact list",
               "s05": "campaign funnel", "s06": "bounced contacts", "s07": "daily report",
               "s08": "segment preview", "s09": "campaign last-touch"}


@pytest.fixture(scope="module")
def ev() -> results.Evidence:
    return results.load_real()


@pytest.fixture(scope="module")
def held(ev):
    return results.held_out_verdicts(ev, ev.cells)


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


def _fail(cells: list[dict], cfg: str, sl: str | None, cs: str = "warm") -> dict[str, float]:
    (cell,) = [c for c in cells if c["config"] == cfg and c["level"] == LEVEL
               and c["cache_state"] == cs and (sl is None or c["slice"] == sl)]
    return {s: v["failure_fraction"] for s, v in cell["by_shape"].items()}


def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _met(v: dict) -> int:
    return v["counts"].get("met", 0)


# ------------------------------------------------------------ the answer

def test_the_answer_is_the_frozen_refusal(ev, doc):
    assert ev.frozen["kind"] == "refusal"
    assert f"Refusal `{ev.frozen['refusal_id']}`, class {ev.frozen['class']}" in doc
    assert f"the {len(ev.doc['criteria'])} success criteria" in doc


def test_the_shape_table_matches_the_cells(ev, doc):
    today = _fail(ev.cells, "naive", split.HELD_OUT)
    best = _fail(ev.dev_cells[f"x-{BEST}"], f"x-{BEST}", None)
    over = {s for s, f in best.items() if f > ev.load_model["errors"]["error_ceiling_fraction"]}
    assert over == {"s07", "s08", "s09"}
    for sid, name in SHAPE_NAMES.items():
        cell = f"**{_pct(best[sid])}**" if sid in over else _pct(best[sid])
        assert f"| {sid} {name} | {_pct(today[sid])} | {cell} |" in doc, sid


def test_todays_timeouts_quoted_in_the_summary(ev, doc):
    today = _fail(ev.cells, "naive", split.HELD_OUT)
    line = (f"Times out on {_pct(today['s07'], 0)} of daily reports, "
            f"{_pct(today['s02'], 0)} of email lookups, {_pct(today['s05'], 0)} of campaign "
            f"funnels and {_pct(today['s03'], 0)} of account timelines at {LEVEL} sessions")
    assert line in doc


def test_every_failure_was_a_timeout(ev, doc):
    errors = sum(s["errors"] for c in ev.cells for s in c["by_shape"].values())
    assert errors == 0
    assert ev.load_model["timing"]["per_query_timeout_ms"] == 5000
    assert "Every failure was a\nserver statement timeout at 5 s; none was an error." in doc


def test_criteria_met_counts(held, ev, doc):
    h = held[split.HELD_OUT]
    assert f"meets {_met(h['naive'])} of 80 criteria" in doc
    assert f"today's setup meets **{_met(h['naive'])} of 80**" in doc
    assert f"default (primary keys only) meets **{_met(h['default'])} of 80**" in doc
    (best,) = [t for t in ev.frozen["tested"] if t["bundle"] == BEST]
    assert ev.frozen["best_tested"]["bundle"] == BEST
    assert f"meets {best['predicted_counts']['met']} of 80 (development prediction)" in doc


def test_s07_capacity_figures(ev, doc):
    best_warm = _fail(ev.dev_cells[f"x-{BEST}"], f"x-{BEST}", None)
    best_cold = _fail(ev.dev_cells[f"x-{BEST}"], f"x-{BEST}", None, "post-restart-with-eviction")
    assert f"{_pct(best_warm['s07'], 0)} of calls time out warm, {_pct(best_cold['s07'], 0)} " \
           "after a restart" in doc
    (c041,) = [u for u in ev.frozen["best_tested"]["unmet"] if u["id"] == "C041"]
    assert (c041["shape"], c041["level"], c041["support"]) == ("s07", LEVEL, "measured")
    assert f"p95\n  is {c041['value_ms']:,.0f} ms against a {c041['bound_ms']:,.0f} ms ceiling" \
        in doc


def test_spend_and_search(ev, doc):
    s, b = ev.frozen["spent"], ev.frozen["budgets"]
    line = (f"{s['experiments']} of {b['max_experiments']} experiments, "
            f"{s['compute_cu_hours']:.1f} of {b['max_compute_cu_hours']:g} CU-h, "
            f"${s['model_usd']:.2f} of ${b['max_model_spend_usd']:g} of model spend")
    assert line in doc
    assert ev.frozen["stop"]["by"] == "proposer"
    left = b["max_experiments"] - s["experiments"]
    assert f"It stopped with {left} experiments and about " \
           f"{round(b['max_compute_cu_hours'] - s['compute_cu_hours'], -1):.0f} CU-h" in doc


def test_the_quoted_stop_reason_is_the_logged_one(ev, doc):
    reason = ev.frozen["stop"]["reason"]
    quoted = re.search(r"Claude's reason, as logged: \*(.+?)\*", doc, re.S).group(1)
    assert re.sub(r"\s+", " ", quoted) == reason


def test_held_out_runs(doc):
    rows = [json.loads(x) for x in
            (config.REPO_ROOT / "results/phase4/eval-record.jsonl").read_text().splitlines()]
    runs = [r for r in rows if r.get("kind") == "run"]
    scored = [r for r in runs if r["status"] == "scored"]
    assert f"{len(scored)} of {len(runs)} runs scored, {len(runs) - len(scored)} rejected" in doc


def test_profile_figures(ev, doc):
    h = ev.profile["headline"]
    assert (f"{h['log_rows_profiled']:,} query-log rows into {h['shapes']} query shapes, "
            f"over a {h['dataset_bytes'] / 1e9:.2f} GB dataset") in doc
    assert f"a {h['log_rows_total']:,}-row query log" in doc
    sp = ev.space
    total = sp["dataset"]["rows"]["accounts"]
    assert f"{round(total * sp['split']['held_out_fraction']):,} of {total:,} accounts" in doc


def test_calibration(ev, held, doc):
    acc = results.accuracy(results.today_prediction(ev)["verdicts"],
                           held[split.HELD_OUT]["naive"]["verdicts"])
    assert f"agreed on **{acc['agree']} of {acc['criteria']}** verdicts" in doc
    assert f"on only **{acc['inside_interval']} of {acc['latency_compared']}** latency" in doc


def test_cost_context(ev, doc):
    (best,) = [t for t in ev.frozen["tested"] if t["bundle"] == BEST]
    (today,) = [v for v in results.held_out_verdicts(ev, ev.cells)[split.HELD_OUT]["naive"]
                ["verdicts"] if v["kind"] == "cost_ceiling" and v["level"] == LEVEL
                and v["cache_state"] == "warm"]
    assert f"${best['usd_per_ok_query_at_objective'] * 1e6:.2f} per million successful " \
           f"queries at {LEVEL} sessions, against today's\n${today['value'] * 1e6:.2f}" in doc


def test_run_cost(ev, doc):
    held_cu = sum(ev.execution["cu_hours_by_task"].values())
    words = {6: "six", 7: "seven", 8: "eight"}
    assert f"held {held_cu:.1f} CU-h across {words[ev.execution['jobs']]} jobs" in doc
    usd = (held_cu + 2.5) * ev.space["cost"]["cu_hour_usd"] + ev.execution["model_usd"]
    assert f"about ${usd:.2f}" in doc or f"about ${round(usd * 2) / 2:.2f}" in doc


def test_the_loop_log_page_it_links_is_current():
    assert loop_report.DOC_PATH.read_text() == loop_report.render_committed()


# ------------------------------------------------------------ links

def test_every_relative_link_resolves(doc):
    for href in re.findall(r"\]\(([^)]+)\)", doc):
        if re.match(r"^[a-z]+:", href) or href.startswith("#"):
            continue
        target = (DOC.parent / href.split("#")[0]).resolve()
        assert target.exists(), href


def test_the_adr_range_quoted_is_the_one_on_disk(doc):
    last = max(p.name[:4] for p in (DOC.parent / "adr").glob("*.md"))
    assert f"0001 to {last}" in doc
