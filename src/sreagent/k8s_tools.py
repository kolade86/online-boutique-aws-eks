"""Read-only Kubernetes tools, scoped to the application namespace.

Only get/list/read calls are made, and the namespace is fixed by config - the
model cannot choose it. In the cluster, a namespaced Role enforces the same
limits; running locally with your own kubeconfig, this code is the only limit.
"""

import re
from datetime import datetime, timezone

from tools import Tool, ToolError, bounded_int, optional_str, require_str

NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
MAX_ITEMS = 50


class KubeReader:
    """Holds the API clients, created on first use so that a missing or broken
    kubeconfig surfaces as a tool error rather than stopping the agent."""

    def __init__(self, namespace: str, context=None, core=None, apps=None, autoscaling=None):
        self.namespace = namespace
        self._context = context
        self._apis = (core, apps, autoscaling) if core is not None else None

    def _load(self):
        if self._apis is None:
            from kubernetes import client, config
            try:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config(context=self._context)
            except config.ConfigException as e:
                raise ToolError(f"Kubernetes config not available: {e}") from None
            self._apis = (client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV2Api())
        return self._apis

    @property
    def core(self):
        return self._load()[0]

    @property
    def apps(self):
        return self._load()[1]

    @property
    def autoscaling(self):
        return self._load()[2]


def _age(ts) -> str:
    if ts is None:
        return "?"
    seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _resources(container) -> str:
    r = container.resources
    if r is None:
        return "none"
    req = r.requests or {}
    lim = r.limits or {}
    return (f"req cpu={req.get('cpu', '-')} mem={req.get('memory', '-')}, "
            f"lim cpu={lim.get('cpu', '-')} mem={lim.get('memory', '-')}")


def format_pod(pod) -> str:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for s in statuses if s.ready)
    restarts = sum(s.restart_count for s in statuses)
    notes = []
    for s in statuses:
        if s.state and s.state.waiting and s.state.waiting.reason:
            notes.append(f"{s.name} waiting: {s.state.waiting.reason}")
        last = s.last_state.terminated if s.last_state else None
        if last is not None:
            notes.append(f"{s.name} last exit: {last.reason} (code {last.exit_code}, {_age(last.finished_at)} ago)")
    line = (f"{pod.metadata.name}: {pod.status.phase}, ready {ready}/{len(statuses)}, "
            f"restarts {restarts}, age {_age(pod.metadata.creation_timestamp)}, "
            f"node {pod.spec.node_name}")
    return line + ("".join(f"\n    {n}" for n in notes))


def format_deployment(d) -> str:
    s = d.status
    containers = "\n".join(
        f"    {c.name}: image tag {c.image.rsplit(':', 1)[-1]}; {_resources(c)}"
        for c in d.spec.template.spec.containers)
    return (f"{d.metadata.name}: desired {d.spec.replicas}, ready {s.ready_replicas or 0}, "
            f"updated {s.updated_replicas or 0}, available {s.available_replicas or 0}, "
            f"generation {d.metadata.generation}/observed {s.observed_generation}\n{containers}")


def format_hpa(h) -> str:
    current = []
    for m in h.status.current_metrics or []:
        if m.resource and m.resource.current:
            current.append(f"{m.resource.name} {m.resource.current.average_utilization}%")
    targets = []
    for m in h.spec.metrics or []:
        if m.resource and m.resource.target:
            targets.append(f"{m.resource.name} {m.resource.target.average_utilization}%")
    return (f"{h.metadata.name}: min {h.spec.min_replicas}, max {h.spec.max_replicas}, "
            f"current {h.status.current_replicas}, desired {h.status.desired_replicas}; "
            f"utilization {', '.join(current) or 'unknown'} (target {', '.join(targets) or '?'})")


def format_event(e) -> str:
    when = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
    obj = e.involved_object
    return (f"{_age(when)} ago {e.type} {e.reason} {obj.kind}/{obj.name} "
            f"(x{e.count or 1}): {e.message}")


def _event_time(e):
    return (e.last_timestamp or e.event_time or e.metadata.creation_timestamp
            or datetime.min.replace(tzinfo=timezone.utc))


def _cap(lines: list[str]) -> str:
    if not lines:
        return "None found."
    extra = f"\n... and {len(lines) - MAX_ITEMS} more" if len(lines) > MAX_ITEMS else ""
    return "\n".join(lines[:MAX_ITEMS]) + extra


def make_tools(kube: KubeReader) -> list[Tool]:
    ns = kube.namespace

    def list_pods(args):
        app = optional_str(args, "app", NAME)
        pods = kube.core.list_namespaced_pod(ns, label_selector=f"app={app}" if app else None)
        return _cap([format_pod(p) for p in pods.items])

    def list_deployments(args):
        return _cap([format_deployment(d) for d in kube.apps.list_namespaced_deployment(ns).items])

    def list_hpas(args):
        hpas = kube.autoscaling.list_namespaced_horizontal_pod_autoscaler(ns).items
        return _cap([format_hpa(h) for h in hpas])

    def list_events(args):
        name = optional_str(args, "object_name", NAME)
        selector = f"involvedObject.name={name}" if name else None
        events = kube.core.list_namespaced_event(ns, field_selector=selector).items
        if args.get("warnings_only"):
            events = [e for e in events if e.type == "Warning"]
        events.sort(key=_event_time, reverse=True)
        return _cap([format_event(e) for e in events])

    def get_pod_logs(args):
        pod = require_str(args, "pod", NAME)
        container = optional_str(args, "container", NAME)
        tail = bounded_int(args, "tail_lines", 100, 1, 500)
        previous = bool(args.get("previous", False))
        try:
            logs = kube.core.read_namespaced_pod_log(
                pod, ns, container=container, tail_lines=tail, previous=previous)
        except Exception as e:
            status = getattr(e, "status", None)
            if status in (400, 404):  # no such pod/container, or no previous instance
                raise ToolError(f"Cannot read logs ({status}): {getattr(e, 'body', e)}") from None
            raise
        return logs or "(no log output)"

    no_input = {"type": "object", "properties": {}}
    return [
        Tool("list_pods",
             f"List pods in the application namespace ({ns}) with phase, readiness, "
             "restart count, waiting reason and last termination reason (e.g. OOMKilled).",
             {"type": "object", "properties": {
                 "app": {"type": "string", "description": "Only pods with label app=<value>, e.g. cartservice"}}},
             list_pods),
        Tool("list_deployments",
             f"List deployments in {ns} with desired/ready replicas, image tag, and "
             "container CPU/memory requests and limits.",
             no_input, list_deployments),
        Tool("list_hpas",
             f"List HorizontalPodAutoscalers in {ns} with min/max/current replicas and "
             "current vs target CPU utilization.",
             no_input, list_hpas),
        Tool("list_events",
             f"List recent Kubernetes events in {ns}, newest first (scheduling failures, "
             "probe failures, OOM kills, image pull errors).",
             {"type": "object", "properties": {
                 "object_name": {"type": "string", "description": "Only events about this object (pod or deployment name)"},
                 "warnings_only": {"type": "boolean", "description": "Only Warning events"}}},
             list_events),
        Tool("get_pod_logs",
             f"Read the last lines of a pod's logs in {ns}. Set previous=true to read "
             "the logs of the container instance that crashed before the current one.",
             {"type": "object", "properties": {
                 "pod": {"type": "string", "description": "Exact pod name (from list_pods)"},
                 "container": {"type": "string", "description": "Container name, if the pod has several"},
                 "tail_lines": {"type": "integer", "description": "Lines from the end (1-500, default 100)"},
                 "previous": {"type": "boolean", "description": "Logs of the previous (crashed) container"}},
              "required": ["pod"]},
             get_pod_logs),
    ]
