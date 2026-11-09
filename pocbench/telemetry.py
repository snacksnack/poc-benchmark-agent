"""Datadog LLM Observability for the loop, and gauges for the eval suite
(RC1-634, ADR 0026).

Decoration, never load-bearing. `enable_llmobs()` is a no-op returning False
unless ddtrace is installed and DD_API_KEY is in the environment; a failure
to start tracing is a decline, never an error. A loop job holding two Neon
branches must not die for its telemetry.

Fleet conventions this module follows (agent-evals RC1-322, RC1-331, RC1-407,
esp-migration-agent RC1-548):
- One ml_app per agent, `poc-benchmark-agent`, so the LLM Obs app list is the
  fleet inventory.
- A root span's tags reach the trace metrics only when the payload carries a
  `version:` tag, and ddtrace reads DD_VERSION at import time, so
  `apply_version()` sets ddtrace's config directly.
- Non-anthropic LLM integrations are env-defaulted off before enable(),
  because enable() patches with raise_errors=True.

Each loop iteration is one root workflow span, `loop.iteration`, around the
proposal, validation and budget checks. The anthropic llm span nests inside
it. The span carries the iteration's model spend and the experiment's CU-hour
estimate as metrics; the experiment itself (1.5 to 2 hours on Neon) runs
outside the span, and its measured CU-hours are in the loop log. All
iterations of one loop share a session id (the log's header hash), so the
pane groups a loop.

Gauges go to the v2 series API. Without DD_API_KEY, `submit()` declines; a
rejected payload prints in full, because a swallowed 400 looks exactly like
no traffic (RC1-440).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx

try:  # documented optional-dep exception: ddtrace is a dev-group dependency
    from ddtrace import config as _dd_config
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    _dd_config = None
    LLMObs = None

ML_APP = "poc-benchmark-agent"
SERVICE = "poc-benchmark-agent"
METRIC_PREFIX = "pocbench."
GAUGE = 3


def package_version() -> str:
    try:
        return version("poc-benchmark-agent")
    except PackageNotFoundError:  # pragma: no cover - installed in every run
        return "0.0.0"


def apply_version() -> str:
    """Set DD_VERSION and ddtrace's own config to the package version.

    Setting only the env var after ddtrace is imported sends version:"", the
    RC1-407 drop this guards (measured in RC1-548)."""
    v = os.environ.setdefault("DD_VERSION", package_version())
    if _dd_config is not None and not _dd_config.version:
        _dd_config.version = v
    return v


def tracing() -> bool:
    """Whether LLM Obs is on in this process, whoever enabled it (this module
    for loop jobs, agent_evals.llmobs for the eval suite)."""
    return LLMObs is not None and bool(LLMObs.enabled)


def enable_llmobs(*, service: str | None = None) -> bool:
    """Turn on LLM Observability, or quietly decline. Idempotent."""
    if tracing():
        return True
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    apply_version()
    _restrict_patching_to_anthropic()
    try:
        LLMObs.enable(ml_app=ML_APP, agentless_enabled=True,
                      site=os.environ.get("DD_SITE", "datadoghq.com"),
                      service=service or SERVICE)
    except Exception as exc:  # a run must never die for its decoration
        print(f"telemetry: llmobs disabled, enable() failed: {exc}", file=sys.stderr)
        return False
    return True


def flush() -> None:
    if tracing():
        try:
            LLMObs.flush()
        except Exception as exc:  # pragma: no cover - decoration
            print(f"telemetry: llmobs flush failed: {exc}", file=sys.stderr)


def _llm_integration_modules() -> tuple[str, ...]:
    try:
        from ddtrace.llmobs._constants import SUPPORTED_LLMOBS_INTEGRATIONS
        from ddtrace.llmobs._llmobs import _INTEGRATIONS_W_PROPAGATION_SUPPORT
    except ImportError:  # pragma: no cover - a moved private layout
        return ()
    return tuple(set(SUPPORTED_LLMOBS_INTEGRATIONS.values())
                 | set(_INTEGRATIONS_W_PROPAGATION_SUPPORT.values()))


def _restrict_patching_to_anthropic() -> None:
    """setdefault, so an explicit DD_TRACE_<X>_ENABLED still wins."""
    for module in _llm_integration_modules():
        if module != "anthropic":
            os.environ.setdefault(f"DD_TRACE_{module.upper().replace('-', '_')}_ENABLED",
                                  "false")


# ------------------------------------------------------------ loop spans

class _NullIteration:
    def record(self, entry: dict) -> None:
        return None


class _Iteration:
    def __init__(self, span):
        self._span = span

    def record(self, entry: dict) -> None:
        """Attach the logged proposal row: what was proposed, what the
        validator said, and what it cost."""
        validator = entry.get("validator")
        if isinstance(validator, dict):
            outcome = "valid" if validator.get("valid") else "rejected"
        else:
            outcome = str(validator or "pending")
        usage = entry.get("usage") or {}
        metrics = {"model_usd": float(entry.get("model_usd") or 0.0)}
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens"):
            if usage.get(k) is not None:
                metrics[k] = float(usage[k])
        if isinstance(validator, dict) and validator.get("estimated_cu_hours") is not None:
            metrics["estimated_cu_hours"] = float(validator["estimated_cu_hours"])
        try:
            LLMObs.annotate(
                self._span,
                input_data=_clip({"hypothesis": entry.get("hypothesis"),
                                  "proposal": entry.get("proposal")}),
                output_data=_clip({"action": entry.get("action"), "validator": validator,
                                   "stop_reason": entry.get("stop_reason")}),
                metadata={"iteration": entry.get("iteration"),
                          "request_id": usage.get("request_id")},
                metrics=metrics,
                tags={"action": str(entry.get("action")), "validator": outcome})
        except Exception as exc:  # pragma: no cover - decoration
            print(f"telemetry: annotate failed: {exc}", file=sys.stderr)


@contextmanager
def iteration(session_id: str | None = None) -> Iterator[_Iteration | _NullIteration]:
    """One loop iteration as a root workflow span; a no-op handle when
    tracing is off."""
    if not tracing():
        yield _NullIteration()
        return
    with LLMObs.workflow(name="loop.iteration", session_id=session_id) as span:
        yield _Iteration(span)


def _clip(value: object, cap: int = 2000) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= cap else text[: cap - 1] + "…"


# ----------------------------------------------------------------- gauges

def base_tags() -> list[str]:
    return [f"service:{SERVICE}", f"env:{os.environ.get('DD_ENV', 'prod')}",
            f"version:{os.environ.get('DD_VERSION') or package_version()}"]


def series_payload(points: list[tuple[str, float, dict[str, str]]],
                   at: int | None = None) -> dict:
    """One payload for POST /api/v2/series. Each point is (name, value,
    extra tags). Pure, so the shape is testable."""
    stamp = at or int(time.time())
    return {"series": [
        {"metric": METRIC_PREFIX + name, "type": GAUGE,
         "points": [{"timestamp": stamp, "value": float(value)}],
         "tags": base_tags() + [f"{k}:{v}" for k, v in sorted(tags.items())]}
        for name, value, tags in sorted(points, key=lambda p: (p[0], sorted(p[2].items())))]}


def submit(points: list[tuple[str, float, dict[str, str]]]) -> bool:
    """Post gauges; False without DD_API_KEY or on a failed POST, never raises."""
    key = os.environ.get("DD_API_KEY")
    if not key or not points:
        return False
    site = os.environ.get("DD_SITE", "datadoghq.com")
    body = series_payload(points)
    try:
        r = httpx.post(f"https://api.{site}/api/v2/series", json=body, timeout=20,
                       headers={"DD-API-KEY": key, "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        print(f"telemetry: series POST failed: {exc}", file=sys.stderr)
        return False
    if r.status_code >= 300:
        print(f"telemetry: series POST {r.status_code}: {r.text}\npayload: "
              f"{json.dumps(body)}", file=sys.stderr)
        return False
    return True
