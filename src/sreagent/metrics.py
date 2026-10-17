"""Prometheus metrics for the server: what the agent does, how long, at what cost.

Labels are deliberately few and bounded: kind, outcome, decision, status,
tool, model, stop_reason, support, type, and service only for alerts about
the app namespace that passed the alert-name check. Never an investigation
id, a question, alert text or a pod name.

Each app gets its own registry (create_app), so tests do not share counters.
"""

from contextlib import contextmanager

import agent

from prometheus_client import (CollectorRegistry, Counter, Gauge, Histogram,
                               disable_created_metrics, generate_latest)
from prometheus_client.exposition import CONTENT_TYPE_LATEST

# No *_created series beside every counter and histogram: nothing reads them
disable_created_metrics()

DURATION_BUCKETS = (5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 450)
TOOL_CALL_BUCKETS = (1, 2, 4, 6, 8, 10, 12, 15, 20)
COST_BUCKETS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5)
SCORE_BUCKETS = tuple(range(10, 101, 10))
TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")
OUTCOMES = (agent.ANSWERED, agent.ANSWERED_AT_LIMIT, agent.TOOL_LIMIT, agent.TIMEOUT,
            agent.MODEL_STOPPED, agent.MODEL_ERROR)
KINDS = ("alert", "ask")
# The model's stop reasons that can be known in advance. Refusal categories
# ("refusal:cyber", ...) form an open set; they appear on first use.
STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "refusal")
SUPPORTS = ("observed", "inferred", "inconclusive")
DECISIONS = ("investigated", "added_to_incident", "busy", "not_worth")
# pr_tool.finish() statuses
PR_STATUSES = ("opened", "dry_run", "below_confidence", "commented", "comment_dry_run",
               "recently_merged", "not_opened", "failed")
FOLLOWUP_RESULTS = ("posted", "failed")


class Metrics:
    content_type = CONTENT_TYPE_LATEST

    def __init__(self, model: str, registry: CollectorRegistry = None, tools=(),
                 models=()):
        """`tools`: the agent's tool names; `models`: every model that may answer
        (the configured one, and the priced ones a fallback can reach)."""
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.investigations = Counter(
            "sreagent_investigations_total", "Investigations finished",
            ["kind", "outcome", "model"], registry=r)
        self.duration = Histogram(
            "sreagent_investigation_duration_seconds", "Time an investigation took",
            ["kind"], buckets=DURATION_BUCKETS, registry=r)
        self.calls_per_investigation = Histogram(
            "sreagent_investigation_tool_calls", "Tool calls made in one investigation",
            ["kind"], buckets=TOOL_CALL_BUCKETS, registry=r)
        self.tool_calls = Counter(
            "sreagent_tool_calls_total", "Tool calls, by tool and result",
            ["tool", "result"], registry=r)
        self.model_requests = Counter(
            "sreagent_model_requests_total", "Model responses, by the model that answered",
            ["model", "stop_reason"], registry=r)
        self.tokens = Counter(
            "sreagent_model_tokens_total", "Model tokens, by type",
            ["model", "type"], registry=r)
        self.cost = Counter(
            "sreagent_cost_usd_total", "Estimated model cost in USD (see model_prices.json)",
            ["model"], registry=r)
        self.cost_per_investigation = Histogram(
            "sreagent_investigation_cost_usd", "Estimated cost of one investigation in USD",
            ["kind"], buckets=COST_BUCKETS, registry=r)
        self.unpriced = Counter(
            "sreagent_unpriced_responses_total",
            "Model responses with no price in the price file (cost under-counted)",
            ["model"], registry=r)
        self.score = Histogram(
            "sreagent_evidence_score", "Evidence score of the latest assessment",
            ["kind", "support"], buckets=SCORE_BUCKETS, registry=r)
        self.assessments = Counter(
            "sreagent_assessments_total", "Investigations by how the cause was credited",
            ["kind", "support"], registry=r)
        self.alert_decisions = Counter(
            "sreagent_alert_decisions_total", "/alert decisions, one per incident",
            ["decision", "service"], registry=r)
        self.pull_requests = Counter(
            "sreagent_pull_requests_total", "Results of the pull request step",
            ["status"], registry=r)
        self.followups = Counter(
            "sreagent_followup_comments_total", "Comments on an open agent PR",
            ["result"], registry=r)
        self.in_progress = Gauge(
            "sreagent_investigation_in_progress", "1 while an investigation runs", registry=r)
        Gauge("sreagent_info", "The agent's configured model", ["model"],
              registry=r).labels(model=model).set(1)
        self._start_at_zero(model, tools, models)

    def _start_at_zero(self, model: str, tools, models) -> None:
        """Create every series whose label values are known in advance, at 0.

        A series that first appears holding its first event is invisible to
        rate() and increase(): the first investigation, the first use of a
        tool, the first refusal would never be counted. Only label values that
        cannot be known stay lazy: a service name in an alert decision, a
        refusal category, a model with no price.
        """
        models = list(dict.fromkeys([model, *models]))
        for kind in KINDS:
            for outcome in OUTCOMES:
                self.investigations.labels(kind=kind, outcome=outcome, model=model)
            self.duration.labels(kind=kind)
            self.calls_per_investigation.labels(kind=kind)
            self.cost_per_investigation.labels(kind=kind)
            for support in SUPPORTS:
                self.score.labels(kind=kind, support=support)
            for support in (*SUPPORTS, "not_assessed"):
                self.assessments.labels(kind=kind, support=support)
        for tool in tools:
            for result in ("ok", "error"):
                self.tool_calls.labels(tool=tool, result=result)
        for m in models:
            self.cost.labels(model=m)
            for t in TOKEN_TYPES:
                self.tokens.labels(model=m, type=t)
            for reason in STOP_REASONS:
                self.model_requests.labels(model=m, stop_reason=reason)
        for decision in DECISIONS:
            self.alert_decisions.labels(decision=decision, service="-")
        for status in PR_STATUSES:
            self.pull_requests.labels(status=status)
        for result in FOLLOWUP_RESULTS:
            self.followups.labels(result=result)

    @contextmanager
    def running(self):
        self.in_progress.set(1)
        try:
            yield
        finally:
            self.in_progress.set(0)

    def record(self, kind: str, result, model: str, assessment, usage: dict, prices) -> None:
        """One finished investigation (agent.Investigation, the assessment tool, and
        the usage block from cost.summarize)."""
        self.investigations.labels(kind=kind, outcome=result.outcome, model=model).inc()
        self.duration.labels(kind=kind).observe(result.elapsed_seconds)
        self.calls_per_investigation.labels(kind=kind).observe(len(result.evidence))
        for e in result.evidence:
            self.tool_calls.labels(tool=e.tool, result="error" if e.is_error else "ok").inc()
        for entry in result.usage:
            self.model_requests.labels(model=entry["model"],
                                       stop_reason=entry.get("stop_reason", "")).inc()
            for t in TOKEN_TYPES:
                if entry.get(t):
                    self.tokens.labels(model=entry["model"], type=t).inc(entry[t])
            if prices.cost(entry["model"], entry) is None:
                self.unpriced.labels(model=entry["model"]).inc()
        for model_name, per_model in usage["by_model"].items():
            if per_model["cost_usd"]:
                self.cost.labels(model=model_name).inc(per_model["cost_usd"])
        if usage["cost_usd"] is not None:
            self.cost_per_investigation.labels(kind=kind).observe(usage["cost_usd"])
        latest = assessment.latest if assessment is not None else None
        if latest is None:
            self.assessments.labels(kind=kind, support="not_assessed").inc()
        else:
            support = latest.support or "inconclusive"
            self.assessments.labels(kind=kind, support=support).inc()
            self.score.labels(kind=kind, support=support).observe(latest.value)

    def decision(self, decision: str, service: str) -> None:
        self.alert_decisions.labels(decision=decision, service=service or "-").inc()

    def pull_request(self, status: str) -> None:
        self.pull_requests.labels(status=status).inc()

    def followup(self, result: str) -> None:
        self.followups.labels(result=result).inc()

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
