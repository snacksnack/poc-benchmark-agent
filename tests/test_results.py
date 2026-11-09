"""Phase 5 results document tests (RC1-633): the honest-reporting rule proved
from fixtures, the best-tested label, platform labels and the platform-join
guard, evidence verification, and freshness of the committed documents."""

import copy
import json
import re
import shutil

import pytest

from pocbench import config, phase4, phase5_fixtures, results, verdicts
from pocbench.verdicts import PlatformJoinError

FIXTURE_DOCS = {n: config.REPO_ROOT / "docs" / "fixtures" / f"results-{n}.md"
                for n in phase5_fixtures.NAMES}


@pytest.fixture(scope="module")
def evs() -> dict[str, results.Evidence]:
    return {n: phase5_fixtures.evidence(n) for n in phase5_fixtures.NAMES}


@pytest.fixture(scope="module")
def real() -> results.Evidence:
    return results.load_real()


def _summary(md: str) -> list[str]:
    """The bullets of the first summary section."""
    lines = md.splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith("## Summary"))
    out = []
    for x in lines[start + 1:]:
        if x.startswith("## "):
            break
        if x.startswith("- "):
            out.append(x)
    return out


def _lead(ev: results.Evidence) -> list[dict]:
    held = results.held_out_verdicts(ev, ev.cells)
    return results.lead(ev, held, ev.cells)


# -------------------------------------------- honest reporting, from fixtures

@pytest.mark.parametrize("name", phase5_fixtures.NAMES)
def test_fixture_produces_its_planted_lead(evs, name):
    ev, exp = evs[name], phase5_fixtures.load(name)["expected"]
    assert ev.frozen["kind"] == exp["kind"]
    kinds = [i["kind"] for i in _lead(ev)]
    assert kinds == exp["lead"]


def test_losing_fixture_names_the_loss_and_leads_with_it(evs):
    ev = evs["losing"]
    items = _lead(ev)
    assert {i["shape"] for i in items if i["kind"] == "loss"} == {"s04"}
    bullets = _summary(results.render(ev))
    assert bullets[0].startswith("- Loss against today, s04")
    assert "spread" in bullets[0]
    # The headline comes after every lead item.
    head = next(i for i, b in enumerate(bullets) if "Headline" in b)
    assert head == len(items)


def test_under_sampled_fixture_leads_with_the_key_tail(evs):
    ev = evs["under-sampled"]
    items = _lead(ev)
    assert {i["shape"] for i in items if i["kind"] == "under_sampled"} == {"s02"}
    bullets = _summary(results.render(ev))
    assert bullets[0].startswith("- Under-sampled key tail, s02")
    assert "8 distinct beyond" in bullets[0]
    held = results.held_out_verdicts(ev, ev.cells)
    s02 = [v for v in held["held_out"][ev.frozen["config"]["name"]]["verdicts"]
           if v["kind"] == "latency_ceiling" and v["shape"] == "s02"]
    assert s02 and all(v["verdict"] == verdicts.UNSUPPORTED for v in s02)


def test_refusal_fixture_leads_with_the_refusal(evs):
    ev, exp = evs["refusal"], phase5_fixtures.load("refusal")["expected"]
    assert ev.frozen["class"] == exp["class"]
    assert set(exp["shapes_include"]) <= set(ev.frozen["shapes"])
    md = results.render(ev)
    bullets = _summary(md)
    assert bullets[0].startswith("- No recommendation. Refusal")
    assert exp["class"] in bullets[0]
    # Tested candidates with evidence, and both held-out baselines scored.
    assert "## Refusal" in md
    for t in ev.frozen["tested"]:
        assert f"`{t['bundle']}`" in md
    assert "held-out baseline `default`" in md and "held-out baseline `naive`" in md


def test_a_clean_win_leads_with_the_headline():
    """Control: the qualifying workload with nothing planted meets every
    held-out criterion, so nothing outranks the headline."""
    ev = phase5_fixtures.build({"phase4_fixture": "qualifying", "held_out": [],
                                "about": "control"}, "control")
    assert _lead(ev) == []
    bullets = _summary(results.render(ev))
    assert bullets[0].startswith("- Headline")
    assert "at 9% of today's cost" in bullets[0] or "of today's cost" in bullets[0]


@pytest.mark.parametrize("which", ["refusal", "real"])
def test_best_tested_candidate_is_labeled_everywhere_it_appears(evs, real, which):
    ev = real if which == "real" else evs[which]
    best = ev.frozen["best_tested"]["bundle"]
    md = results.render(ev)
    hits = [x for x in md.splitlines() if best in x]
    assert hits
    for x in hits:
        assert results.BEST_LABEL in x, x


def test_regressions_carry_spread_on_both_sides(evs):
    ev = evs["losing"]
    held = results.held_out_verdicts(ev, ev.cells)
    regs = results.regressions(ev, held, ev.cells, ev.frozen["config"]["name"])
    assert regs and regs[0]["verdict"] == verdicts.FAILED
    assert regs[0]["cand_stat"]["spread"] is not None
    assert regs[0]["base_stat"]["spread"] is not None


def test_prediction_accuracy_is_bundle_level_and_per_change_says_which(evs):
    ev = evs["losing"]
    md = results.render(ev)
    assert "Accuracy is stated for whole bundles only" in md
    # The qualifying loop ran 1 CU and 0.5 CU with the same indexes, so only
    # the tier change is isolated; each index is listed as not decomposed.
    pc = {p["change"]: p for p in ev.frozen["per_change"]}
    assert pc["tier 1 CU"]["isolated"] is True
    assert all(not p["isolated"] for k, p in pc.items() if k.startswith("add "))
    assert "| add I1 | no |" in md


# ------------------------------------------------------- platforms (ADR 0007)

def _tables(md: str) -> list[tuple[str, int]]:
    """(caption, line number) of every markdown table's header row."""
    lines = md.splitlines()
    return [(lines[i - 2], i) for i, x in enumerate(lines)
            if x.startswith("| ") and not lines[i - 1].startswith("|")]


@pytest.mark.parametrize("which", [*phase5_fixtures.NAMES, "real"])
def test_every_figure_is_platform_labeled(evs, real, which):
    ev = real if which == "real" else evs[which]
    md = results.render(ev)
    labels = tuple(results.PLATFORM_LABELS.values())
    for caption, _ in _tables(md):
        assert caption.startswith("*Platform: "), caption
    for h in (x for x in md.splitlines() if x.startswith("## ")):
        assert h == "## Sources and pins" or any(lab in h for lab in labels), h
    assert "Headline (" in md or "Today on held-out accounts (" in md


def _fallback(ev: results.Evidence) -> results.Evidence:
    """The ADR 0007 fallback shape: latency on local Postgres (warm only, no
    restart, no cost), cost and everything else on Neon."""
    neon = [c | {"platform": "neon"} for c in ev.cells]
    local = [copy.deepcopy(c) | {"platform": "local-postgres",
                                 "cost_per_successful_query_usd": None}
             for c in ev.cells if c["cache_state"] == "warm"]
    return results.Evidence(**{**ev.__dict__, "cells": neon + local})


def test_joining_platforms_is_refused(evs):
    ev = _fallback(evs["losing"])
    name = ev.frozen["config"]["name"]
    with pytest.raises(PlatformJoinError):
        verdicts.evaluate(ev.doc, ev.cells, name, error_ceiling_fraction=0.01, basis="x")
    with pytest.raises(PlatformJoinError):
        results.held_out_verdicts(ev, ev.cells)
    with pytest.raises(PlatformJoinError):
        results.headline_rows(ev, ev.cells)
    with pytest.raises(PlatformJoinError):
        results.cost_ratio(ev, ev.cells)


def test_fallback_document_reports_each_platform_separately(evs):
    ev = _fallback(evs["losing"])
    md = results.render(ev)  # raises if any figure mixed the platforms
    summaries = [x for x in md.splitlines() if x.startswith("## Summary: ")]
    assert summaries == ["## Summary: local Postgres (fallback latency platform)",
                         "## Summary: Neon Postgres"]
    heads = [x for x in md.splitlines() if "Headline (" in x]
    assert len(heads) == 2
    assert "local Postgres" in heads[0] and "Neon Postgres" not in heads[0]
    assert "Neon Postgres" in heads[1] and "local Postgres" not in heads[1]
    assert "reported separately and never combined" in md
    # Local measured no cost: its section says so instead of a figure.
    local = md[md.index("## local Postgres"):md.index("## Neon Postgres:")]
    assert "no cost measured on this platform" in local


# ------------------------------------------------------- evidence verification

def test_outcomes_must_agree_with_the_pooled_cell():
    cell = {"slice": "held_out", "config": "naive", "level": 8, "cache_state": "warm",
            "by_shape": {"s01": {"observations": 10, "failure_fraction": 0.3}}}
    row = {"kind": "run", "status": "scored", "slice": "held_out",
           "config": {"name": "naive"}, "level": 8, "cache_state": "warm",
           "run": {"by_shape": {"s01": {"errors": 1, "timeouts": 2, "observations": 10}}}}
    (out,) = results.attach_outcomes([cell], [row])
    assert (out["by_shape"]["s01"]["timeouts"], out["by_shape"]["s01"]["errors"]) == (2, 1)
    row["run"]["by_shape"]["s01"]["timeouts"] = 5
    with pytest.raises(ValueError, match="disagree"):
        results.attach_outcomes([cell], [row])


def test_real_run_refuses_verdicts_that_do_not_re_derive(tmp_path):
    state = tmp_path / "phase4"
    shutil.copytree(phase4.STATE, state)
    path = state / "eval-verdicts.json"
    v = json.loads(path.read_text())
    first = v["verdicts"]["held_out"]["naive"]["verdicts"][0]
    first["verdict"] = "met" if first["verdict"] != "met" else "failed"
    path.write_text(json.dumps(v))
    with pytest.raises(ValueError, match="do not re-derive"):
        results.load_real(state)


def test_real_run_is_the_frozen_refusal(real):
    assert real.frozen["kind"] == "refusal"
    assert real.frozen["class"] == "CRITERIA UNMET"
    assert {c["platform"] for c in real.cells} == {"neon"}
    assert len(real.cells) == 24
    s07 = [c["by_shape"]["s07"] for c in real.cells]
    assert all("timeouts" in s and "errors" in s for s in s07)


# ------------------------------------------------------------------ freshness

def test_committed_real_document_is_current(real):
    assert results.DOC_PATH.read_text() == results.render(real), \
        "regenerate: uv run pocbench results-doc --write docs/results.md"


@pytest.mark.parametrize("name", phase5_fixtures.NAMES)
def test_committed_fixture_documents_are_current(evs, name):
    assert FIXTURE_DOCS[name].read_text() == results.render(evs[name]), \
        f"regenerate: uv run pocbench results-fixture {name} --write {FIXTURE_DOCS[name]}"


def test_fixture_documents_say_they_are_fixtures(evs):
    for name, ev in evs.items():
        md = results.render(ev)
        assert "**Fixture document.**" in md
        assert not re.search(r"Neon Postgres", md), name
