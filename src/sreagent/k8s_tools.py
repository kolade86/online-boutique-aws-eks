"""Read-only Kubernetes tools.

Only get/list/read calls are made. The pod, Deployment, event and HPA tools
read the application namespace, fixed by config - the model cannot choose it.
get_resource reads a short allow-list of kinds, each from the one place the
agent may read it: the application namespace, Argo CD's Applications, or
nodes. Secrets are never on it. In the cluster, RBAC enforces the same limits
(templates/rbac.yaml); running locally with your own kubeconfig, this code is
the only limit.

Every output has node names, IPs, instance IDs and load balancer hostnames
replaced with placeholders before the model sees it (redact.redact_infra).
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from redact import redact_infra, truncate
from tools import Tool, ToolError, bounded_int, optional_str, require_str

NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
MAX_ITEMS = 50
# get_resource's own limit, below the registry's: one object, trimmed
MAX_RESOURCE_CHARS = 5000
MAX_CONFIG_VALUE_CHARS = 2000
# The placeholder mapping is kept for the process's life so numbering stays
# the same across investigations; pod IPs churn, so it is reset when it grows
MAX_PLACEHOLDERS = 1000


class KubeReader:
    """Holds the API clients, created on first use so that a missing or broken
    kubeconfig surfaces as a tool error rather than stopping the agent."""

    def __init__(self, namespace: str, context=None, core=None, apps=None, autoscaling=None,
                 argocd_namespace: str = "argocd", apis: dict = None):
        self.namespace = namespace
        self.argocd_namespace = argocd_namespace
        self._context = context
        self._apis = (core, apps, autoscaling) if core is not None else None
        self._extra = dict(apis or {})
        # One placeholder mapping for every tool, so <node-2> in list_pods is
        # <node-2> in get_resource nodes too
        self.names = {}

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

    def api(self, name: str):
        """discovery, networking, batch or custom: the clients get_resource needs."""
        if name not in self._extra:
            self._load()
            from kubernetes import client
            self._extra[name] = {"discovery": client.DiscoveryV1Api,
                                 "networking": client.NetworkingV1Api,
                                 "batch": client.BatchV1Api,
                                 "custom": client.CustomObjectsApi}[name]()
        return self._extra[name]

    def redact(self, text: str) -> str:
        if sum(len(v) for v in self.names.values()) > MAX_PLACEHOLDERS:
            self.names.clear()
        return redact_infra(text, self.names)

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


# --- get_resource ---------------------------------------------------------------------

# Where a kind may be read: the application namespace, Argo CD's namespace, or
# cluster-scoped. Each matches a grant in templates/rbac.yaml.
APP, ARGOCD, CLUSTER = "app", "argocd", "cluster"
PLACEHOLDER = re.compile(r"<node-\d+>")


@dataclass(frozen=True)
class Kind:
    singular: str
    scope: str
    list: Callable      # (kube, namespace) -> API response
    read: Callable      # (kube, name, namespace) -> API response
    line: Callable      # object dict -> one summary line


def _ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (AttributeError, ValueError):
        return None


def _name(o) -> str:
    return (o.get("metadata") or {}).get("name", "?")


def service_line(o) -> str:
    spec = o.get("spec") or {}
    selector = spec.get("selector")
    sel = (",".join(f"{k}={v}" for k, v in sorted(selector.items())) if selector
           else "none (its endpoints are not chosen by labels)")
    ports = ", ".join(f"{p.get('name') or '-'} {p.get('port')}->{p.get('targetPort')}/"
                      f"{p.get('protocol', 'TCP')}" for p in spec.get("ports") or [])
    return f"{_name(o)}: {spec.get('type', 'ClusterIP')}, selector {sel}, ports {ports or 'none'}"


def endpointslice_line(o) -> str:
    service = ((o.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/service-name", "?")
    endpoints = o.get("endpoints") or []
    # A missing ready condition means ready (the API's own rule)
    ready = [e for e in endpoints if (e.get("conditions") or {}).get("ready") is not False]
    pods = [(e.get("targetRef") or {}).get("name") for e in ready]
    pods = [p for p in pods if p]
    shown = f" [{', '.join(pods[:5])}{', ...' if len(pods) > 5 else ''}]" if pods else ""
    ports = ", ".join(f"{p.get('name') or '-'} {p.get('port')}/{p.get('protocol', 'TCP')}"
                      for p in o.get("ports") or [])
    return (f"{_name(o)}: service {service}, {len(endpoints)} endpoints (ready {len(ready)}, "
            f"not ready {len(endpoints) - len(ready)}){shown}, ports {ports or 'none'}")


def configmap_line(o) -> str:
    keys = [f"{k} ({len(v or '')} chars)" for k, v in sorted((o.get("data") or {}).items())]
    keys += [f"{k} (binary)" for k in sorted(o.get("binaryData") or {})]
    return f"{_name(o)}: keys {', '.join(keys) or 'none'}"


def ingress_line(o) -> str:
    spec = o.get("spec") or {}
    cls = (spec.get("ingressClassName")
           or ((o.get("metadata") or {}).get("annotations") or {}).get("kubernetes.io/ingress.class", "?"))
    routes = []
    for rule in spec.get("rules") or []:
        for path in ((rule.get("http") or {}).get("paths") or []):
            svc = ((path.get("backend") or {}).get("service") or {})
            port = (svc.get("port") or {})
            routes.append(f"{rule.get('host') or '*'}{path.get('path', '/')} -> "
                          f"{svc.get('name', '?')}:{port.get('number') or port.get('name', '?')}")
    lbs = [i.get("hostname") or i.get("ip") for i in
           (((o.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])]
    return (f"{_name(o)}: class {cls}, routes {'; '.join(routes) or 'none'}, "
            f"load balancer {', '.join(l for l in lbs if l) or 'not provisioned'}")


def job_line(o) -> str:
    spec, status = o.get("spec") or {}, o.get("status") or {}
    conditions = [f"{c['type']}" + (f" ({c.get('reason')})" if c.get("reason") else "")
                  for c in status.get("conditions") or [] if c.get("status") == "True"]
    return (f"{_name(o)}: completions {status.get('succeeded', 0)}/{spec.get('completions', 1)}, "
            f"failed {status.get('failed', 0)}, active {status.get('active', 0)}, "
            f"conditions {', '.join(conditions) or 'none'}, "
            f"age {_age(_ts((o.get('metadata') or {}).get('creationTimestamp')))}")


def node_line(o) -> str:
    meta, spec, status = o.get("metadata") or {}, o.get("spec") or {}, o.get("status") or {}
    labels = meta.get("labels") or {}
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    ready = conditions.get("Ready", {})
    problems = [t for t, c in conditions.items() if t != "Ready" and c.get("status") == "True"]
    taints = [f"{t.get('key')}{'=' + t['value'] if t.get('value') else ''}:{t.get('effect')}"
              for t in spec.get("taints") or []]
    alloc, cap = status.get("allocatable") or {}, status.get("capacity") or {}
    return (f"{_name(o)}: Ready {ready.get('status', '?')}"
            + (f" ({ready.get('reason')})" if ready.get("status") != "True" and ready.get("reason") else "")
            + (f", pressure/problems {', '.join(problems)}" if problems else "")
            + (", unschedulable (cordoned)" if spec.get("unschedulable") else "")
            + f", taints {', '.join(taints) or 'none'}"
            + f", allocatable cpu {alloc.get('cpu', '?')} mem {alloc.get('memory', '?')} pods {alloc.get('pods', '?')}"
            + f" (capacity cpu {cap.get('cpu', '?')} mem {cap.get('memory', '?')})"
            + f", {labels.get('node.kubernetes.io/instance-type', '?')}"
            + f" in {labels.get('topology.kubernetes.io/zone', '?')}"
            + (f", node group {labels['eks.amazonaws.com/nodegroup']}" if labels.get("eks.amazonaws.com/nodegroup") else "")
            + f", age {_age(_ts(meta.get('creationTimestamp')))}")


def _not_ok(resource) -> bool:
    health = (resource.get("health") or {}).get("status")
    return resource.get("status") not in (None, "Synced") or health not in (None, "Healthy")


def application_line(o) -> str:
    spec, status = o.get("spec") or {}, o.get("status") or {}
    source = spec.get("source") or (spec.get("sources") or [{}])[0]
    sync, health = status.get("sync") or {}, status.get("health") or {}
    op = status.get("operationState") or {}
    line = (f"{_name(o)}: source {source.get('path', '?')}@{source.get('targetRevision', '?')}, "
            f"sync {sync.get('status', '?')} at {(sync.get('revision') or '?')[:8]}, "
            f"health {health.get('status', '?')}")
    if op:
        line += (f"; last operation {op.get('phase', '?')} "
                 f"{_age(_ts(op.get('finishedAt') or op.get('startedAt')))} ago"
                 f" at {((op.get('syncResult') or {}).get('revision') or '?')[:8]}: "
                 f"{(op.get('message') or '').strip()[:300]}")
        # Why the sync failed is on the resources that failed, not in the message
        for r in [r for r in (op.get("syncResult") or {}).get("resources") or []
                  if r.get("status") not in (None, "Synced")][:3]:
            line += (f"\n    failed: {r.get('kind')}/{r.get('name')} {r.get('status')}: "
                     f"{(r.get('message') or '').strip()[:300]}")
    for c in status.get("conditions") or []:
        line += f"\n    condition {c.get('type')}: {(c.get('message') or '').strip()[:300]}"
    resources = status.get("resources") or []
    # Unhealthy (Missing, Degraded, ...) before merely out of sync: a rollout
    # waiting on a failed sync leaves every Deployment OutOfSync but Healthy
    bad = sorted((r for r in resources if _not_ok(r)),
                 key=lambda r: (r.get("health") or {}).get("status") in (None, "Healthy"))
    if resources:
        line += f"\n    resources: {len(resources)}, not synced or not healthy: {len(bad)}"
    for r in bad[:10]:
        line += (f"\n      {r.get('kind')}/{r.get('name')}: {r.get('status', '?')}, "
                 f"health {(r.get('health') or {}).get('status', '-')}"
                 + (f" ({(r.get('health') or {}).get('message')})" if (r.get('health') or {}).get('message') else ""))
    return line


def _trim(kind: str, o: dict) -> dict:
    """One object without what costs space and explains nothing."""
    o = json.loads(json.dumps(o))
    meta = o.get("metadata") or {}
    for key in ("managedFields", "uid", "resourceVersion", "selfLink"):
        meta.pop(key, None)
    (meta.get("annotations") or {}).pop(LAST_APPLIED, None)
    if "annotations" in meta and not meta["annotations"]:
        del meta["annotations"]
    for ref in meta.get("ownerReferences") or []:
        for key in ("uid", "blockOwnerDeletion", "controller"):
            ref.pop(key, None)
    for e in o.get("endpoints") or []:
        (e.get("targetRef") or {}).pop("uid", None)
        (e.get("targetRef") or {}).pop("resourceVersion", None)
    status = o.get("status") or {}
    if kind == "nodes":
        meta.pop("annotations", None)          # per-node IDs and volume plumbing
        for key in ("images", "volumesInUse", "volumesAttached", "daemonEndpoints",
                    "runtimeHandlers", "features", "config"):
            status.pop(key, None)
        for key in ("machineID", "systemUUID", "bootID"):
            (status.get("nodeInfo") or {}).pop(key, None)
        for c in status.get("conditions") or []:
            c.pop("lastHeartbeatTime", None)
    if kind == "configmaps":
        o["data"] = {k: truncate(v or "", MAX_CONFIG_VALUE_CHARS, keep="head")
                     for k, v in (o.get("data") or {}).items()}
        if o.get("binaryData"):
            o["binaryData"] = {k: f"<{len(v or '')} base64 characters>" for k, v in o["binaryData"].items()}
    if kind == "applications":
        resources = status.get("resources") or []
        if resources:
            status["resources"] = [r for r in resources if _not_ok(r)]
            status["resourcesTotal"] = len(resources)
        status["history"] = (status.get("history") or [])[-3:]
        result = (status.get("operationState") or {}).get("syncResult") or {}
        if result.get("resources"):
            result["resources"] = [r for r in result["resources"]
                                   if r.get("status") != "Synced" or r.get("hookPhase") == "Failed"][:10]
    return o


def _yaml(o: dict) -> str:
    import yaml  # a dependency of the kubernetes client
    return yaml.safe_dump(o, sort_keys=False, default_flow_style=False, width=120)


def _response(resp) -> dict:
    """The typed clients are called with _preload_content=False (plain JSON, the
    same shape as kubectl -o json); the custom objects client returns a dict."""
    if isinstance(resp, dict):
        return resp
    data = getattr(resp, "data", None)
    if isinstance(data, (bytes, str)):
        return json.loads(data)
    raise ToolError(f"Unexpected API response: {type(resp).__name__}")


_RAW = {"_preload_content": False}
ARGO = ("argoproj.io", "v1alpha1")
KINDS = {
    "services": Kind("service", APP,
                     lambda k, ns: k.core.list_namespaced_service(ns, **_RAW),
                     lambda k, n, ns: k.core.read_namespaced_service(n, ns, **_RAW), service_line),
    "endpointslices": Kind("endpointslice", APP,
                           lambda k, ns: k.api("discovery").list_namespaced_endpoint_slice(ns, **_RAW),
                           lambda k, n, ns: k.api("discovery").read_namespaced_endpoint_slice(n, ns, **_RAW),
                           endpointslice_line),
    "configmaps": Kind("configmap", APP,
                       lambda k, ns: k.core.list_namespaced_config_map(ns, **_RAW),
                       lambda k, n, ns: k.core.read_namespaced_config_map(n, ns, **_RAW), configmap_line),
    "ingresses": Kind("ingress", APP,
                      lambda k, ns: k.api("networking").list_namespaced_ingress(ns, **_RAW),
                      lambda k, n, ns: k.api("networking").read_namespaced_ingress(n, ns, **_RAW), ingress_line),
    "jobs": Kind("job", APP,
                 lambda k, ns: k.api("batch").list_namespaced_job(ns, **_RAW),
                 lambda k, n, ns: k.api("batch").read_namespaced_job(n, ns, **_RAW), job_line),
    "nodes": Kind("node", CLUSTER,
                  lambda k, ns: k.core.list_node(**_RAW),
                  lambda k, n, ns: k.core.read_node(n, **_RAW), node_line),
    "applications": Kind("application", ARGOCD,
                         lambda k, ns: k.api("custom").list_namespaced_custom_object(*ARGO, ns, "applications"),
                         lambda k, n, ns: k.api("custom").get_namespaced_custom_object(*ARGO, ns, "applications", n),
                         application_line),
}
_ALIASES = {**{k: k for k in KINDS}, **{v.singular: k for k, v in KINDS.items()},
            "svc": "services", "cm": "configmaps", "ing": "ingresses"}
FORBIDDEN = {"secret", "secrets"}


def resource_request(args: dict, app_namespace: str, argocd_namespace: str) -> tuple:
    """(kind, name, namespace) for a get_resource call, or ToolError. Shared by the
    live tool and replays, so a replay rejects exactly what the live tool rejects."""
    raw = require_str(args, "kind").strip().lower()
    if raw in FORBIDDEN:
        raise ToolError("Secrets are never readable by the agent.")
    kind = _ALIASES.get(raw)
    if kind is None:
        raise ToolError(f"Kind {raw!r} is not allowed. Allowed: {', '.join(KINDS)}.")
    spec = KINDS[kind]
    name = args.get("name") or None
    if name is not None and not (isinstance(name, str) and (
            NAME.fullmatch(name) or (kind == "nodes" and PLACEHOLDER.fullmatch(name)))):
        raise ToolError(f"'name' has an invalid value: {name!r}")
    given = optional_str(args, "namespace", NAME)
    if spec.scope == CLUSTER:
        return kind, name, None                     # nodes have no namespace
    allowed = app_namespace if spec.scope == APP else argocd_namespace
    if given is not None and given != allowed:
        raise ToolError(f"{kind} can be read only in namespace {allowed!r}, not {given!r}.")
    return kind, name, allowed


def canonical_resource_args(args: dict, app_namespace: str, argocd_namespace: str) -> dict:
    """The input a recording is keyed on: kind by its plural, and no namespace (each
    kind has exactly one place it may be read)."""
    kind, name, _ = resource_request(args, app_namespace, argocd_namespace)
    return {"kind": kind, **({"name": name} if name else {})}


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

    def get_resource(args):
        kind, name, where = resource_request(args, ns, kube.argocd_namespace)
        spec = KINDS[kind]
        at = f" in {where}" if where else ""
        real = name
        if name and PLACEHOLDER.fullmatch(name):    # the model only ever saw the placeholder
            real = next((v for v, p in kube.names.get("node", {}).items() if p == name), None)
            if real is None:
                raise ToolError(f"Unknown node placeholder {name}: list nodes first.")
        try:
            if name:
                text = (f"{spec.singular} {name}{at} (trimmed):\n"
                        + _yaml(_trim(kind, _response(spec.read(kube, real, where)))))
            else:
                items = sorted(_response(spec.list(kube, where)).get("items") or [], key=_name)
                text = f"{len(items)} {kind}{at}:\n" + _cap([spec.line(o) for o in items])
        except ToolError:
            raise
        except Exception as e:
            status = getattr(e, "status", None)
            if status == 404:
                raise ToolError(f"No {spec.singular} named {name!r}{at}" if name
                                else f"No {kind}{at}: the API is not installed") from None
            if status == 403:
                raise ToolError(f"Not permitted to read {kind}{at} (403): the agent's RBAC "
                                "does not grant it") from None
            raise
        return truncate(text, MAX_RESOURCE_CHARS, keep="head")

    def redacted(handler):
        def run(args):
            try:
                return kube.redact(handler(args))
            except ToolError as e:          # an API error body can name a node or an IP
                raise ToolError(kube.redact(str(e))) from None
        return run

    no_input = {"type": "object", "properties": {}}
    tools = [
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
        Tool("get_resource",
             "Read objects the other tools do not cover. Without a name it lists them, one "
             "summary line each; with a name it returns that object, trimmed. Kinds: "
             f"services, endpointslices, configmaps, ingresses, jobs (namespace {ns}); "
             f"applications (Argo CD, namespace {kube.argocd_namespace}: sync and health "
             "status, the last sync's result and error); nodes (cluster-wide: readiness, "
             "pressure, taints, allocatable CPU/memory). Nothing else, and never Secrets. "
             "Use it to compare a Service's selector with its EndpointSlices, to see why "
             "Argo CD did not deploy a commit, or whether nodes have room for pending "
             "pods. Node names and IPs are placeholders such as <node-1>; pass the "
             "placeholder as the name.",
             {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": list(KINDS),
                          "description": "What to read"},
                 "name": {"type": "string", "description": "One object's name; omit to list"},
                 "namespace": {"type": "string",
                               "description": f"Optional: each kind has one allowed namespace "
                                              f"({ns}, or {kube.argocd_namespace} for applications)"}},
              "required": ["kind"]},
             get_resource),
    ]
    return [Tool(t.name, t.description, t.input_schema, redacted(t.handler), t.keep, t.strict)
            for t in tools]
