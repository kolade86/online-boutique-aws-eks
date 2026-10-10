"""Configuration, read once from environment variables.

Secrets (ANTHROPIC_API_KEY, GITHUB_TOKEN, SREAGENT_API_TOKEN) are read from
the environment only; in the cluster they come from a Kubernetes Secret
created by hand.
"""

import os
from dataclasses import dataclass

IN_CLUSTER_PROMETHEUS = "http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090"
SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    model: str
    max_tokens: int
    model_timeout_seconds: float
    max_tool_calls: int
    investigation_timeout_seconds: float
    tool_output_max_chars: int
    prometheus_url: str
    app_namespace: str
    kube_context: str | None
    github_repo: str
    github_token: str | None
    github_branch: str
    values_file: str
    api_token: str | None
    open_prs: bool
    min_confidence_for_pr: int
    min_confidence_for_rollback: int
    dedup_minutes: int
    port: int

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        def integer(name, default):
            raw = env.get(name, str(default))
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive")
            return value

        def percent(name, default):
            value = integer(name, default)
            if value > 100:
                raise ConfigError(f"{name} must be between 1 and 100")
            return value

        repo = env.get("GITHUB_REPO", "")
        if repo.count("/") != 1:
            raise ConfigError("GITHUB_REPO must be set to owner/name")

        return cls(
            model=env.get("SREAGENT_MODEL", "claude-sonnet-5-5"),
            max_tokens=integer("SREAGENT_MAX_TOKENS", 16000),
            model_timeout_seconds=integer("SREAGENT_MODEL_TIMEOUT_SECONDS", 120),
            max_tool_calls=integer("SREAGENT_MAX_TOOL_CALLS", 15),
            investigation_timeout_seconds=integer("SREAGENT_TIMEOUT_SECONDS", 300),
            tool_output_max_chars=integer("SREAGENT_TOOL_OUTPUT_MAX_CHARS", 6000),
            prometheus_url=env.get("PROMETHEUS_URL", IN_CLUSTER_PROMETHEUS),
            app_namespace=env.get("APP_NAMESPACE") or _service_account_namespace(),
            kube_context=env.get("KUBE_CONTEXT") or None,
            github_repo=repo,
            github_token=env.get("GITHUB_TOKEN") or None,
            github_branch=env.get("GITHUB_BRANCH", "main"),
            values_file=env.get("VALUES_FILE", "helm/online-boutique/values-dev.yaml"),
            api_token=env.get("SREAGENT_API_TOKEN") or None,
            open_prs=_flag(env, "SREAGENT_OPEN_PRS", False),
            # Evidence score (confidence.py) a proposed change needs; an
            # image.tag rollback affects every service, so it needs more
            min_confidence_for_pr=percent("SREAGENT_MIN_CONFIDENCE_FOR_PR", 70),
            min_confidence_for_rollback=percent("SREAGENT_MIN_CONFIDENCE_FOR_ROLLBACK", 85),
            dedup_minutes=integer("SREAGENT_DEDUP_MINUTES", 30),
            port=integer("PORT", 8080),
        )


def _flag(env, name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ConfigError(f"{name} must be true or false, got {raw!r}")
    return raw in ("true", "1", "yes")


def _service_account_namespace() -> str:
    """In the cluster, default to the namespace the agent's pod runs in."""
    try:
        with open(SA_NAMESPACE_FILE) as f:
            return f.read().strip()
    except OSError:
        raise ConfigError("APP_NAMESPACE must be set when running outside the cluster") from None
