"""The loop's proposer: Claude reads the evidence and names the next
experiment (RC1-632, ADR 0022).

The model only proposes. Its output is a JSON object constrained by a schema
(structured outputs; forced tool use is not available on this model), and
the object then goes through pocbench.space.validate like any untrusted
input. Spend is computed from each response's usage at the prices below and
charged against the loop's model budget before the next call.
"""

import json

import anthropic

from pocbench import loop

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 16_000
# USD per million tokens, claude-opus-5-5, first-party API (claude-api skill
# model table, cached 2026-10-06). Cache writes at the 5-minute TTL cost
# 1.25x input; reads 0.20.
PRICE = {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}
# Input tokens assumed when reserving budget for the next call.
RESERVE_INPUT_TOKENS = 40_000

SYSTEM = """\
You run the experiment loop of a database proof of concept. A buyer agreed
success criteria up front; they are frozen. Your job is to find the cheapest
configuration that meets every criterion, or to show that none in the
permitted space does, within a fixed budget.

What you can change, and nothing else:
- the compute tier (one of the listed CU sizes, fixed size);
- which of the listed index candidates exist (by id; the starting point is
  primary keys only, the naive baseline's indexes are not available).
Memory settings, autoscaling and free-form DDL are out of scope; a proposal
that includes them is rejected and wastes the iteration.

How an experiment runs: the candidate and the naive baseline are measured
interleaved on the profiling slice at 8 and 32 sessions, warm and after a
restart with cache eviction, 2 repetitions each. Each experiment costs about
1.5-2 hours and several CU-hours. You see per-shape net latency (client
latency minus the network round trip), failure fractions (timeouts count as
failures), throughput and cost per successful query, and which predicted
criteria are not met and why.

How the outcome is chosen (by code, not by you): every tested bundle whose
predicted criteria are all met qualifies; the qualifying bundle with the
lowest cost per successful query at 32 sessions (warm) is recommended. If
none qualifies the result is a refusal, which is a valid, honest outcome.
Do not stop early to avoid a refusal, and do not keep testing to escape one.

Rules for good iterations:
- State a falsifiable hypothesis tied to the evidence (a shape, a plan, a
  failure fraction) and what result would refute it.
- Change as little as needed to learn something. Per-change effects can only
  be claimed when two tested bundles differ by exactly that change.
- A tail percentile counts only when it is supported (enough observations
  beyond it); slow configurations cannot reach support.
- Stop when a qualifying bundle exists and no cheaper bundle is plausibly
  qualifying, or when the evidence shows no permitted bundle can meet the
  criteria. Say why.

Respond with one JSON object matching the schema. For a stop, set action to
"stop", cu to null and index_ids to [].

Engagement briefing (static for the whole loop):
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "action": {"type": "string", "enum": ["experiment", "stop"]},
        "cu": {"type": ["number", "null"]},
        "index_ids": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "expected": {"type": "string"},
        "stop_reason": {"type": "string"},
    },
    "required": ["hypothesis", "action", "cu", "index_ids", "why", "expected", "stop_reason"],
    "additionalProperties": False,
}


def usd(usage) -> float:
    def g(name: str) -> int:
        return getattr(usage, name, 0) or 0
    return (g("input_tokens") * PRICE["input"] + g("output_tokens") * PRICE["output"]
            + g("cache_creation_input_tokens") * PRICE["cache_write"]
            + g("cache_read_input_tokens") * PRICE["cache_read"]) / 1e6


class ProposerError(RuntimeError):
    """The model returned no usable proposal (refusal, truncation, bad JSON)."""


class ClaudeProposer:
    def __init__(self, static_context: dict, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.system = [{"type": "text",
                        "text": SYSTEM + json.dumps(static_context, indent=1, sort_keys=True,
                                                    default=str),
                        "cache_control": {"type": "ephemeral"}}]

    def worst_case_usd(self) -> float:
        return (RESERVE_INPUT_TOKENS * PRICE["input"] + MAX_TOKENS * PRICE["output"]) / 1e6

    def propose(self, context: dict) -> loop.Proposal:
        dynamic = {k: v for k, v in context.items() if k != "static"}
        resp = self.client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=self.system,
            output_config={"effort": EFFORT,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content":
                       "Evidence so far, budgets and history (JSON). Propose the next "
                       "experiment or stop.\n\n" + json.dumps(dynamic, sort_keys=True,
                                                              default=str)}])
        cost = usd(resp.usage)
        usage = {k: getattr(resp.usage, k, None) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens")} | {"request_id": resp._request_id,
                                                 "stop_reason": resp.stop_reason}
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if resp.stop_reason != "end_turn" or text is None:
            # Charged even though unusable; the loop logs it as an invalid proposal.
            return loop.Proposal(hypothesis="", action="experiment", why="", model_usd=cost,
                                 usage=usage, extra={"unusable_response": resp.stop_reason})
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            return loop.Proposal(hypothesis="", action="experiment", why="", model_usd=cost,
                                 usage=usage, extra={"unusable_response": "invalid JSON"})
        return loop.Proposal(hypothesis=d["hypothesis"], action=d["action"], why=d["why"],
                             cu=d["cu"], index_ids=d["index_ids"], expected=d["expected"],
                             stop_reason=d["stop_reason"], model_usd=cost, usage=usage)


class ScriptedProposer:
    """Fixtures and tests: proposals from a list, no model, no spend."""

    def __init__(self, script: list[dict], usd_per_call: float = 0.0):
        self.script = list(script)
        self.usd_per_call = usd_per_call
        self.contexts: list[dict] = []

    def worst_case_usd(self) -> float:
        return self.usd_per_call

    def propose(self, context: dict) -> loop.Proposal:
        self.contexts.append(context)
        if not self.script:
            return loop.Proposal("script exhausted", "stop", "", stop_reason="script exhausted",
                                 model_usd=self.usd_per_call)
        d = dict(self.script.pop(0))
        known = {"hypothesis", "action", "why", "cu", "index_ids", "expected", "stop_reason"}
        extra = {k: v for k, v in d.items() if k not in known}
        return loop.Proposal(hypothesis=d.get("hypothesis", ""), action=d.get("action",
                             "experiment"), why=d.get("why", ""), cu=d.get("cu"),
                             index_ids=d.get("index_ids", []), expected=d.get("expected", ""),
                             stop_reason=d.get("stop_reason", ""),
                             model_usd=self.usd_per_call, extra=extra)
