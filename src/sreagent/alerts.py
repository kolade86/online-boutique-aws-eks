"""Alert identity and deduplication.

An alert's AlertManager fingerprint includes every label, including `pod`, so
a crash-looping service produces a new fingerprint each time its pod is
replaced. The agent therefore identifies an alert by
(alertname, namespace, service), with the service worked out from whichever
labels the rule provides.
"""

import re
import threading
import time
from dataclasses import dataclass

# <deployment>-<replicaset hash>-<pod suffix>, <statefulset>-<ordinal>,
# <daemonset>-<suffix>
_POD_PATTERNS = [
    re.compile(r"(?P<owner>[a-z0-9-]+?)-[a-z0-9]{6,10}-[a-z0-9]{5}"),
    re.compile(r"(?P<owner>[a-z0-9-]+?)-\d+"),
    re.compile(r"(?P<owner>[a-z0-9-]+?)-[a-z0-9]{5}"),
]


@dataclass(frozen=True)
class AlertKey:
    alertname: str
    namespace: str
    service: str

    def __str__(self) -> str:
        return f"{self.alertname}/{self.namespace}/{self.service or '-'}"


def service_of(labels: dict) -> str:
    """Best guess at the service an alert is about, from its labels.

    Order matters. kube-state-metrics series (PodCrashLooping, ContainerOOMKilled,
    ...) carry service="monitoring-kube-state-metrics", the exporter's own
    Service, so `service` is only trusted when nothing more specific exists.
    `container` is never used: every service in this chart names it "server".
    """
    for name in ("deployment", "horizontalpodautoscaler", "app"):
        if labels.get(name):
            return labels[name]
    pod = labels.get("pod", "")
    if pod:
        for pattern in _POD_PATTERNS:
            match = pattern.fullmatch(pod)
            if match:
                return match.group("owner")
        return pod
    if labels.get("grpc_service"):
        # "hipstershop.CartService" -> "cartservice"
        return labels["grpc_service"].rsplit(".", 1)[-1].lower()
    return labels.get("service", "")


def key_of(alert: dict) -> AlertKey:
    labels = alert.get("labels", {})
    return AlertKey(labels.get("alertname", ""), labels.get("namespace", ""),
                    service_of(labels))


def firing(payload: dict) -> list[dict]:
    return [a for a in payload.get("alerts", []) if a.get("status", "firing") == "firing"]


class Deduplicator:
    """Remembers when each AlertKey was last investigated (in memory only)."""

    def __init__(self, window_seconds: float, clock=time.monotonic):
        self._window = window_seconds
        self._clock = clock
        self._seen: dict[AlertKey, float] = {}
        self._lock = threading.Lock()

    def is_recent(self, key: AlertKey) -> bool:
        with self._lock:
            seen = self._seen.get(key)
            return seen is not None and self._clock() - seen < self._window

    def mark(self, key: AlertKey) -> None:
        with self._lock:
            now = self._clock()
            self._seen[key] = now
            # Forget expired keys so the map cannot grow without bound
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._window}
