"""Apply a proposed change to values-dev.yaml and validate it against the allow-list.

The model never writes YAML. It proposes (path, value) pairs; this module
applies them with a comment-preserving round trip, then validates the
*result* - the parsed difference between the old and new file - so the
allow-list holds no matter how the change was produced.

Allowed leaves (in values-dev.yaml):
    image.tag                                          rollback to a recent tag only
    services.<svc>.resources.(requests|limits).(cpu|memory)
    services.<svc>.replicas                            only for services WITHOUT an HPA
    services.<svc>.hpa.(minReplicas|maxReplicas)       only for services WITH an HPA

"With an HPA" is decided from the merged chart values (values.yaml + the new
values-dev.yaml), the same rule the chart's templates use.
"""

import copy
import difflib
import io
import re
from dataclasses import dataclass

from ruamel.yaml import YAML

TAG = re.compile(r"v\d{8}-\d{6}-[0-9a-f]{8}")
CPU = re.compile(r"(\d+)m|(\d+(?:\.\d+)?)")
MEMORY = re.compile(r"(\d+)(Mi|Gi)")
TAG_LINE = re.compile(r"^  tag: ", re.MULTILINE)   # build.yml update-manifest's sed target
SERVICE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")

PROTECTED_SERVICES = {"sreagent"}   # the agent may never change itself
MAX_CHANGES = 6
CPU_BOUNDS = (0.01, 2.0)            # cores; t3.large nodes have 2 vCPU
MEMORY_BOUNDS_MI = (16, 4096)       # t3.large nodes have 8 GiB
MAX_REPLICAS = 5                    # replicas for services without an HPA
HPA_BOUNDS = (1, 10)

_MISSING = object()


class Rejected(Exception):
    """The change is not allowed. `reasons` lists every rule it broke."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Change:
    path: tuple[str, ...]
    value: object

    def __str__(self):
        return f"{'.'.join(self.path)} = {self.value}"


def parse_changes(raw) -> list[Change]:
    """Turn the model's input ([{path, value}, ...]) into Changes; shape checks only."""
    if not isinstance(raw, list) or not raw:
        raise Rejected(["'changes' must be a non-empty list of {path, value}"])
    changes = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or "value" not in item:
            raise Rejected([f"each change needs a string 'path' and a 'value': {item!r}"])
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise Rejected([f"{item['path']}: value must be a string or number"])
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        changes.append(Change(tuple(item["path"].split(".")), value))
    return changes


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def load(text: str) -> dict:
    data = YAML(typ="safe").load(text)
    return data if isinstance(data, dict) else {}


def apply_changes(text: str, changes: list[Change]) -> str:
    """Set each path in values-dev.yaml, keeping comments and layout."""
    y = _yaml()
    data = y.load(text)
    for change in changes:
        node = data
        for key in change.path[:-1]:
            if key not in node or node[key] is None:
                node[key] = {}
            node = node[key]
            if not hasattr(node, "keys"):
                raise Rejected([f"{'.'.join(change.path)}: {key} is not a mapping"])
        node[change.path[-1]] = change.value
    out = io.StringIO()
    y.dump(data, out)
    return out.getvalue()


def merge(base, override):
    """Helm semantics: maps merge recursively, everything else is replaced."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for k, v in override.items():
            merged[k] = merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return merged
    return copy.deepcopy(override)


def leaf_diff(old, new, prefix=()) -> dict:
    """{path: (old, new)} for every leaf that differs; _MISSING marks add/remove."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = {}
        for k in old.keys() | new.keys():
            out.update(leaf_diff(old.get(k, _MISSING), new.get(k, _MISSING), prefix + (str(k),)))
        return out
    if isinstance(old, dict) and new is _MISSING:
        return {p: (v, _MISSING) for p, (v, _) in leaf_diff(old, {}, prefix).items()}
    if old is _MISSING and isinstance(new, dict):
        return {p: (_MISSING, v) for p, (_, v) in leaf_diff({}, new, prefix).items()}
    return {} if old == new else {prefix: (old, new)}


def _cores(value):
    m = CPU.fullmatch(str(value))
    if not m:
        return None
    return int(m.group(1)) / 1000 if m.group(1) else float(m.group(2))


def _mebibytes(value):
    m = MEMORY.fullmatch(str(value))
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def has_hpa(svc: dict) -> bool:
    return bool(svc.get("hpa")) and not svc.get("noHpa")


def _check_leaf(path, new, merged_services, base_services, recent_tags):
    """Reasons this one changed leaf is not allowed (empty if allowed)."""
    dotted = ".".join(path)
    if new is _MISSING:
        return [f"{dotted}: removing an existing setting is not allowed"]

    if path == ("image", "tag"):
        if not TAG.fullmatch(str(new)):
            return [f"image.tag: {new!r} is not a build tag (vYYYYMMDD-HHMMSS-<sha8>)"]
        if new not in recent_tags:
            return [f"image.tag: {new} is not one of the recently deployed tags "
                    f"({', '.join(recent_tags) or 'none found'}); older images may have "
                    f"been expired from ECR"]
        return []

    if len(path) < 3 or path[0] != "services":
        return [f"{dotted}: not on the allow-list"]
    svc_name, rest = path[1], path[2:]
    if svc_name in PROTECTED_SERVICES:
        return [f"{dotted}: the agent may not change its own settings"]
    if not SERVICE.fullmatch(svc_name) or svc_name not in base_services:
        return [f"{dotted}: {svc_name!r} is not a service in the chart"]
    svc = merged_services.get(svc_name)
    if not isinstance(svc, dict):
        return [f"{dotted}: services.{svc_name} is not a mapping"]
    if svc.get("enabled") is False:
        return [f"{dotted}: {svc_name} is disabled in the chart"]

    if rest == ("replicas",):
        if has_hpa(svc):
            return [f"{dotted}: {svc_name} has an HPA, so the chart ignores replicas; "
                    f"change hpa.minReplicas/maxReplicas instead"]
        if isinstance(new, bool) or not isinstance(new, int) or not 0 <= new <= MAX_REPLICAS:
            return [f"{dotted}: must be an integer from 0 to {MAX_REPLICAS}"]
        return []

    if len(rest) == 2 and rest[0] == "hpa" and rest[1] in ("minReplicas", "maxReplicas"):
        if not has_hpa(svc):
            return [f"{dotted}: {svc_name} has no HPA; change replicas instead"]
        low, high = HPA_BOUNDS
        if isinstance(new, bool) or not isinstance(new, int) or not low <= new <= high:
            return [f"{dotted}: must be an integer from {low} to {high}"]
        return []

    if (len(rest) == 3 and rest[0] == "resources" and rest[1] in ("requests", "limits")
            and rest[2] in ("cpu", "memory")):
        if rest[2] == "cpu":
            cores = _cores(new)
            low, high = CPU_BOUNDS
            if cores is None or not low <= cores <= high:
                return [f"{dotted}: {new!r} must be a CPU quantity from {int(low * 1000)}m "
                        f"to {int(high * 1000)}m"]
        else:
            mi = _mebibytes(new)
            low, high = MEMORY_BOUNDS_MI
            if mi is None or not low <= mi <= high:
                return [f"{dotted}: {new!r} must be a memory quantity (Mi or Gi) from "
                        f"{low}Mi to {high // 1024}Gi"]
        return []

    return [f"{dotted}: not on the allow-list"]


def _check_service_consistency(svc_name, svc):
    reasons = []
    resources = svc.get("resources") or {}
    for kind, parse in (("cpu", _cores), ("memory", _mebibytes)):
        req = parse((resources.get("requests") or {}).get(kind))
        lim = parse((resources.get("limits") or {}).get(kind))
        if req is not None and lim is not None and req > lim:
            reasons.append(f"services.{svc_name}.resources: {kind} request is above its limit")
    if has_hpa(svc):
        hpa = svc["hpa"]
        if isinstance(hpa.get("minReplicas"), int) and isinstance(hpa.get("maxReplicas"), int) \
                and hpa["minReplicas"] > hpa["maxReplicas"]:
            reasons.append(f"services.{svc_name}.hpa: minReplicas is above maxReplicas")
    return reasons


def validate(old_text: str, new_text: str, base_values: dict, recent_tags: list[str]) -> dict:
    """Check the change from old_text to new_text. Returns the leaf diff; raises Rejected."""
    try:
        old, new = load(old_text), load(new_text)
    except Exception as e:
        raise Rejected([f"the new values-dev.yaml does not parse: {e}"]) from None

    reasons = []
    if len(TAG_LINE.findall(new_text)) != 1:
        reasons.append("values-dev.yaml must keep exactly one '  tag:' line "
                       "(build.yml's update-manifest rewrites it)")
    old_comments = [l.strip() for l in old_text.splitlines() if l.strip().startswith("#")]
    new_lines = {l.strip() for l in new_text.splitlines()}
    if any(c not in new_lines for c in old_comments):
        reasons.append("the change would drop comments from values-dev.yaml")

    diff = leaf_diff(old, new)
    if not diff:
        raise Rejected(reasons + ["the change does not modify anything"])
    if len(diff) > MAX_CHANGES:
        reasons.append(f"{len(diff)} settings changed; at most {MAX_CHANGES} per pull request")
    if ("image", "tag") in diff and len(diff) > 1:
        reasons.append("an image tag rollback must be the only change in its pull request")

    base_services = base_values.get("services") or {}
    merged_services = merge(base_values, new).get("services") or {}
    for path, (_, value) in sorted(diff.items()):
        reasons += _check_leaf(path, value, merged_services, base_services, recent_tags)

    for svc_name in sorted({p[1] for p in diff if len(p) > 1 and p[0] == "services"}):
        if isinstance(merged_services.get(svc_name), dict) and svc_name not in PROTECTED_SERVICES:
            reasons += _check_service_consistency(svc_name, merged_services[svc_name])

    if reasons:
        raise Rejected(reasons)
    return diff


def unified_diff(old_text: str, new_text: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


def describe(diff: dict) -> list[str]:
    def show(v):
        return "(unset)" if v is _MISSING else str(v)
    return [f"{'.'.join(p)}: {show(o)} -> {show(n)}" for p, (o, n) in sorted(diff.items())]
