import pytest

from pocbench import stats


def test_nearest_rank_never_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert stats.percentile(values, 50) == 2.0
    assert stats.percentile(values, 99) == 4.0
    assert stats.percentile(values, 100) == 4.0


def test_p99_of_hundred_samples_is_the_99th():
    values = [float(i) for i in range(1, 101)]
    assert stats.percentile(values, 99) == 99.0
    assert stats.percentile(values, 95) == 95.0


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        stats.percentile([], 50)
    with pytest.raises(ValueError):
        stats.percentile([1.0], 0)


def test_errors_and_timeouts_are_counted_not_dropped():
    s = stats.summarize([5.0] * 90, error_count=4, timeout_count=6, min_samples_for_p99=10)
    assert s.sample_count == 100
    assert s.ok_count == 90
    assert s.failure_fraction == pytest.approx(0.10)
    assert s.p99_ms == 5.0


def test_p99_flagged_unstable_below_the_sample_floor():
    assert not stats.summarize([1.0] * 99, 0, 0, min_samples_for_p99=100).p99_stable
    assert stats.summarize([1.0] * 100, 0, 0, min_samples_for_p99=100).p99_stable


def test_all_failed_run_has_no_latency():
    s = stats.summarize([], error_count=3, timeout_count=0, min_samples_for_p99=1)
    assert s.p99_ms is None and s.failure_fraction == 1.0


def test_spread_is_range_over_median():
    assert stats.spread([90.0, 100.0, 110.0]) == pytest.approx(0.20)
    with pytest.raises(ValueError):
        stats.spread([1.0])


def _run(p95, p99, errors=0, stable=True):
    return stats.Summary(sample_count=20_000, ok_count=20_000 - errors, error_count=errors,
                         timeout_count=0, p50_ms=1.0, p95_ms=p95, p99_ms=p99, max_ms=p99 * 2,
                         p99_stable=stable)


def test_gate_goes_when_both_spreads_sit_below_the_smallest_pass_effect():
    runs = [_run(10, 20), _run(10.5, 21), _run(10.2, 20.5)]
    v = stats.noise_gate(runs, threshold=0.20, error_ceiling=0.01)
    assert v.go and not v.reasons


def test_gate_falls_back_when_p99_spread_swallows_the_effect():
    runs = [_run(10, 20), _run(10, 26), _run(10, 21)]
    v = stats.noise_gate(runs, threshold=0.20, error_ceiling=0.01)
    assert not v.go
    assert any("p99 spread" in r for r in v.reasons)


def test_gate_falls_back_on_unstable_tail_or_error_ceiling():
    assert not stats.noise_gate([_run(10, 20), _run(10, 20, stable=False)], 0.2, 0.01).go
    assert not stats.noise_gate([_run(10, 20), _run(10, 20, errors=500)], 0.2, 0.01).go


def test_gate_spread_exactly_at_threshold_is_not_a_go():
    # (max - min) / median == threshold: noise alone could produce a "pass".
    v = stats.noise_gate([_run(10, 18), _run(10, 20), _run(10, 22)], 0.20, 0.01)
    assert not v.go
