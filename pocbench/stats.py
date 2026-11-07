"""Latency summaries and the Phase 0 noise-gate rule.

Percentiles use the nearest-rank method: p is the smallest observed value with
at least p percent of samples at or below it. It never interpolates, so every
reported figure is a latency that actually happened.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


def percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    if not 0 < p <= 100:
        raise ValueError(f"percentile must be in (0, 100]: {p}")
    rank = math.ceil(p / 100 * len(sorted_values))
    return sorted_values[rank - 1]


@dataclass(frozen=True)
class Summary:
    """One measured run. Latencies are of successful queries only; errors and
    timeouts are counted beside them, never dropped."""

    sample_count: int
    ok_count: int
    error_count: int
    timeout_count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    p99_stable: bool

    @property
    def failure_fraction(self) -> float:
        if not self.sample_count:
            return 0.0
        return (self.error_count + self.timeout_count) / self.sample_count


def summarize(
    ok_latencies_ms: Sequence[float],
    error_count: int,
    timeout_count: int,
    min_samples_for_p99: int,
) -> Summary:
    values = sorted(ok_latencies_ms)
    total = len(values) + error_count + timeout_count
    if values:
        p50, p95, p99, top = (percentile(values, p) for p in (50, 95, 99, 100))
    else:
        p50 = p95 = p99 = top = None
    return Summary(
        sample_count=total,
        ok_count=len(values),
        error_count=error_count,
        timeout_count=timeout_count,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        max_ms=top,
        p99_stable=len(values) >= min_samples_for_p99,
    )


def spread(values: Sequence[float]) -> float:
    """Run-to-run spread: (max - min) / median."""
    if len(values) < 2:
        raise ValueError("spread needs at least two runs")
    median = statistics.median(values)
    if median <= 0:
        raise ValueError("spread is undefined for a non-positive median")
    return (max(values) - min(values)) / median


@dataclass(frozen=True)
class GateVerdict:
    go: bool
    p95_spread: float
    p99_spread: float
    threshold: float
    reasons: tuple[str, ...]


def noise_gate(runs: Sequence[Summary], threshold: float, error_ceiling: float) -> GateVerdict:
    """Go when both p95 and p99 run-to-run spread sit below the smallest
    effect the criteria could call a pass, every run produced a stable p99,
    and no run broke the error ceiling. Anything else is the fallback."""
    reasons = []
    if any(r.p99_ms is None for r in runs):
        raise ValueError("a run with no successful queries cannot enter the gate")
    s95 = spread([r.p95_ms for r in runs])
    s99 = spread([r.p99_ms for r in runs])
    if s95 >= threshold:
        reasons.append(f"p95 spread {s95:.1%} is not below {threshold:.0%}")
    if s99 >= threshold:
        reasons.append(f"p99 spread {s99:.1%} is not below {threshold:.0%}")
    unstable = [i for i, r in enumerate(runs, 1) if not r.p99_stable]
    if unstable:
        reasons.append(f"runs {unstable} had too few samples for a stable p99")
    noisy = [i for i, r in enumerate(runs, 1) if r.failure_fraction > error_ceiling]
    if noisy:
        reasons.append(f"runs {noisy} broke the error ceiling")
    return GateVerdict(not reasons, s95, s99, threshold, tuple(reasons))


def quantile_ci_ranks(n: int, p: float, z: float = 1.96) -> tuple[int, int]:
    """Distribution-free confidence interval for the p-th percentile of n
    sorted samples, as 1-based ranks (normal approximation to the binomial).
    The interval is [sorted[lo - 1], sorted[hi - 1]]."""
    if n < 1 or not 0 < p < 100:
        raise ValueError(f"need n >= 1 and 0 < p < 100: n={n}, p={p}")
    q = p / 100
    half = z * math.sqrt(n * q * (1 - q))
    lo = max(1, math.floor(n * q - half))
    hi = min(n, math.ceil(n * q + half) + 1)
    return lo, hi


def quantile_ci(sorted_values: Sequence[float], p: float, z: float = 1.96
                ) -> tuple[float, float]:
    lo, hi = quantile_ci_ranks(len(sorted_values), p, z)
    return sorted_values[lo - 1], sorted_values[hi - 1]
