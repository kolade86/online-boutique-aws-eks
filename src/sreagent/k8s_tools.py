"""Read-only Kubernetes tools, scoped to the application namespace.

Only get/list/read calls are made, and the namespace is fixed by config - the
model cannot choose it. In the cluster, a namespaced Role enforces the same
limits; running locally with your own kubeconfig, this code is the only limit.
"""

import json
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
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:  # keep minutes: cycle lengths matter
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h{rest // 60}m" if rest >= 60 else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
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
    owners = pod.metadata.owner_references or []
    owner = f", owner {owners[0].kind}/{owners[0].name}" if owners else ""
    line = (f"{pod.metadata.name}: {pod.status.phase}, ready {ready}/{len(statuses)}, "
            f"restarts {restarts}, age {_age(pod.metadata.creation_timestamp)}, "
            f"node {pod.spec.node_name}{owner}")
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
    # Who reported it (kubelet, deployment-controller, horizontal-pod-autoscaler)
    # separates a probe failure from a controller scaling down.
    source = (e.source.component if e.source and e.source.component
              else e.reporting_component) or "?"
    # Kubernetes merges repeats into one event with a count. Without the first
    # time, "x19, 4m ago" reads as one recent event rather than a cycle.
    count = e.count or 1
    first = getattr(e, "first_timestamp", None)
    repeats = f"x{count} since {_age(first)} ago" if count > 1 and first else f"x{count}"
    return (f"{_age(when)} ago {e.type} {e.reason} {obj.kind}/{obj.name} "
            f"[{source}] ({repeats}): {e.message}")


def _event_time(e):
    return (e.last_timestamp or e.event_time or e.metadata.creation_timestamp
            or datetime.min.replace(tzinfo=timezone.utc))


REVISION = "deployment.kubernetes.io/revision"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _revision(obj) -> int:
    try:
        return int((obj.metadata.annotations or {}).get(REVISION, "0"))
    except ValueError:
        return 0


def describe_deployment(d, replica_sets) -> str:
    """What is actually deployed, as opposed to what the chart's values say.

    - live spec.replicas, and whether the applied manifest sets it at all
      (a template can omit a field that values.yaml still contains);
    - which field managers own spec.replicas (e.g. the HPA via the scale
      subresource, or Argo CD);
    - the Deployment's ReplicaSets: a rollout creates a new one with a new
      pod-template hash, a replica-count change resizes the current one.
    """
    annotations = d.metadata.annotations or {}
    s = d.status
    lines = [
        f"Deployment {d.metadata.name}: revision {annotations.get(REVISION, '?')}, "
        f"generation {d.metadata.generation} (observed {s.observed_generation})",
        f"Live spec.replicas: {d.spec.replicas}; status: replicas {s.replicas or 0}, "
        f"ready {s.ready_replicas or 0}, updated {s.updated_replicas or 0}, "
        f"available {s.available_replicas or 0}",
    ]

    applied = annotations.get(LAST_APPLIED)
    if applied:
        try:
            spec = json.loads(applied).get("spec") or {}
            in_manifest = (f"present, = {spec['replicas']}" if "replicas" in spec
                           else "absent (the manifest does not set it)")
        except ValueError:
            in_manifest = "unknown (annotation is not valid JSON)"
        lines.append(f"spec.replicas in the last applied manifest ({LAST_APPLIED}): {in_manifest}")
    else:
        lines.append(f"No {LAST_APPLIED} annotation: the last apply was not a client-side apply")

    owners = []
    for m in d.metadata.managed_fields or []:
        if "f:replicas" in ((m.fields_v1 or {}).get("f:spec") or {}):
            via = f", subresource {m.subresource}" if m.subresource else ""
            owners.append(f"{m.manager} ({m.operation}{via}, {_age(m.time)} ago)")
    lines.append("Field managers of spec.replicas: " + ("; ".join(owners) or "none recorded"))

    current = _revision(d)
    lines.append("ReplicaSets, newest revision first (a rollout adds a new one; "
                 "a replica-count change resizes the current one):")
    for rs in sorted(replica_sets, key=_revision, reverse=True):
        tags = ", ".join(c.image.rsplit(":", 1)[-1] for c in rs.spec.template.spec.containers)
        marker = " (current)" if _revision(rs) == current else ""
        lines.append(f"  {rs.metadata.name}: revision {_revision(rs)}{marker}, desired "
                     f"{rs.spec.replicas}, ready {rs.status.ready_replicas or 0}, image tag "
                     f"{tags}, created {_age(rs.metadata.creation_timestamp)} ago")
    if not replica_sets:
        lines.append("  none found")
    return "\n".join(lines)


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

    def describe_deployment_tool(args):
        name = require_str(args, "name", NAME)
        try:
            d = kube.apps.read_namespaced_deployment(name, ns)
        except Exception as e:
            if getattr(e, "status", None) == 404:
                raise ToolError(f"No Deployment named {name!r} in {ns}") from None
            raise
        owned = [rs for rs in kube.apps.list_namespaced_replica_set(
                     ns, label_selector=f"app={name}").items
                 if any(o.kind == "Deployment" and o.name == name
                        for o in rs.metadata.owner_references or [])]
        return describe_deployment(d, owned)

    def list_hpas(args):
        hpas = kube.autoscaling.list_namespaced_horizontal_pod_autoscaler(ns).items
        return _cap([format_hpa(h) for h in hpas])

    def list_events(args):
        name = optional_str(args, "object_name", NAME)
        app = optional_str(args, "app", NAME)
        selector = f"involvedObject.name={name}" if name else None
        events = kube.core.list_namespaced_event(ns, field_selector=selector).items
        if app:
            # The Deployment and HPA are named <app>; its ReplicaSets and pods
            # are <app>-<hash>[-<suffix>]. One call covers the whole chain.
            events = [e for e in events if e.involved_object.name == app
                      or e.involved_object.name.startswith(app + "-")]
        if args.get("warnings_only"):
            events = [e for e in events if e.type == "Warning"]
        events.sort(key=_event_time, reverse=True)
        # Controller events first: there are few of them and they explain why
        # pods come and go, so they must not be buried under (or truncated
        # behind) the per-pod Scheduled/Pulled/Created/Started lifecycle noise.
        controllers = [e for e in events if e.involved_object.kind != "Pod"]
        pods = [e for e in events if e.involved_object.kind == "Pod"]
        lines = []
        if controllers:
            lines += ["Controller events (HPA, Deployment, ReplicaSet, ...), newest first:"]
            lines += [format_event(e) for e in controllers]
        if pods:
            lines += ["Pod events, newest first:"] + [format_event(e) for e in pods]
        return _cap(lines)

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
        Tool("describe_deployment",
             f"Show what is actually deployed for one Deployment in {ns}: live "
             "spec.replicas, whether the applied manifest sets spec.replicas at all, "
             "which field managers (HPA, Argo CD, ...) own it, and its ReplicaSets with "
             "revision, size and image tag. Use it before concluding from the chart's "
             "values what the cluster runs - templates can omit a value.",
             {"type": "object", "properties": {
                 "name": {"type": "string", "description": "Deployment name, e.g. emailservice"}},
              "required": ["name"]},
             describe_deployment_tool),
        Tool("list_hpas",
             f"List HorizontalPodAutoscalers in {ns} with min/max/current replicas and "
             "current vs target CPU utilization.",
             no_input, list_hpas),
        Tool("list_events",
             f"List recent Kubernetes events in {ns}, newest first (scheduling failures, "
             "probe failures, OOM kills, image pull errors, scaling). Events are kept "
             "for about an hour, and survive the pod they describe. To explain why a "
             "pod was killed or replaced, use app=<service> to see the events of its "
             "Deployment, ReplicaSets, pods and HPA together.",
             {"type": "object", "properties": {
                 "app": {"type": "string", "description": "Events for a service's Deployment, ReplicaSets, pods and HPA, e.g. emailservice"},
                 "object_name": {"type": "string", "description": "Only events about this exact object name"},
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
             get_pod_logs, keep="tail"),
    ]
