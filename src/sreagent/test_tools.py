"""Tests for redaction, the tool modules' formatting/validation, and alert parsing."""

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import github_tools
import k8s_tools
import prometheus_tools
import prompts
from redact import redact, truncate
from tools import ToolError, ToolRegistry


class RedactTest(unittest.TestCase):
    def test_known_secret_formats(self):
        secrets = [
            "sk-ant-api03-" + "x" * 40,
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 60,
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJlc2lnbmF0dXJl",
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:12]):
                out = redact(f"value: {secret} end")
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED]", out)

    def test_key_value_pairs_and_headers(self):
        cases = {
            "password=hunter22": "hunter22",
            'DB_PASSWORD: "s3cr3tvalue"': "s3cr3tvalue",
            "api_key=abcd1234efgh": "abcd1234efgh",
            "Authorization: Bearer abc.def.ghi123": "abc.def.ghi123",
            "redis://user:p4ssw0rd@redis:6379": "p4ssw0rd",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))

    def test_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(redact(f"a {pem} b"), "a [REDACTED] b")

    def test_ordinary_text_is_untouched(self):
        text = 'max_tokens=100 grpc_code="Unavailable" memory=512Mi cpu: 100m'
        self.assertEqual(redact(text), text)

    def test_infra_identifiers_get_numbered_placeholders(self):
        from redact import redact_infra, redact_public
        text = ("pods on ip-10-0-10-135.ec2.internal and ip-10-0-11-45.ec2.internal, again "
                "ip-10-0-10-135.ec2.internal; node ip-10-0-1-5.us-east-1.compute.internal")
        self.assertEqual(redact_infra(text), "pods on <node-1> and <node-2>, again <node-1>; "
                                             "node <node-3>")
        self.assertEqual(redact_infra('connect "10.0.11.207:8080"; dns 172.20.0.10:53; 10.0.11.207'),
                         'connect "<ip-1>:8080"; dns <ip-2>:53; <ip-1>')
        self.assertEqual(
            redact_infra("073759315444.dkr.ecr.us-east-1.amazonaws.com/online-boutique-dev-x:v20261004"),
            "<account-id>.dkr.ecr.us-east-1.amazonaws.com/online-boutique-dev-x:v20261004")
        self.assertEqual(redact_infra("arn:aws:iam::073759315444:role/r arn:aws:sns:us-east-1:073759315444:t"),
                         "arn:aws:iam::<account-id>:role/r arn:aws:sns:us-east-1:<account-id>:t")
        self.assertEqual(redact_public("token=abcdef123456 on 10.1.2.3"), "token=[REDACTED] on <ip-1>")

    def test_instance_ids_resource_name_hosts_and_load_balancers(self):
        from redact import redact_infra
        self.assertEqual(
            redact_infra("aws:///us-east-1a/i-0abc123def4567890 old i-1a2b3c4d; host "
                         "i-0abc123def4567890.ec2.internal; lb k8s-onlinebo-frontend-1a2b3c4d5e-"
                         "1234567890.us-east-1.elb.amazonaws.com"),
            "aws:///us-east-1a/<instance-1> old <instance-2>; host <node-1>; lb <lb-1>")
        text = "pod cartservice-7f9c8d-x2kq; i-am-fine; hi-0abc12345678"
        self.assertEqual(redact_infra(text), text)

    # Shapes of real AWS hostnames; the ids are made up
    REDIS = "online-boutique-dev-redis.abc123.ng.0001.use1.cache.amazonaws.com"
    DB = "online-boutique-dev-postgres.c9xyz1abcd2e.us-east-1.rds.amazonaws.com"
    EFS = "fs-0123456789abcdef0.efs.us-east-1.amazonaws.com"
    EKS = "EF4A454DC45744A5FD2C489FDD29EFCC.gr7.us-east-1.eks.amazonaws.com"

    def test_aws_service_hostnames_keep_their_ports(self):
        from redact import redact_infra
        cases = {
            f"redis.addr: {self.REDIS}:6379": "redis.addr: <redis-1>:6379",
            f"DATABASE_HOST={self.DB}:5432": "DATABASE_HOST=<db-1>:5432",
            f"mount {self.EFS}:/ and us-east-1a.{self.EFS}:/": "mount <efs-1>:/ and <efs-2>:/",
            f'dial tcp: lookup {self.EKS}: no such host': "dial tcp: lookup <aws-host-1>: no such host",
            "sts.us-east-1.amazonaws.com, my-bucket.s3.us-east-1.amazonaws.com":
                "<aws-host-1>, <aws-host-2>",
            "postgres.abc.cn-north-1.rds.amazonaws.com.cn": "<db-1>.cn",
        }
        for text, want in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(redact_infra(text), want)

    def test_one_mapping_numbers_aws_hosts_across_texts(self):
        from redact import redact_infra
        names = {}
        first = redact_infra(f"{self.REDIS}:6379 then {self.DB}", names)
        second = redact_infra(f"other.def456.ng.0001.use1.cache.amazonaws.com and {self.REDIS}", names)
        self.assertEqual((first, second), ("<redis-1>:6379 then <db-1>", "<redis-2> and <redis-1>"))

    def test_the_ecr_registry_stays_readable(self):
        # Its account ID is replaced; the general rule leaves the rest of the host
        from redact import redact_infra
        image = "<account-id>.dkr.ecr.us-east-1.amazonaws.com/online-boutique-dev-frontend:v20261025"
        self.assertEqual(redact_infra(image), image)
        self.assertEqual(redact_infra(image.replace("<account-id>", "073759315444")), image)

    def test_lint_finds_each_unredacted_aws_host(self):
        import evaluation
        for host, kind in ((self.REDIS, "redis"), (self.DB, "db"), (self.EFS, "efs"),
                           (self.EKS, "aws-host")):
            with self.subTest(kind=kind):
                # each pattern is checked on its own, so the general rule may report it too
                self.assertIn(f"{kind}: {host[:40]}", evaluation._leaks(f"value: {host}:1234"))
        self.assertEqual(evaluation._leaks(
            "Pulling <account-id>.dkr.ecr.us-east-1.amazonaws.com/x:v1, value: <redis-1>:6379"), [])

    def test_prometheus_output_shares_the_mapping_and_keeps_ips(self):
        # The Prometheus tools redact AWS hostnames only, numbered as the Kubernetes tools number them
        from redact import redact_infra
        names = {}
        k8s = redact_infra(f"redis.addr: {self.REDIS}:6379 on 10.0.1.5", names)

        class Prom:
            def instant(self, query):
                return {"resultType": "vector", "result": [
                    {"metric": {"instance": "10.0.11.7:9121", "addr": f"{TestRedis.REDIS}:6379"},
                     "value": [0, "1"]}]}

        TestRedis = type(self)
        tool = {t.name: t for t in prometheus_tools.make_tools(Prom(), names=names)}["prometheus_query"]
        out = tool.handler({"query": "redis_up"})
        self.assertEqual(k8s, "redis.addr: <redis-1>:6379 on <ip-1>")
        self.assertIn('addr="<redis-1>:6379"', out)
        self.assertIn('instance="10.0.11.7:9121"', out)   # series IPs stay

    def test_infra_redaction_leaves_ordinary_numbers(self):
        from redact import redact_infra
        text = ("v1.29.3 4.295e7 1.2.3 999.1.1.1 1.2.3.4.5 count 123456789012 "
                "2026-10-04T15:45:34Z sha 20b4232a tag v20261004-134739-d34db63e 37.5%")
        self.assertEqual(redact_infra(text), text)

    def test_truncate_tail_mode_keeps_head_and_tail(self):
        out = truncate("H" * 50 + "M" * 1000 + "T" * 50, 120, keep="tail")
        self.assertTrue(out.startswith("H"))
        self.assertTrue(out.endswith("T" * 50))
        self.assertIn("truncated 980 characters", out)
        self.assertEqual(truncate("short", 100), "short")

    def test_truncate_head_mode_keeps_the_start(self):
        out = truncate("H" * 100 + "T" * 100, 120, keep="head")
        self.assertTrue(out.startswith("H" * 100 + "T" * 20))
        self.assertTrue(out.endswith("[truncated 80 characters]"))


class PrometheusFormatTest(unittest.TestCase):
    def test_vector(self):
        data = {"resultType": "vector", "result": [
            {"metric": {"__name__": "up", "pod": "cart-1"}, "value": [1, "1"]}]}
        self.assertEqual(prometheus_tools.format_result(data), '1 series\nup{pod="cart-1"} = 1')

    def test_empty(self):
        self.assertIn("No data", prometheus_tools.format_result({"resultType": "vector", "result": []}))

    def test_matrix_summary(self):
        values = [[1700000000 + 60 * i, str(i)] for i in range(100)]
        data = {"resultType": "matrix", "result": [{"metric": {"pod": "a"}, "values": values}]}
        out = prometheus_tools.format_result(data)
        self.assertIn("100 points, min=0 max=99 last=99", out)
        self.assertEqual(out.count("="), 4 + 10)  # label, min, max, last + 10 samples

    def test_series_are_capped(self):
        result = [{"metric": {"i": str(i)}, "value": [0, "1"]} for i in range(40)]
        out = prometheus_tools.format_result({"resultType": "vector", "result": result})
        self.assertIn("40 series, showing first 25", out)
        self.assertEqual(len(out.splitlines()), 26)

    def test_range_query_widens_step(self):
        seen = {}

        class FakeProm:
            def range(self, query, start, end, step):
                seen.update(start=start, end=end, step=step)
                return {"resultType": "matrix", "result": []}

        tool = {t.name: t for t in prometheus_tools.make_tools(FakeProm(), clock=lambda: 100000.0)}
        tool["prometheus_query_range"].handler({"query": "up", "minutes": 1440, "step_seconds": 15})
        self.assertEqual(seen["end"] - seen["start"], 1440 * 60)
        self.assertEqual(seen["step"], 1440 * 60 // prometheus_tools.MAX_RANGE_POINTS)

    def test_bad_promql_becomes_tool_error(self):
        def opener(url, timeout):
            body = io.BytesIO(json.dumps({"status": "error", "error": "parse error"}).encode())
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)

        prom = prometheus_tools.PrometheusClient("http://prom", opener=opener)
        with self.assertRaisesRegex(ToolError, "HTTP 400: parse error"):
            prom.instant("rate(")


class KubeFormatTest(unittest.TestCase):
    def test_pod_with_oomkill(self):
        now = datetime.now(timezone.utc)
        status = NS(name="server", ready=False, restart_count=4,
                    state=NS(waiting=NS(reason="CrashLoopBackOff")),
                    last_state=NS(terminated=NS(reason="OOMKilled", exit_code=137,
                                                finished_at=now - timedelta(minutes=3))))
        pod = NS(metadata=NS(name="cartservice-abc", creation_timestamp=now - timedelta(hours=2),
                             owner_references=[NS(kind="ReplicaSet", name="cartservice-7f9")]),
                 status=NS(phase="Running", container_statuses=[status]),
                 spec=NS(node_name="ip-10-0-1-1"))
        out = k8s_tools.format_pod(pod)
        self.assertIn("cartservice-abc: Running, ready 0/1, restarts 4, age 2h", out)
        self.assertIn("owner ReplicaSet/cartservice-7f9", out)
        self.assertIn("server waiting: CrashLoopBackOff", out)
        self.assertIn("last exit: OOMKilled (code 137, 3m ago)", out)

    @staticmethod
    def _event(kind, name, reason, component, minutes_ago, type_="Normal",
               count=1, first_minutes_ago=None):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(minutes=minutes_ago)
        first = now - timedelta(minutes=first_minutes_ago) if first_minutes_ago else ts
        return NS(type=type_, reason=reason, message=f"{reason} message", count=count,
                  first_timestamp=first, last_timestamp=ts, event_time=None,
                  metadata=NS(creation_timestamp=ts),
                  involved_object=NS(kind=kind, name=name),
                  source=NS(component=component), reporting_component=None)

    def _list_events(self, events, **args):
        core = NS(list_namespaced_event=lambda ns, field_selector=None: NS(items=list(events)))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        tools = {t.name: t for t in k8s_tools.make_tools(kube)}
        return tools["list_events"].handler(args).splitlines()

    def test_events_by_app_cover_the_ownership_chain_controllers_first(self):
        # Shaped like the live emailservice cycle (regressions/emailservice-events-raw.txt)
        events = [
            self._event("Pod", "emailservice-6b9f7c-new01", "Unhealthy", "kubelet", 4, "Warning", count=2),
            self._event("Pod", "emailservice-6b9f7c-new01", "Started", "kubelet", 4),
            self._event("Pod", "emailservice-6b9f7c-x2k4p", "Killing", "kubelet", 5),
            self._event("ReplicaSet", "emailservice-6b9f7c", "SuccessfulDelete", "replicaset-controller", 5),
            self._event("Deployment", "emailservice", "ScalingReplicaSet", "deployment-controller", 4,
                        count=19, first_minutes_ago=95),
            self._event("HorizontalPodAutoscaler", "emailservice", "SuccessfulRescale",
                        "horizontal-pod-autoscaler", 6, count=18, first_minutes_ago=96),
            self._event("Pod", "emailservicex-1", "Started", "kubelet", 1),  # different app
            self._event("Pod", "cartservice-abc", "Killing", "kubelet", 1),
        ]

        lines = self._list_events(events, app="emailservice")

        self.assertEqual(lines[0], "Controller events (HPA, Deployment, ReplicaSet, ...), newest first:")
        self.assertEqual([l.split()[4].split("/")[0] for l in lines[1:4]],
                         ["Deployment", "ReplicaSet", "HorizontalPodAutoscaler"])
        self.assertIn("ScalingReplicaSet Deployment/emailservice [deployment-controller] "
                      "(x19 since 1h35m ago)", lines[1])
        self.assertIn("[horizontal-pod-autoscaler] (x18 since 1h36m ago)", lines[3])
        self.assertEqual(lines[4], "Pod events, newest first:")
        self.assertEqual(len(lines), 8)
        text = "\n".join(lines)
        self.assertIn("Unhealthy Pod/emailservice-6b9f7c-new01 [kubelet] (x2 since 4m ago)", text)
        self.assertNotIn("cartservice", text)
        self.assertNotIn("emailservicex", text)

    def test_controller_events_survive_truncation(self):
        events = [self._event("Pod", f"emailservice-6b9f7c-p{i:04d}", "Pulled", "kubelet", 1)
                  for i in range(40)]
        events.append(self._event("Deployment", "emailservice", "ScalingReplicaSet",
                                  "deployment-controller", 30, count=19, first_minutes_ago=95))
        core = NS(list_namespaced_event=lambda ns, field_selector=None: NS(items=events))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        registry = ToolRegistry(k8s_tools.make_tools(kube), max_output_chars=600)

        output, is_error = registry.run("list_events", {"app": "emailservice"})

        self.assertFalse(is_error)
        self.assertIn("ScalingReplicaSet Deployment/emailservice", output)
        self.assertIn("truncated", output)

    def test_describe_deployment_after_the_replicas_fix(self):
        now = datetime.now(timezone.utc)

        def rs(name, revision, replicas, tag, minutes):
            return NS(metadata=NS(name=name, annotations={k8s_tools.REVISION: str(revision)},
                                  creation_timestamp=now - timedelta(minutes=minutes)),
                      spec=NS(replicas=replicas, template=NS(spec=NS(containers=[
                          NS(image=f"123456789012.dkr.ecr.us-east-1.amazonaws.com/x-emailservice:{tag}")]))),
                      status=NS(ready_replicas=replicas))

        dep = NS(
            metadata=NS(name="emailservice", generation=49, annotations={
                k8s_tools.REVISION: "5",
                k8s_tools.LAST_APPLIED: '{"kind": "Deployment", "spec": {"selector": {}}}'},
                managed_fields=[
                    NS(manager="argocd-controller", operation="Update", subresource=None,
                       time=now - timedelta(minutes=20), fields_v1={"f:spec": {"f:template": {}}}),
                    NS(manager="kube-controller-manager", operation="Update", subresource="scale",
                       time=now - timedelta(minutes=10), fields_v1={"f:spec": {"f:replicas": {}}}),
                ]),
            spec=NS(replicas=1),
            status=NS(observed_generation=49, replicas=1, ready_replicas=1, updated_replicas=1,
                      available_replicas=1))
        sets = [rs("emailservice-7959ddb655", 4, 0, "v20261004-134739-d34db63e", 180),
                rs("emailservice-748b648c5f", 5, 1, "v20261004-154933-68ac8238", 18)]

        out = k8s_tools.describe_deployment(dep, sets).splitlines()

        self.assertEqual(out[1], "Live spec.replicas: 1; status: replicas 1, ready 1, updated 1, available 1")
        self.assertIn("last applied manifest", out[2])
        self.assertTrue(out[2].endswith("absent (the manifest does not set it)"))
        self.assertEqual(out[3], "Field managers of spec.replicas: "
                                 "kube-controller-manager (Update, subresource scale, 10m ago)")
        self.assertTrue(out[5].startswith("  emailservice-748b648c5f: revision 5 (current), desired 1"))
        self.assertIn("image tag v20261004-154933-68ac8238", out[5])
        self.assertTrue(out[6].startswith("  emailservice-7959ddb655: revision 4, desired 0"))

    def test_describe_deployment_before_the_fix_shows_replicas_in_manifest(self):
        dep = NS(metadata=NS(name="emailservice", generation=40, managed_fields=[],
                             annotations={k8s_tools.LAST_APPLIED: '{"spec": {"replicas": 2}}'}),
                 spec=NS(replicas=2), status=NS(observed_generation=40, replicas=2, ready_replicas=2,
                                               updated_replicas=2, available_replicas=2))
        out = k8s_tools.describe_deployment(dep, [])
        self.assertIn("present, = 2", out)
        self.assertIn("Field managers of spec.replicas: none recorded", out)

    def test_rejects_invalid_names(self):
        tools = {t.name: t for t in k8s_tools.make_tools(k8s_tools.KubeReader("ns", core=object()))}
        for bad in ("../etc", "Pod_Name", "a b", ""):
            with self.subTest(name=bad), self.assertRaises(ToolError):
                tools["get_pod_logs"].handler({"pod": bad})


class PodLogsTest(unittest.TestCase):
    """get_pod_logs returns the log as text. The pinned kubernetes client
    decodes a log body into str(bytes): b'...' on one line, every newline and
    non-ASCII byte escaped. The tool asks for the raw response instead."""

    BODY = ("starting on 10.0.11.231:7070\nline two ünï\n".encode() + b"\xff end\n")

    def real_client_core(self):
        """The real CoreV1Api, with only the HTTP call faked."""
        import urllib3
        from kubernetes import client

        def request(method, url, **kw):
            self.query = dict(kw.get("fields") or [])
            return urllib3.HTTPResponse(body=self.BODY, status=200,
                                        headers={"Content-Type": "text/plain"},
                                        preload_content=kw.get("preload_content", True))

        api = client.ApiClient(client.Configuration(host="http://k8s.invalid"))
        api.rest_client.pool_manager.request = request
        return client.CoreV1Api(api)

    def test_the_client_decodes_a_log_into_a_bytes_repr(self):
        # Why the tool does not use the client's default: this is what it returns
        out = self.real_client_core().read_namespaced_pod_log("p", "ns")
        self.assertTrue(out.startswith("b'starting on"))
        self.assertIn("\\n", out)

    def test_logs_are_text_with_real_newlines(self):
        core = self.real_client_core()
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        registry = ToolRegistry(k8s_tools.make_tools(kube), 6000)
        out, is_error = registry.run("get_pod_logs", {"pod": "cartservice-abc", "tail_lines": 20})
        self.assertFalse(is_error, out)
        self.assertEqual(out.splitlines(), ["starting on <ip-1>:7070", "line two ünï", "� end"])
        self.assertNotIn("b'", out)
        self.assertEqual(self.query, {"tailLines": 20, "previous": False})

    def test_redaction_and_the_size_cap_still_apply(self):
        token = "ghp_" + "a" * 36
        lines = [f"req {i} from 10.0.1.{i % 250} token={token}" for i in range(2000)] + ["the last line"]
        core = NS(read_namespaced_pod_log=lambda *a, **kw: NS(data="\n".join(lines).encode()))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        out, _ = ToolRegistry(k8s_tools.make_tools(kube), 3000).run("get_pod_logs", {"pod": "p"})
        self.assertNotIn(token, out)
        self.assertNotIn("10.0.1.", out)
        self.assertLess(len(out), 3200)
        self.assertTrue(out.endswith("the last line"))      # logs keep their newest end

    def test_an_empty_log(self):
        core = NS(read_namespaced_pod_log=lambda *a, **kw: NS(data=b""))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        tool = {t.name: t for t in k8s_tools.make_tools(kube)}["get_pod_logs"]
        self.assertEqual(tool.handler({"pod": "p"}), "(no log output)")


def _raw(obj):
    """What a typed client returns with _preload_content=False."""
    return NS(data=json.dumps(obj).encode())


NODE_NAME = "ip-10-0-11-231.ec2.internal"
NODE = {
    "metadata": {
        "name": NODE_NAME, "uid": "5f1c-uid", "resourceVersion": "991",
        "creationTimestamp": "2026-10-20T10:00:00Z",
        "managedFields": [{"manager": "kubelet", "fieldsV1": {"f:status": {}}}],
        "labels": {"kubernetes.io/hostname": NODE_NAME,
                   "node.kubernetes.io/instance-type": "t3.large",
                   "topology.kubernetes.io/zone": "us-east-1a",
                   "eks.amazonaws.com/nodegroup": "online-boutique-dev-ng"},
        "annotations": {"alpha.kubernetes.io/provided-node-ip": "10.0.11.231",
                        "csi.volume.kubernetes.io/nodeid": '{"ebs.csi.aws.com":"i-0abc123def4567890"}'}},
    "spec": {"providerID": "aws:///us-east-1a/i-0abc123def4567890", "taints": []},
    "status": {
        "addresses": [{"type": "InternalIP", "address": "10.0.11.231"},
                      {"type": "Hostname", "address": NODE_NAME},
                      {"type": "InternalDNS", "address": NODE_NAME}],
        "allocatable": {"cpu": "1930m", "memory": "7220184Ki", "pods": "35"},
        "capacity": {"cpu": "2", "memory": "8047576Ki", "pods": "35"},
        "conditions": [{"type": "MemoryPressure", "status": "False"},
                       {"type": "Ready", "status": "True", "reason": "KubeletReady",
                        "lastHeartbeatTime": "2026-10-24T12:00:00Z"}],
        "nodeInfo": {"kubeletVersion": "v1.35.8-eks", "machineID": "ec2abc", "systemUUID": "ec2def",
                     "bootID": "b00t"},
        "images": [{"names": [f"registry/image-{i}:v1"], "sizeBytes": 1} for i in range(200)]},
}


class GetResourceTest(unittest.TestCase):
    """get_resource: the allow-list of kinds and namespaces, trimming, the size
    cap, and redaction of node data - through the registry, as the model sees it."""

    def setUp(self):
        self.calls = []

        def record(name, value):
            def call(*args, **kwargs):
                self.calls.append((name, args))
                return value(*args) if callable(value) else value
            return call

        svc = {"metadata": {"name": "cartservice"},
               "spec": {"type": "ClusterIP", "selector": {"app": "cartservice-v2"},
                        "ports": [{"name": "grpc", "port": 7070, "targetPort": 7070}]}}
        self.core = NS(
            list_namespaced_service=record("list_services", _raw({"items": [svc]})),
            read_namespaced_service=record("read_service", _raw(svc)),
            list_node=record("list_nodes", _raw({"items": [NODE]})),
            read_node=record("read_node", lambda name: _raw(NODE)),
            list_namespaced_config_map=record("list_cm", None),
            read_namespaced_config_map=record("read_cm", None),
            list_namespaced_pod=record("list_pods", NS(items=[self._pod()])),
        )
        self.custom = NS(list_namespaced_custom_object=record(
            "list_apps", {"items": [{"metadata": {"name": "online-boutique"},
                                     "status": {"sync": {"status": "OutOfSync"}}}]}))
        self.kube = k8s_tools.KubeReader("online-boutique-dev", core=self.core, apps=None,
                                         autoscaling=None, apis={"custom": self.custom})
        self.registry = ToolRegistry(k8s_tools.make_tools(self.kube), 6000)

    @staticmethod
    def _pod():
        return NS(metadata=NS(name="cartservice-abc", creation_timestamp=None, owner_references=[]),
                  status=NS(phase="Running", container_statuses=[]), spec=NS(node_name=NODE_NAME))

    def run_tool(self, **args):
        return self.registry.run("get_resource", args)

    def test_secrets_are_rejected_in_any_spelling_without_an_api_call(self):
        for kind in ("secrets", "Secret", "secret", " SECRETS "):
            with self.subTest(kind=kind):
                out, is_error = self.run_tool(kind=kind)
                self.assertTrue(is_error)
                self.assertIn("Secrets are never readable", out)
        self.assertEqual(self.calls, [])

    def test_unlisted_kinds_are_rejected(self):
        for kind in ("pods", "deployments", "roles", "clusterroles", "serviceaccounts",
                     "customresourcedefinitions", "networkpolicies", ""):
            with self.subTest(kind=kind):
                out, is_error = self.run_tool(kind=kind)
                self.assertTrue(is_error)
        self.assertEqual(self.calls, [])

    def test_each_kind_is_read_only_in_its_one_namespace(self):
        for args in ({"kind": "services", "namespace": "kube-system"},
                     {"kind": "services", "namespace": "argocd"},
                     {"kind": "configmaps", "namespace": "monitoring"},
                     {"kind": "applications", "namespace": "online-boutique-dev"},
                     {"kind": "services", "namespace": "../x"}):
            with self.subTest(args=args):
                out, is_error = self.run_tool(**args)
                self.assertTrue(is_error)
        self.assertEqual(self.calls, [])
        self.run_tool(kind="services", namespace="online-boutique-dev")
        self.run_tool(kind="applications")
        self.assertEqual(self.calls, [("list_services", ("online-boutique-dev",)),
                                      ("list_apps", ("argoproj.io", "v1alpha1", "argocd", "applications"))])

    def test_a_list_is_one_summary_line_per_object(self):
        out, is_error = self.run_tool(kind="service")
        self.assertFalse(is_error)
        self.assertEqual(out, "1 services in online-boutique-dev:\n"
                              "cartservice: ClusterIP, selector app=cartservice-v2, ports grpc 7070->7070/TCP")

    def test_node_data_is_redacted_list_and_detail(self):
        listed, _ = self.run_tool(kind="nodes")
        detail, is_error = self.run_tool(kind="nodes", name="<node-1>")
        self.assertFalse(is_error, detail)
        for out in (listed, detail):
            for leak in ("10.0.11.231", "ip-10-0-11-231", "i-0abc123def4567890"):
                self.assertNotIn(leak, out)
        self.assertTrue(listed.startswith("1 nodes:\n<node-1>: Ready True, taints none, "
                                          "allocatable cpu 1930m"), listed)
        self.assertIn("aws:///us-east-1a/<instance-1>", detail)
        # the placeholder was read back as the real name, never shown to the model
        self.assertEqual(self.calls[-1], ("read_node", (NODE_NAME,)))

    def test_one_placeholder_mapping_across_tools(self):
        pods, _ = self.registry.run("list_pods", {})
        nodes, _ = self.run_tool(kind="nodes")
        self.assertIn("node <node-1>", pods)
        self.assertTrue(nodes.startswith("1 nodes:\n<node-1>:"))
        out, is_error = self.run_tool(kind="nodes", name="<node-7>")
        self.assertTrue(is_error)
        self.assertIn("list nodes first", out)

    def test_detail_drops_noise(self):
        self.run_tool(kind="nodes")
        out, _ = self.run_tool(kind="nodes", name="<node-1>")
        for noise in ("managedFields", "uid", "resourceVersion", "images", "machineID",
                      "systemUUID", "bootID", "lastHeartbeatTime", "annotations"):
            self.assertNotIn(noise, out)
        self.assertIn("allocatable", out)
        self.assertIn("kubeletVersion", out)

    def test_output_is_capped(self):
        big = {"metadata": {"name": "big"}, "data": {f"key{i}": "x" * 10000 for i in range(40)}}
        self.core.read_namespaced_config_map = lambda name, ns, **kw: _raw(big)
        self.core.list_namespaced_config_map = lambda ns, **kw: _raw(
            {"items": [{"metadata": {"name": f"cm{i:03d}"}, "data": {}} for i in range(200)]})
        tool = {t.name: t for t in k8s_tools.make_tools(self.kube)}["get_resource"]
        detail = tool.handler({"kind": "configmaps", "name": "big"})
        self.assertLessEqual(len(detail), k8s_tools.MAX_RESOURCE_CHARS + 60)
        self.assertIn("[truncated", detail)
        listed = tool.handler({"kind": "configmaps"})
        self.assertIn(f"... and {200 - k8s_tools.MAX_ITEMS} more", listed)

    def test_rbac_refusal_is_a_tool_error(self):
        def forbidden(*a, **kw):
            raise type("ApiException", (Exception,), {"status": 403})()
        self.core.list_node = forbidden
        out, is_error = self.run_tool(kind="nodes")
        self.assertTrue(is_error)
        self.assertIn("Not permitted to read nodes (403)", out)

    def test_replay_finds_the_recording_and_rejects_what_live_rejects(self):
        import os
        import tempfile
        import replay
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "svc.txt"), "w", encoding="utf-8") as f:
                f.write("recorded services")
            spec = {"repo_ref": "HEAD", "namespace": "online-boutique-dev",
                    "recorded": [{"tool": "get_resource", "input": {"kind": "services"}, "file": "svc.txt"}]}
            registry = replay.replay_registry(spec, d, 6000)
            self.assertEqual(registry.run("get_resource", {"kind": "Service",
                                                           "namespace": "online-boutique-dev"}),
                             ("recorded services", False))
            out, is_error = registry.run("get_resource", {"kind": "secrets"})
            self.assertTrue(is_error)
            self.assertIn("Secrets are never readable", out)


class ReadRepoFileTest(unittest.TestCase):
    """Uses the real chart values.yaml, which is longer than the output limit."""

    def setUp(self):
        import base64
        import os
        values = os.path.join(os.path.dirname(__file__), "..", "..", "helm",
                              "online-boutique", "values.yaml")
        with open(values, "rb") as f:
            self.text = f.read().decode("utf-8")
        content = base64.b64encode(self.text.encode()).decode()
        gh = NS(branch="main", values_file="v.yaml", chart_path="helm/online-boutique",
                get=lambda path, params=None: {"encoding": "base64", "content": content})
        self.read = {t.name: t for t in github_tools.make_tools(gh)}["read_repo_file"].handler

    def test_search_then_read_finds_a_block_past_the_truncation_point(self):
        line_no = self.text.splitlines().index("  emailservice:") + 1
        # The block's HPA settings lie past the 6000-character output limit
        hpa_offset = self.text.index("minReplicas", self.text.index("  emailservice:"))
        self.assertGreater(hpa_offset, 6000)

        hits = self.read({"path": "helm/online-boutique/values.yaml", "search": "  emailservice:"})
        self.assertIn(f"{line_no}:   emailservice:", hits)

        block = self.read({"path": "helm/online-boutique/values.yaml",
                           "start_line": line_no, "max_lines": 40})
        self.assertTrue(block.startswith(f"helm/online-boutique/values.yaml lines {line_no}-"))
        self.assertIn("replicas:", block)
        self.assertIn("minReplicas:", block)
        self.assertLess(len(block), 6000)

    def test_default_window_and_paging_hint(self):
        out = self.read({"path": "helm/online-boutique/values.yaml"})
        total = len(self.text.splitlines())
        first = out.splitlines()[0]
        self.assertEqual(first, f"helm/online-boutique/values.yaml lines 1-"
                                f"{github_tools.DEFAULT_FILE_LINES} of {total}; "
                                f"read on with start_line={github_tools.DEFAULT_FILE_LINES + 1}")

    def test_search_miss(self):
        self.assertIn("not found", self.read({"path": "x.yaml", "search": "nope-not-here"}))


class RecentChartCommitsTest(unittest.TestCase):
    def test_template_changes_and_deploys_both_show(self):
        def commit(sha, message):
            return {"sha": sha, "commit": {"message": message,
                                           "author": {"date": "2026-10-04T15:49:00Z", "name": "dev"}}}

        commits = [commit("20b4232a" + "0" * 32, "chore(deploy): v20261004-154933-68ac8238 [skip ci]"),
                   commit("96cf4f5a" + "0" * 32, "fix(chart): let the HPA own replicas")]
        details = {
            commits[0]["sha"]: {"files": [{"filename": "helm/online-boutique/values-dev.yaml",
                                           "status": "modified", "patch": '-  tag: "a"\n+  tag: "b"'}]},
            commits[1]["sha"]: {"files": [
                {"filename": "helm/online-boutique/templates/deployments.yaml", "status": "modified",
                 "patch": "-  replicas: {{ $svc.replicas }}\n" + "+x\n" * 2000},
                {"filename": "README.md", "status": "modified", "patch": "+docs"}]},
        }
        seen = []

        def get(path, params=None):
            seen.append((path, params))
            return commits if path == "/commits" else details[path.rsplit("/", 1)[-1]]

        gh = NS(branch="main", values_file="helm/online-boutique/values-dev.yaml",
                chart_path="helm/online-boutique", get=get)
        tool = {t.name: t for t in github_tools.make_tools(gh)}["recent_chart_commits"]

        out = tool.handler({"limit": 2})

        self.assertEqual(seen[0], ("/commits", {"path": "helm/online-boutique", "sha": "main",
                                                "per_page": 2}))
        self.assertIn("--- helm/online-boutique/values-dev.yaml (modified)", out)
        self.assertIn("--- helm/online-boutique/templates/deployments.yaml (modified)", out)
        self.assertIn("-  replicas: {{ $svc.replicas }}", out)
        self.assertIn("[patch cut,", out)
        self.assertNotIn("README.md", out)
        self.assertLess(out.index("chore(deploy)"), out.index("fix(chart)"))

    def test_chart_path_is_the_values_file_folder(self):
        gh = github_tools.GitHubReader("o/r", None, "main", "helm/online-boutique/values-dev.yaml")
        self.assertEqual(gh.chart_path, "helm/online-boutique")


class GitHubPathTest(unittest.TestCase):
    def test_safe_path(self):
        self.assertEqual(github_tools.safe_path("helm/online-boutique/values.yaml"),
                         "helm/online-boutique/values.yaml")
        for bad in ("../secrets", "/etc/passwd", "helm/../../x", "a//b", "a\\b"):
            with self.subTest(path=bad), self.assertRaises(ToolError):
                github_tools.safe_path(bad)


class PromptTest(unittest.TestCase):
    def test_system_prompts_render(self):
        for kind in ("investigate", "ask"):
            text = prompts.system_prompt(kind, "online-boutique-dev", 12)
            self.assertIn('namespace="online-boutique-dev"', text)
            self.assertIn("at most 12 tool calls", text)
            self.assertIn("readiness probe only removes the pod", text)
            self.assertIn("automated sync, self-heal and", text)
            self.assertIn("explain both halves", text)
            self.assertIn("first seconds after a container starts", text)
            self.assertNotIn("{", text.replace('{namespace="', ""))  # nothing left unformatted


class CurrentTimeTest(unittest.TestCase):
    def test_task_is_prefixed_with_utc_time(self):
        now = datetime(2026, 10, 4, 16, 1, 32, tzinfo=timezone.utc)
        self.assertEqual(prompts.with_current_time("Is it stable?", now=now),
                         "Current time: 2026-10-04T16:01:32Z (UTC)\n\nIs it stable?")

    def test_system_prompt_has_no_clock(self):
        # The time lives in the task so the system prompt stays cacheable
        self.assertNotIn("Current time", prompts.system_prompt("ask", "ns", 15))


class RefreshStartsAtTest(unittest.TestCase):
    def test_sets_recent_start_time(self):
        import cli
        payload = {"alerts": [{"startsAt": "2026-10-04T12:00:00Z"}, {"startsAt": "x"}]}
        now = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
        cli.refresh_starts_at(payload, 10, now=now)
        self.assertEqual([a["startsAt"] for a in payload["alerts"]],
                         ["2026-10-05T09:20:00Z"] * 2)


class ReportTest(unittest.TestCase):
    def test_report_keeps_non_ascii_and_lists_calls(self):
        import cli
        from agent import Evidence, Investigation
        result = Investigation("answered", "HPA → 1, then Argo CD → 2 — a cycle",
                               [Evidence("list_events", {"app": "emailservice"}, "...", False)], 3.2)
        report = cli.format_report(result, 15, header="ask: why?")
        self.assertTrue(report.startswith("ask: why?\n\nHPA → 1, then Argo CD → 2 — a cycle"))
        self.assertIn("Tool calls: 1 of 15 allowed", report)
        self.assertIn('[ok] list_events {"app": "emailservice"}', report)

    def test_report_shows_score_revision_and_note_and_is_redacted(self):
        import cli
        import confidence
        from agent import Evidence, Investigation
        ev = [Evidence("list_pods", {}, "x", False), Evidence("list_deployments", {}, "x", False)]
        tool = confidence.AssessmentTool(70, 85)
        tool.attach(lambda: ev)
        a = {"conclusion": "cause_found", "cause": "c", "cause_support": "observed",
             "cause_evidence": [1], "decisive_check": {"what": "limit 40Mi", "evidence": [2]},
             "alternatives": [{"cause": "a leak", "result": "not_ruled_out", "evidence": []}],
             "timing": "not_checked", "timing_evidence": [], "unverified": []}
        tool.handle(a)
        ev.append(Evidence("list_hpas", {}, "x", False))
        tool.handle(dict(a, cause_support="inferred"))
        result = Investigation("answered", "killed on ip-10-0-10-135.ec2.internal", ev, 2.0)
        report = cli.format_report(result, 15, assessment=tool,
                                   note=confidence.note_no_change(tool))
        self.assertIn("Evidence score: 40/100 (PRs need 70, image.tag rollbacks 85) - an "
                      "evidence score, not a probability", report)
        self.assertIn(" +30  Cause inferred: list_pods (call 1)", report)
        self.assertIn("Open questions (not scored):", report)
        self.assertIn("   ?  Not ruled out: a leak", report)
        self.assertIn("Revised: 55 (after 2 calls) -> 40 (after 3 calls)", report)
        self.assertIn("No change proposed: the cause is inferred, not directly observed", report)
        self.assertIn("killed on <node-1>", report)


class AlertTaskTest(unittest.TestCase):
    def test_only_firing_alerts_and_redacted(self):
        payload = {"alerts": [
            {"status": "firing", "labels": {"alertname": "PodCrashLooping", "severity": "critical",
                                            "pod": "cart-1"},
             "annotations": {"summary": "crash", "description": "token=abcdef123456"},
             "startsAt": "2026-10-04T12:00:00Z"},
            {"status": "resolved", "labels": {"alertname": "OldAlert"}}]}
        task = prompts.alert_task(payload)
        self.assertIn("Alert: PodCrashLooping (severity critical)", task)
        self.assertIn("pod=cart-1", task)
        self.assertNotIn("OldAlert", task)
        self.assertNotIn("abcdef123456", task)


if __name__ == "__main__":
    unittest.main()
