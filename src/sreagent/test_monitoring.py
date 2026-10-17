"""Agent run monitoring: the cost estimate, the /metrics endpoint, and the chart
pieces that scrape and show it (ServiceMonitor, Grafana dashboard)."""

import json
import os
import re
import tempfile
import unittest

import cost
from agent import ANSWERED, MODEL_ERROR, Evidence, Investigation
from config import Config

try:
    from fastapi.testclient import TestClient

    import metrics
    import server
except ImportError:  # fastapi / httpx / prometheus_client not installed
    server = None

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "helm", "online-boutique")


def entry(model, input=0, output=0, cache_read=0, cache_write=0, stop_reason="end_turn"):
    return {"model": model, "stop_reason": stop_reason, "input": input, "output": output,
            "cache_read": cache_read, "cache_write": cache_write}


class PriceFileTest(unittest.TestCase):
    def test_the_price_file_is_dated_sourced_and_complete(self):
        prices = cost.load_prices()
        self.assertEqual(prices.checked, "2026-10-17")
        self.assertEqual(prices.source, "https://platform.claude.com/docs/en/about-claude/pricing")
        # every model the agent may run: the default, the evaluation models, and
        # Claude Sonnet 5, where server-side fallbacks send "cyber" refusals
        import evaluation
        needed = {Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns"}).model,
                  *evaluation.EVAL_MODELS, "claude-sonnet-5"}
        self.assertEqual(needed - set(prices.models), set())

    def test_prices_as_checked_on_2026_10_17(self):
        m = cost.load_prices().models
        self.assertEqual(m["claude-sonnet-5-5"], {"input": 2.0, "output": 10.0,
                                                  "cache_read": 0.10, "cache_write": 2.50})
        self.assertEqual(m["claude-opus-5-5"], {"input": 4.0, "output": 20.0,
                                                "cache_read": 0.20, "cache_write": 5.00})
        self.assertEqual(m["claude-sonnet-5"], {"input": 2.0, "output": 10.0,
                                                "cache_read": 0.20, "cache_write": 2.50})

    def test_a_price_file_missing_a_rate_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"checked": "x", "source": "y",
                       "models": {"m": {"input": 1, "output": 2, "cache_read": 0.1}}}, f)
        self.addCleanup(os.remove, f.name)
        with self.assertRaisesRegex(ValueError, "m has no price for cache_write"):
            cost.load_prices(f.name)


class CostTest(unittest.TestCase):
    prices = cost.Prices("2026-10-17", "src", {
        "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.50},
        "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.50}})

    def test_each_token_type_at_its_rate(self):
        # 1M of each: 2 + 10 + 0.10 + 2.50
        self.assertAlmostEqual(self.prices.cost("claude-sonnet-5-5", {
            "input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000,
            "cache_write": 1_000_000}), 14.60)
        # a typical investigation step: mostly cache reads
        self.assertAlmostEqual(self.prices.cost("claude-sonnet-5-5", {
            "input": 1_500, "output": 600, "cache_read": 40_000, "cache_write": 2_000}),
            (1_500 * 2 + 600 * 10 + 40_000 * 0.10 + 2_000 * 2.50) / 1e6)

    def test_summary_prices_each_model_at_its_own_rate(self):
        usage = [entry("claude-sonnet-5-5", input=10_000, stop_reason="refusal"),
                 entry("claude-sonnet-5", input=10_000, output=1_000),
                 entry("claude-sonnet-5-5", output=500, cache_read=20_000)]
        s = cost.summarize(usage, self.prices)
        self.assertEqual((s["input"], s["output"], s["cache_read"], s["cache_write"]),
                         (20_000, 1_500, 20_000, 0))
        self.assertEqual(s["by_model"]["claude-sonnet-5-5"]["cost_usd"],
                         round((10_000 * 2 + 500 * 10 + 20_000 * 0.10) / 1e6, 6))
        self.assertEqual(s["by_model"]["claude-sonnet-5"]["cost_usd"],
                         round((10_000 * 2 + 1_000 * 10) / 1e6, 6))
        self.assertEqual(s["cost_usd"], round(0.027 + 0.030, 6))
        self.assertEqual((s["unpriced_models"], s["prices_checked"]), ([], "2026-10-17"))

    def test_an_unpriced_model_is_never_guessed(self):
        s = cost.summarize([entry("claude-sonnet-5-5", input=1_000),
                            entry("claude-future-9", input=1_000)], self.prices)
        self.assertIsNone(s["cost_usd"])
        self.assertIsNone(s["by_model"]["claude-future-9"]["cost_usd"])
        self.assertEqual(s["unpriced_models"], ["claude-future-9"])
        self.assertEqual(s["input"], 2_000)                  # the tokens still count

    def test_no_model_calls_cost_nothing(self):
        self.assertEqual(cost.summarize([], self.prices)["cost_usd"], 0)


@unittest.skipIf(server is None, "fastapi/httpx/prometheus_client not installed")
class MetricsTest(unittest.TestCase):
    TOKEN = "t-123"
    AUTH = {"Authorization": "Bearer t-123"}

    class Agent:
        """Builds itself as the agent factory; each run returns `result`."""

        def __init__(self, result):
            self.result = result

        def __call__(self, extra_tools):
            self.extra = extra_tools
            return self

        def run(self, system, task):
            return self.result

    def client(self, result, **env):
        cfg = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "online-boutique-dev",
                               "SREAGENT_API_TOKEN": self.TOKEN, **env})
        pending = []
        self.run_spawned = lambda: [pending.pop(0)() for _ in list(pending)]
        return TestClient(server.create_app(cfg, self.Agent(result), spawn=pending.append))

    def scrape(self, client):
        resp = client.get("/metrics")                      # no token
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))
        return resp.text

    @staticmethod
    def value(text, name, **labels):
        for line in text.splitlines():
            if line.startswith(name + "{") or line.startswith(name + " "):
                found = dict(re.findall(r'(\w+)="([^"]*)"', line.split(" ")[0]))
                if all(found.get(k) == v for k, v in labels.items()):
                    return float(line.rsplit(" ", 1)[1])
        return None

    RESULT = Investigation(ANSWERED, "All good.", [
        Evidence("list_pods", {"app": "cartservice"}, "ok", False),
        Evidence("describe_deployment", {"name": "cartservice"}, "gone", True)], 42.0,
        [entry("claude-sonnet-5-5", input=2_000, output=500, cache_read=30_000, cache_write=1_000,
               stop_reason="tool_use"),
         entry("claude-sonnet-5-5", input=300, output=800, cache_read=33_000)])

    def test_an_ask_is_counted_timed_and_costed(self):
        client = self.client(self.RESULT)
        text = self.scrape(client)
        self.assertEqual(self.value(text, "sreagent_investigations_total", kind="ask",
                                    outcome="answered"), 0)    # starts at 0 for increase()
        self.assertEqual(self.value(text, "sreagent_investigation_in_progress"), 0)

        record = client.post("/ask", json={"question": "Is checkout healthy? secret-question-text"},
                             headers=self.AUTH).json()
        expected = round((2_300 * 2 + 1_300 * 10 + 63_000 * 0.10 + 1_000 * 2.50) / 1e6, 6)
        self.assertEqual(record["usage"]["cost_usd"], expected)
        self.assertEqual(record["usage"]["input"], 2_300)
        self.assertEqual(record["usage"]["prices_checked"], "2026-10-17")

        text = self.scrape(client)
        self.assertEqual(self.value(text, "sreagent_investigations_total", kind="ask",
                                    outcome="answered", model="claude-sonnet-5-5"), 1)
        self.assertEqual(self.value(text, "sreagent_investigation_duration_seconds_sum", kind="ask"), 42)
        self.assertEqual(self.value(text, "sreagent_investigation_tool_calls_sum", kind="ask"), 2)
        self.assertEqual(self.value(text, "sreagent_tool_calls_total", tool="describe_deployment",
                                    result="error"), 1)
        self.assertEqual(self.value(text, "sreagent_model_requests_total", model="claude-sonnet-5-5",
                                    stop_reason="tool_use"), 1)
        self.assertEqual(self.value(text, "sreagent_model_tokens_total", model="claude-sonnet-5-5",
                                    type="cache_read"), 63_000)
        self.assertAlmostEqual(self.value(text, "sreagent_cost_usd_total", model="claude-sonnet-5-5"),
                               expected)
        self.assertAlmostEqual(self.value(text, "sreagent_investigation_cost_usd_sum", kind="ask"),
                               expected)
        self.assertEqual(self.value(text, "sreagent_assessments_total", kind="ask",
                                    support="not_assessed"), 1)
        self.assertEqual(self.value(text, "sreagent_info", model="claude-sonnet-5-5"), 1)
        # never an investigation id or the question in the metrics
        self.assertNotIn(record["id"], text)
        self.assertNotIn("secret-question-text", text)

    def test_alert_decisions_by_service(self):
        client = self.client(Investigation(MODEL_ERROR, "Model call failed", [], 1.0, []))

        def alert(name="PodCrashLooping", ns="online-boutique-dev", service="cartservice"):
            return {"version": "4", "status": "firing", "alerts": [{
                "status": "firing", "startsAt": "2026-10-17T12:00:00Z",
                "labels": {"alertname": name, "namespace": ns, "workload": service,
                           "pod": f"{service}-6b9f7c8d4-aaaaa", "severity": "critical"},
                "annotations": {"summary": "secret-alert-summary"}}]}

        client.post("/alert", json=alert(), headers=self.AUTH)                  # investigated
        self.run_spawned()
        client.post("/alert", json=alert("PodNotReady"), headers=self.AUTH)     # same incident
        client.post("/alert", json=alert(ns="kube-system", service="coredns"), headers=self.AUTH)
        text = self.scrape(client)
        self.assertEqual(self.value(text, "sreagent_alert_decisions_total", decision="investigated",
                                    service="cartservice"), 1)
        self.assertEqual(self.value(text, "sreagent_alert_decisions_total",
                                    decision="added_to_incident", service="cartservice"), 1)
        # not about the app: no service label value from the alert
        self.assertEqual(self.value(text, "sreagent_alert_decisions_total", decision="not_worth",
                                    service="-"), 1)
        self.assertNotIn("coredns", text)
        self.assertNotIn("secret-alert-summary", text)
        self.assertEqual(self.value(text, "sreagent_investigations_total", kind="alert",
                                    outcome="model_error"), 1)

    def test_metrics_has_its_own_registry_per_app(self):
        a = metrics.Metrics("m1")
        b = metrics.Metrics("m2")
        a.decision("busy", "x")
        self.assertIn(b'decision="busy"', a.exposition())
        self.assertNotIn(b'decision="busy"', b.exposition())


class ChartTest(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(CHART, *path), encoding="utf-8") as f:
            return f.read()

    def test_the_agent_has_a_service_monitor_on_its_http_port(self):
        values = self.read("values.yaml")
        block = values[values.index("\n  sreagent:\n"):]
        block = block[:block.index("\n  shoppingassistantservice:")]
        self.assertIn("    protocol: http\n", block)
        self.assertIn("    serviceMonitor:\n      port: http\n      path: /metrics\n", block)
        template = self.read("templates", "servicemonitors.yaml")
        self.assertIn("release: monitoring", template)   # what kube-prometheus-stack selects

    def test_the_dashboard_is_a_labelled_configmap_with_valid_json(self):
        template = self.read("templates", "dashboards.yaml")
        self.assertIn('grafana_dashboard: "1"', template)
        self.assertIn('.Files.Get "dashboards/sreagent.json"', template)
        dashboard = json.loads(self.read("dashboards", "sreagent.json"))
        self.assertEqual(dashboard["uid"], "sreagent")
        exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
        self.assertGreaterEqual(len([p for p in dashboard["panels"] if p["type"] != "row"]), 15)
        # every metric the dashboard reads is one the agent exports (or `up`)
        source = open(os.path.join(HERE, "metrics.py"), encoding="utf-8").read()
        exported = set(re.findall(r'"(sreagent_\w+)"', source))
        used = {m for e in exprs for m in re.findall(r"\b(sreagent_\w+?)(?:_bucket|_sum|_count)?\b", e)}
        self.assertEqual(used - exported, set())
        self.assertTrue(all('namespace="$namespace"' in e for e in exprs))


if __name__ == "__main__":
    unittest.main()
