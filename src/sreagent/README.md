# sreagent

An SRE assistant for Online Boutique. Given an alert or a question, it
investigates the live system with read-only tools and writes a diagnosis.

**Status: stage 1 of 4.** The agent loop, the model provider and the read-only
tools are done and run from your laptop. The HTTP endpoints (`/alert`,
`/ask`), the pull-request tool, and the in-cluster deployment come in later
stages.

## How it works

[agent.py](agent.py) holds the whole loop, in about 50 lines:

1. Send the task, the conversation so far, and the tool definitions to the model.
2. If the model replies without calling a tool, that reply is the answer.
3. Otherwise run each tool it asked for, send the results back, and go to 1.

Two limits stop a runaway investigation:

- **Tool-call limit** (`SREAGENT_MAX_TOOL_CALLS`, default 15). The system
  prompt tells the model its budget. Once the limit is reached, further calls
  are refused and the model gets one more turn to answer. Then the outcome is
  `answered_at_tool_limit`, not `answered`, so you can see it ran out. If it
  asks for tools again, the run stops with `tool_limit`.
- **Wall-clock timeout** (`SREAGENT_TIMEOUT_SECONDS`).

Every tool call is recorded as evidence.

| File | Purpose |
|---|---|
| `agent.py` | The loop and its limits |
| `model.py` | Provider-neutral message types and the `ModelProvider` interface |
| `anthropic_provider.py` | The only provider: the Anthropic API via the `anthropic` SDK |
| `tools.py` | Tool registry. Every output is redacted, then truncated, before the model sees it |
| `redact.py` | Secret redaction and truncation |
| `prometheus_tools.py` | `prometheus_query`, `prometheus_query_range` |
| `k8s_tools.py` | `list_pods`, `list_deployments`, `list_hpas`, `list_events`, `get_pod_logs` |
| `github_tools.py` | `recent_values_commits`, `read_repo_file` |
| `prompts.py` | System prompts; turns an AlertManager payload into a task |
| `config.py` | Environment variables |
| `cli.py` | Local runner |

To add a Bedrock provider later, write a class with the same `complete()`
method as `AnthropicProvider` and choose it in `wiring.py`. The loop does not
change.

## Running it on your laptop against the live cluster

The commands are for Windows PowerShell. Bash equivalents follow.

You need:

- Python 3.12
- `kubectl` with a kubeconfig context for the EKS cluster
- An Anthropic API key
- Optionally, a GitHub fine-grained token for this repository with
  **Contents: Read-only** and **Metadata: Read-only**. Without one, the
  GitHub tools still work against a public repo, but are limited to 60
  requests an hour.

**Terminal 1: forward Prometheus** (leave it running):

```powershell
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
```

**Terminal 2: install and configure** (from the repository root):

```powershell
cd src\sreagent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:APP_NAMESPACE  = "online-boutique-dev"
$env:PROMETHEUS_URL = "http://localhost:9090"
$env:GITHUB_REPO    = "kolade86/online-boutique-aws-eks"
$env:ANTHROPIC_API_KEY = Read-Host "Anthropic API key" -MaskInput
$env:GITHUB_TOKEN      = Read-Host "GitHub token (Enter to skip)" -MaskInput
```

`-MaskInput` needs PowerShell 7. On Windows PowerShell 5.1, leave it off;
the key will be visible while you type it, but it still won't be saved in
your command history.

**Check each tool on its own first.** This makes no model calls and costs
nothing:

```powershell
python cli.py tool                                   # list the tools
python cli.py tool list_pods
python cli.py tool list_pods app=cartservice
python cli.py tool list_deployments
python cli.py tool list_hpas
python cli.py tool list_events warnings_only=true
python cli.py tool list_events app=emailservice      # Deployment, ReplicaSets, pods and HPA
python cli.py tool prometheus_query "query=sum by (pod) (kube_pod_container_status_restarts_total{namespace='online-boutique-dev'})"
python cli.py tool prometheus_query_range "query=sum(rate(container_cpu_usage_seconds_total{namespace='online-boutique-dev',container!=''}[5m]))" minutes=60
python cli.py tool get_pod_logs pod=<a pod name from list_pods> tail_lines=20
python cli.py tool recent_values_commits limit=3
```

The `tool` command takes `key=value` arguments, not JSON, because Windows
PowerShell 5.1 strips the double quotes from JSON passed to native programs.
Use single quotes inside PromQL, as above.

**Ask a question** (answer only):

```powershell
python cli.py ask "Are all deployments healthy? Anything restarting?"
```

**Investigate an alert.** First copy
[examples/alert-podcrashlooping.json](examples/alert-podcrashlooping.json)
to `my-alert.json`, and replace `cartservice-REPLACE-ME` with a real pod
name. Files named `my-*` are git-ignored. Then:

```powershell
python cli.py -v investigate my-alert.json
```

The CLI rewrites each alert's `startsAt` to 10 minutes ago. Otherwise a saved
file's fixed start time would soon predate every pod. To change that, use
`--started-minutes-ago N`, or `--keep-starts-at` to use the file's values
unchanged.

`-v` logs each tool call as JSON. The output is the diagnosis, followed by the
outcome, the number of tool calls used out of the number allowed, the elapsed
time, and the list of calls made.

Bash equivalents:

```bash
cd src/sreagent && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export APP_NAMESPACE=online-boutique-dev PROMETHEUS_URL=http://localhost:9090 \
       GITHUB_REPO=kolade86/online-boutique-aws-eks
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
python cli.py ask "Are all deployments healthy?"
```

**Local runs use your own kubeconfig credentials.** These are probably
cluster-admin. The agent only ever makes get/list/log calls in
`APP_NAMESPACE`, but locally that is enforced by this code alone. In the
cluster, a namespaced read-only Role will enforce it too (stage 4).

## Configuration

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Required for `ask` / `investigate` |
| `GITHUB_REPO` | (none) | Required, as `owner/name` |
| `GITHUB_TOKEN` | (none) | Optional for reads; needed for private repos |
| `APP_NAMESPACE` | the pod's own namespace | Required outside the cluster |
| `PROMETHEUS_URL` | `http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090` | |
| `KUBE_CONTEXT` | current context | kubeconfig context to use locally |
| `SREAGENT_MODEL` | `claude-haiku-4-5-20251001` | |
| `SREAGENT_MAX_TOOL_CALLS` | `15` | Per investigation |
| `SREAGENT_TIMEOUT_SECONDS` | `300` | Per investigation |
| `SREAGENT_MODEL_TIMEOUT_SECONDS` | `120` | Per model request |
| `SREAGENT_MAX_TOKENS` | `8000` | Max output tokens per model turn |
| `SREAGENT_TOOL_OUTPUT_MAX_CHARS` | `6000` | Each tool result is cut to this length |
| `GITHUB_BRANCH` | `main` | |
| `VALUES_FILE` | `helm/online-boutique/values-dev.yaml` | |

## Regression cases

[regressions/](regressions/) records questions the agent once got wrong:

- the question
- the wrong answer
- the expected diagnosis
- pass criteria

[emailservice-pod-replaced.json](regressions/emailservice-pod-replaced.json)
is the first. The agent blamed a readiness probe when the HPA and Argo CD
self-heal were fighting over the replica count.

These cases are checked by hand for now. A replay harness that feeds recorded
tool outputs to the model is possible later.

## Tests

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

The loop tests in `test_agent.py` use a scripted fake model provider, so they
make no network calls and need no packages installed. `test_anthropic_provider.py`
is skipped when the `anthropic` package is not installed.
