"""Read-only Prometheus tools: instant and range queries via the HTTP API."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from tools import Tool, ToolError, bounded_int, require_str

MAX_SERIES = 25
MAX_RANGE_POINTS = 300   # Prometheus allows 11,000; the model needs far fewer
SAMPLE_POINTS = 10       # points shown per range series, evenly spaced


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 15.0,
                 opener=urllib.request.urlopen):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._open = opener

    def instant(self, query: str) -> dict:
        return self._get("/api/v1/query", {"query": query})

    def range(self, query: str, start: float, end: float, step: int) -> dict:
        return self._get("/api/v1/query_range",
                         {"query": query, "start": start, "end": end, "step": step})

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with self._open(url, timeout=self._timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            # Bad PromQL comes back as HTTP 400 with an explanation; pass it to
            # the model so it can fix the query.
            raise ToolError(f"Prometheus HTTP {e.code}: {_error_text(e)}") from None
        except urllib.error.URLError as e:
            raise ToolError(f"Prometheus unreachable at {self._base_url}: {e.reason}") from None
        if body.get("status") != "success":
            raise ToolError(f"Prometheus error: {body.get('error', body)}")
        return body["data"]


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        return json.load(e).get("error", "")
    except Exception:
        return e.reason


def _labels(metric: dict) -> str:
    name = metric.get("__name__", "")
    rest = ", ".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}"


def _num(value: str) -> str:
    try:
        return f"{float(value):.4g}"
    except ValueError:
        return value


def format_result(data: dict) -> str:
    """Render a Prometheus result compactly: one line per series."""
    kind, result = data.get("resultType"), data.get("result")

    if kind in ("scalar", "string"):
        return f"{kind}: {_num(result[1])}"
    if not result:
        return "No data (the query matched no series)."

    lines = [f"{len(result)} series" + (f", showing first {MAX_SERIES}"
                                        if len(result) > MAX_SERIES else "")]
    for series in result[:MAX_SERIES]:
        labels = _labels(series["metric"])
        if kind == "vector":
            lines.append(f"{labels} = {_num(series['value'][1])}")
            continue
        # matrix: summary stats plus an evenly spaced sample of points
        points = series["values"]
        values = [float(v) for _, v in points if v not in ("NaN", "+Inf", "-Inf")]
        stride = max(1, len(points) // SAMPLE_POINTS)
        sample = ", ".join(
            f"{time.strftime('%H:%M', time.gmtime(float(ts)))}={_num(v)}"
            for ts, v in points[::stride][:SAMPLE_POINTS])
        stats = (f"min={min(values):.4g} max={max(values):.4g} last={_num(points[-1][1])}"
                 if values else "no numeric values")
        lines.append(f"{labels}: {len(points)} points, {stats}; samples (UTC): {sample}")
    return "\n".join(lines)


def make_tools(prom: PrometheusClient, clock=time.time) -> list[Tool]:
    def query(args: dict) -> str:
        return format_result(prom.instant(require_str(args, "query")))

    def query_range(args: dict) -> str:
        expr = require_str(args, "query")
        minutes = bounded_int(args, "minutes", 60, 1, 24 * 60)
        step = bounded_int(args, "step_seconds", 60, 15, 3600)
        # Widen the step rather than return thousands of points
        step = max(step, (minutes * 60) // MAX_RANGE_POINTS)
        end = clock()
        return format_result(prom.range(expr, end - minutes * 60, end, step))

    return [
        Tool(
            name="prometheus_query",
            description=(
                "Run an instant PromQL query against the cluster's Prometheus and "
                "return the current value of each matching series. Use for 'what is "
                "it now' questions, e.g. "
                "kube_deployment_status_replicas_available{namespace=\"...\"}."),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "PromQL expression"}},
                "required": ["query"],
            },
            handler=query,
        ),
        Tool(
            name="prometheus_query_range",
            description=(
                "Run a PromQL range query over the last N minutes and return, per "
                "series, min/max/last and a sample of points. Use to see when a "
                "problem started and whether it is getting worse."),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL expression"},
                    "minutes": {"type": "integer", "description": "How far back to look (1-1440, default 60)"},
                    "step_seconds": {"type": "integer", "description": "Resolution in seconds (default 60)"},
                },
                "required": ["query"],
            },
            handler=query_range,
        ),
    ]
