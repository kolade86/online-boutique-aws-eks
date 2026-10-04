# sreagent

An SRE assistant for Online Boutique. Given an alert or a question, it
investigates the live system with read-only tools and writes a diagnosis.

**Status: stage 3 of 4.** Done so far:

- the agent loop, the model provider and the read-only tools
- the HTTP server (`/alert`, `/ask`) with deduplication and limits
- the pull-request tool, with an allow-list enforced in code

All of it runs from your laptop. The in-cluster deployment comes in stage 4.

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
| `k8s_tools.py` | `list_pods`, `list_deployments`, `describe_deployment`, `list_hpas`, `list_events`, `get_pod_logs` |
| `github_tools.py` | `recent_chart_commits`, `read_repo_file` |
| `prompts.py` | System prompts; turns an AlertManager payload into a task |
| `config.py` | Environment variables |
| `cli.py` | Local runner |
| `server.py` | FastAPI app: `/alert`, `/ask`, `/investigations`, `/healthz` |
| `alerts.py` | Alert identity (alertname + namespace + service) and deduplication |
| `values_change.py` | Applies a proposed change to `values-dev.yaml` and validates it against the allow-list |
| `pr_tool.py` | `propose_values_change` (the one write tool) and opening the pull request |

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
python cli.py tool describe_deployment name=emailservice  # live replicas, applied manifest, ReplicaSets
python cli.py tool prometheus_query "query=sum by (pod) (kube_pod_container_status_restarts_total{namespace='online-boutique-dev'})"
python cli.py tool prometheus_query_range "query=sum(rate(container_cpu_usage_seconds_total{namespace='online-boutique-dev',container!=''}[5m]))" minutes=60
python cli.py tool get_pod_logs pod=<a pod name from list_pods> tail_lines=20
python cli.py tool read_repo_file path=helm/online-boutique/values.yaml "search=  emailservice:"
python cli.py tool read_repo_file path=helm/online-boutique/values.yaml start_line=261 max_lines=40
python cli.py tool recent_chart_commits limit=3
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

To save a run, use `-o FILE` (for example
`python cli.py -o regressions\my-run.txt ask "..."`), not `>`. Windows
PowerShell 5.1 redirection re-encodes the output and garbles characters such
as `→` and `—`. `-o` writes UTF-8 with the question, the model and the time
at the top.

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

## The HTTP server

```powershell
# Same environment as above, plus a shared token for callers:
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$env:SREAGENT_API_TOKEN = [Convert]::ToBase64String($b)
python server.py                                   # listens on :8080 (PORT)
```

| Endpoint | Auth | Behaviour |
|---|---|---|
| `GET /healthz` | none | Liveness/readiness probe |
| `POST /alert` | bearer | AlertManager webhook payload. Replies at once with one decision per alert key; the investigation runs in the background |
| `POST /ask` | bearer | `{"question": "..."}`. Runs synchronously and returns the answer. Never opens a PR |
| `GET /investigations` | bearer | The last 20 results (answer, outcome, tools called), newest first |

Every endpoint except `/healthz` needs `Authorization: Bearer <SREAGENT_API_TOKEN>`.
The server refuses to start without a token.

From a second terminal:

```powershell
$h = @{ Authorization = "Bearer $env:SREAGENT_API_TOKEN" }
Invoke-RestMethod -Method Post http://localhost:8080/ask -Headers $h -ContentType application/json `
  -Body (@{ question = "Are all deployments healthy?" } | ConvertTo-Json)
Invoke-RestMethod -Method Post http://localhost:8080/alert -Headers $h -ContentType application/json `
  -InFile my-alert.json
Invoke-RestMethod http://localhost:8080/investigations -Headers $h | ConvertTo-Json -Depth 5
```

`/alert` does not rewrite `startsAt` the way the CLI does. To post a saved
file, update its time first.

### Which alerts are investigated

Only alerts that are worth an investigation reach the agent. Those are the
chart's workload-health rules, in the app namespace, at `warning` or
`critical`: `PodCrashLooping`, `PodNotReady`, `ContainerOOMKilled`,
`HighCPUUsage`, `HighMemoryUsage`, `DeploymentReplicasMismatch` and
`HpaMaxedOut`.

The list is `INVESTIGATE_ALERTS` in [alerts.py](alerts.py), and it is
checked twice:

- The stage 4 AlertManager route uses `alerts.route_matchers()`.
- `/alert` skips anything else with a reason, so a routing mistake cannot
  make the agent investigate noise.

Left out on purpose:

- `TargetDown`, `KubeSchedulerDown` and `RedisDown`: they are about the
  monitoring set-up, and no allowed change can fix them.
- The gRPC/HTTP error-rate and latency rules: the services expose no
  metrics, so these rules can never fire.
- kube-prometheus-stack's duplicates of the chart's rules, such as
  `KubePodCrashLooping`.

[fixtures/live-alerts-2026-10-04.json](fixtures/live-alerts-2026-10-04.json)
holds every alert that fired on the first day Alertmanager ran, as webhook
payloads. The tests check that every one of them is skipped.

### Limits and deduplication

- **One investigation at a time**, shared by `/alert` and `/ask`. While one is
  running:
  - an alert is skipped with reason `another investigation is running`
  - `/ask` returns 409
- **Deduplication.** An alert's identity is alertname + namespace + service.
  The service is worked out from the `deployment`, `horizontalpodautoscaler`,
  `app` or `pod` label, then `grpc_service`, then `service`.
  - A replaced pod therefore counts as the same alert. The raw AlertManager
    fingerprint would not, because it includes the pod name.
  - A key investigated in the last `SREAGENT_DEDUP_MINUTES` (default 30) is
    skipped.
  - An alert skipped because the agent was busy is not marked, so it can be
    investigated later.
- **One key per payload.** AlertManager groups alerts by alertname. When a
  group covers several services, the first new one is investigated and the
  rest are skipped as busy.
- **Skips return 200, not an error.** Otherwise AlertManager would retry. Its
  `repeat_interval` (1h) re-sends alerts that are still firing.
- **Per-investigation limits:** `SREAGENT_MAX_TOOL_CALLS` and
  `SREAGENT_TIMEOUT_SECONDS`, as for the CLI.
- **State is in memory.** A restart forgets the dedup history and the recent
  results. That is acceptable with one replica; at worst an alert is
  investigated twice.
- **Never more than one open agent PR per alert** is enforced in stage 3. It
  checks open PRs on GitHub, so it survives restarts.

## The pull-request tool

The agent's only write is a pull request that changes
`helm/online-boutique/values-dev.yaml`. A human reviews and merges it, and
Argo CD deploys it. The agent never touches the cluster and never writes to
`main`.

**How it works.** There are two steps, so the model gets feedback and the PR
gets the whole story:

1. **During the investigation**, the model calls `propose_values_change` with
   `path`/`value` pairs and a reason.
   - The code applies them to the current `values-dev.yaml` on `main`. The
     round trip keeps every comment and the layout.
   - It then validates the result: the parsed difference between the old and
     new file.
   - If the change is rejected, the reasons go back to the model. If it is
     accepted, it is held, not written.
2. **After the investigation finishes** with a report, the code:
   - re-reads `main` and re-validates the change (main may have moved)
   - creates the branch `sre-agent/<investigation id>`
   - commits the file to that branch
   - opens a PR labelled `sre-agent`

   The PR body has the final report (the diagnosis), every tool call the loop
   recorded with an output excerpt (the evidence), and the exact diff.
   Alerts get the tool; `/ask` never does.

**The allow-list**, enforced in [values_change.py](values_change.py) on the
resulting diff, not on what the model claims:

| Setting | Rule |
|---|---|
| `image.tag` | Rollback only, to a tag deployed by one of the last 6 `values-dev.yaml` commits (ECR keeps only 10 images). Must be the only change in its PR |
| `services.<svc>.resources.(requests\|limits).(cpu\|memory)` | CPU 10m–2000m, memory 16Mi–4Gi, request ≤ limit |
| `services.<svc>.replicas` | Only for services **without** an HPA (the chart ignores it otherwise), 0–5 |
| `services.<svc>.hpa.(minReplicas\|maxReplicas)` | Only for services **with** an HPA, 1–10, min ≤ max |

Whether a service has an HPA is decided from the merged chart values
(`values.yaml` plus the new `values-dev.yaml`), the same way the templates
decide.

Also rejected:

- any other path
- a service that is not in the chart, or is disabled
- anything under `services.sreagent` (the agent never edits itself)
- removing an existing key
- more than 6 changes
- dropping a comment
- a file without exactly one `  tag:` line (build.yml's `update-manifest`
  rewrites that line with `sed`)

**One open PR per alert.**

- The alert key (alertname + namespace + service) is hidden in the PR body.
- A proposal for a key that already has an open `sre-agent/*` PR is rejected,
  with a link to that PR.
- This uses GitHub, not memory, so it survives restarts.

**Dry run by default.**

- With `SREAGENT_OPEN_PRS` off (the default), the server validates a proposal
  and records the diff in `/investigations` as `dry_run`, but writes nothing.
  Turn it on once you trust the output.
- The CLI's `investigate` only opens a PR with `--open-pr`.

**GitHub token for writes.** A fine-grained token on this repository only:

- **Contents: Read and write** (the branch and commit)
- **Pull requests: Read and write**
- **Metadata: Read**

Contents write is also enough to push to `main`, so the code refuses any
branch except `sre-agent/<8 hex chars>`. The backstop is a ruleset on `main`
that this token cannot bypass.

> **Until stage 4 changes build.yml**, merging an agent PR still triggers
> `Build Images`, because the workflow runs on pushes to `helm/**`. Its
> `update-manifest` job then writes a new tag over any image rollback. Don't
> merge a rollback PR before stage 4.

### Testing it live (no cluster needed)

`propose` runs the PR path without the model and without a cluster. It only
needs GitHub.

```powershell
cd src\sreagent
$env:GITHUB_REPO = "kolade86/online-boutique-aws-eks"
$env:APP_NAMESPACE = "online-boutique-dev"

# 1. Dry run. Reads main; prints the validated diff, the PR title and the body.
#    Works without a token on a public repo.
python cli.py propose services.emailservice.resources.limits.memory=256Mi

# 2. A rejection. Every broken rule is listed; exit code 1.
python cli.py propose services.emailservice.replicas=3 redis.addr=x:6379

# 3. Open a real PR (needs the write token above).
$env:GITHUB_TOKEN = Read-Host "GitHub token (Contents + Pull requests: write)"
python cli.py propose services.emailservice.resources.limits.memory=256Mi --open-pr

# 4. Run the same command again: rejected, because a PR for this key is open.
python cli.py propose services.emailservice.resources.limits.memory=256Mi --open-pr
```

Then check the PR on GitHub:

- the branch is `sre-agent/<id>`, never `main`
- the label is `sre-agent`
- the diff touches only `values-dev.yaml`, with its comments intact
- **Unit Tests** runs on it

Close the PR and delete the branch without merging.

With a cluster, `python cli.py investigate my-alert.json` runs the full flow.
It ends with the proposal, if the agent made one, as a dry run. Add
`--open-pr` to open the PR.

## Model choice

The default is **Claude Sonnet 5.5** (`claude-sonnet-5-5`), for both `/alert`
and `/ask`. Set `SREAGENT_MODEL` to change it.

**Why not Haiku 4.5.** It was the original default, and it failed the
[emailservice regression case](regressions/emailservice-pod-replaced.json)
in all three runs on 2026-10-04. That includes the run after the prompt and
tool fixes that let Sonnet 5.5 pass.

- Each time, Haiku stopped after two tool calls (pods and events) and filled
  the gaps with claims no tool result supported:
  - readiness probes as the cause
  - an HPA scale-up that no event shows
  - blame on a node
- It never checked the chart values, although the prompt told it to.
- On the same prompt, Sonnet 5.5 used six tool calls. It confirmed
  `replicas: 2` against `minReplicas: 1` in `values.yaml`, explained both
  halves of the cycle, and marked the Argo CD step as inferred.
- Sonnet 5.5 costs about twice as much per token, and used about three times
  the tool calls on this case. A wrong diagnosis that opens a PR costs more.

**Refusal fallback.** Sonnet 5.5's safety classifiers can decline a request
(`stop_reason: refusal`). Logs full of errors could plausibly trip the
`cyber` classifier by mistake.

- For Sonnet 5.5 and the Opus and Fable models, the provider sends
  `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). The API
  then retries `cyber` declines on Claude Sonnet 5.
- A decline that is not retried ends the run as `model_stopped`, with the
  category shown, for example `refusal:cyber`.
- Haiku and other models are called without the fallback.

## Configuration

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Required for `ask` / `investigate` |
| `GITHUB_REPO` | (none) | Required, as `owner/name` |
| `GITHUB_TOKEN` | (none) | Optional for reads; needed for private repos |
| `APP_NAMESPACE` | the pod's own namespace | Required outside the cluster |
| `PROMETHEUS_URL` | `http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090` | |
| `KUBE_CONTEXT` | current context | kubeconfig context to use locally |
| `SREAGENT_MODEL` | `claude-sonnet-5-5` | Used by `/alert`, `/ask` and the CLI. See [Model choice](#model-choice) |
| `SREAGENT_MAX_TOOL_CALLS` | `15` | Per investigation |
| `SREAGENT_TIMEOUT_SECONDS` | `300` | Per investigation |
| `SREAGENT_MODEL_TIMEOUT_SECONDS` | `120` | Per model request |
| `SREAGENT_MAX_TOKENS` | `16000` | Max output tokens per model turn. Includes Sonnet 5.5's thinking |
| `SREAGENT_TOOL_OUTPUT_MAX_CHARS` | `6000` | Each tool result is cut to this length |
| `SREAGENT_API_TOKEN` | (none) | Required by the server: bearer token for `/alert`, `/ask`, `/investigations` |
| `SREAGENT_DEDUP_MINUTES` | `30` | Skip an alert key investigated this recently |
| `SREAGENT_OPEN_PRS` | `false` | Server only: open the PRs the agent proposes (otherwise `dry_run`) |
| `PORT` | `8080` | Server port |
| `GITHUB_BRANCH` | `main` | |
| `VALUES_FILE` | `helm/online-boutique/values-dev.yaml` | |

## Regression cases

[regressions/](regressions/) records questions the agent once got wrong:

- the question
- the expected diagnosis and the pass criteria
- every run so far, with what was wrong in each
- the recorded tool output, for replaying the case once the cluster no
  longer shows the problem

[emailservice-pod-replaced.json](regressions/emailservice-pod-replaced.json)
is the first: the HPA and Argo CD self-heal were fighting over the replica
count.

- All three Haiku 4.5 runs failed. They blamed readiness probes and never
  found what scaled the Deployment back up.
- Sonnet 5.5 passed on the current prompt.
- The file includes a short model comparison, and the full fix: omit
  `spec.replicas` for HPA-managed services.

A correct answer explains both halves of the cycle: the HPA scale-down to 1,
and the scale-up back to 2 that restores the chart's value. It must not name
readiness probes as the root cause.

[emailservice-stable-after-fix.json](regressions/emailservice-stable-after-fix.json)
is the second: after the chart fix deployed, the agent said the cycle was
still going and predicted another scale-up.

- It read `replicas: 2` in `values.yaml` and could not see that the template
  now omits it.
- It counted the image rollout's new ReplicaSet as another round of the
  cycle.
- A correct answer says the cycle appears to have stopped, or that the
  evidence is not yet enough to tell. It must not say the cycle continues.
- The case motivated four additions: `describe_deployment`,
  `recent_chart_commits`, the rollout and no-prediction guidance, and the
  current time in the task.

These cases are checked by hand for now. A replay harness that feeds recorded
tool outputs to the model is possible later.

## Tests

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

The loop tests in `test_agent.py` use a scripted fake model provider, so they
make no network calls and need no packages installed. Two test files are
skipped when their packages are missing:

- `test_anthropic_provider.py` needs `anthropic`
- `test_server.py` needs `fastapi` and `httpx`
- `test_values_change.py` and `test_pr_tool.py` need `ruamel.yaml`. They run
  the allow-list and the whole PR flow against an in-memory GitHub, using the
  real chart files. Only the HTTP calls themselves are left untested.

`httpx` is used only by FastAPI's test client, so it is not in
`requirements.txt`. Run `pip install httpx` to run the server tests.
