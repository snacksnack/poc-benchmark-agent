import copy

from pocbench import config, cost


def _inputs():
    return config.load_model().data, config.config_space().data


def test_worst_case_is_never_cheaper_than_expected():
    lm, space = _inputs()
    expected = sum(x.usd for x in cost.estimate(lm, space, "expected"))
    worst = sum(x.usd for x in cost.estimate(lm, space, "worst"))
    assert worst >= expected


def test_phase4_compute_never_exceeds_its_budget():
    lm, space = _inputs()
    for case in ("expected", "worst"):
        loop = next(x for x in cost.estimate(lm, space, case) if "adaptive loop" in x.item)
        assert loop.cu_hours <= space["budgets"]["phase4"]["max_compute_cu_hours"]


def test_longer_measure_window_reprices_the_matrix():
    lm, space = _inputs()
    longer = copy.deepcopy(lm)
    longer["timing"]["measure_seconds"] *= 2
    base = cost.run_minutes(lm, space, space["protocol"]["eval"])
    assert cost.run_minutes(longer, space, space["protocol"]["eval"]) > base


def test_committed_cost_doc_is_current():
    # docs/cost-estimate.md is generated; a config change must regenerate it.
    lm, space = config.load_model(), config.config_space()
    rendered = cost.render(lm.data, space.data, lm.sha256, space.sha256)
    committed = (config.REPO_ROOT / "docs" / "cost-estimate.md").read_text()
    assert committed == rendered, "run: uv run pocbench cost --write docs/cost-estimate.md"
