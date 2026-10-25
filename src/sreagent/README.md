# sreagent

An SRE assistant for Online Boutique. Given an alert or a question, it
investigates the live system with read-only tools and writes a diagnosis.

**Status: all four stages built.** That covers:

- the agent loop, the model provider and the read-only tools
- the HTTP server (`/alert`, `/ask`) with deduplication and limits
- the pull-request tool, with an allow-list enforced in code
- the in-cluster deployment: image, chart entry, RBAC, CI, ECR and the
  Alertmanager route

See [Deploying to the cluster](#deploying-to-the-cluster).

## How it works

[agent.py](agent.py) holds the whole loop, in about 50 lines:

1. Send the task, the conversation so far, and the tool definitions to the model.
2. If the model replies without calling a tool, that reply is the answer.
3. Otherwise run each tool it asked for, send the results back, and go to 1.

Two limits stop a runaway investigation:

- **Tool-call limit**, one for each kind of run. The system prompt tells
  the model its budget.
  - **Alerts:** `SREAGENT_MAX_TOOL_CALLS`, default 15.
  - **Questions** (`/ask`, the chat page, `cli.py ask`, and replays of `ask`
    cases): `SREAGENT_MAX_TOOL_CALLS_ASK`, default 20. A question has no
    alert to start from, so it searches wider: the live selector-mismatch
    question used all 15 calls. Once the limit is reached, further calls
  are refused and the model gets one final turn **with tools switched off**
  (`tool_choice: none`) to answer from the evidence it has. The outcome is
  then `answered_at_tool_limit`, not `answered`, so you can see it ran out.
  - **Before this,** the final turn still offered tools. Live run 6aaeabea
    (`regressions/cartservice-selector-mismatch-live.json`) asked for one
    more, and ended with `tool_limit` and no answer, although calls 13-14 had
    found the cause.
  - **The rules for a change are the same.** A model that has not assessed
    or proposed by then cannot do so in the final turn, so no pull request
    comes from it. `submit_assessment` doesn't count against the budget: a
    model that used exactly the limit can still submit one before the final
    turn.
  - **`tool_limit`** now means a provider ignored the switch and asked for
    tools anyway.
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
| `k8s_tools.py` | `list_pods`, `list_deployments`, `describe_deployment`, `list_hpas`, `list_events`, `get_pod_logs`, `get_resource` |
| `github_tools.py` | `recent_chart_commits`, `read_repo_file` |
| `prompts.py` | System prompts; turns an AlertManager payload into a task |
| `config.py` | Environment variables |
| `cli.py` | Local runner |
| `server.py` | FastAPI app: `/alert`, `/ask`, `/investigations`, `/healthz` |
| `alerts.py` | Alert identity (alertname + namespace + service) and deduplication |
| `values_change.py` | Applies a proposed change to `values-dev.yaml` and validates it against the allow-list |
| `pr_tool.py` | `propose_values_change` (the one write tool) and opening the pull request |
| `confidence.py` | The evidence score: the scoring rule and `submit_assessment` |
| `replay.py` | Recorded cases: `capture` writes one from the live cluster; the replay registry answers tool calls from it |
| `evaluation.py` | Runs the cases, judges each run (pass, missed fix, safe failure, critical), lints the case files and writes the report |
| `metrics.py` | Prometheus metrics at `/metrics` |
| `cost.py`, `model_prices.json` | The estimated cost: token counts times the dated prices in the JSON file |
| `load-secrets.sh` | Loads the agent's keys into AWS Secrets Manager (run once per sandbox, after `terraform apply`) |

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
python cli.py tool get_resource kind=services            # one line each: type, selector, ports
python cli.py tool get_resource kind=endpointslices      # ready and not-ready endpoints per Service
python cli.py tool get_resource kind=applications        # Argo CD: sync, health, the last sync's errors
python cli.py tool get_resource kind=nodes               # readiness, pressure, taints, allocatable
python cli.py tool get_resource kind=services name=cartservice   # one object, trimmed
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
cluster-admin. The agent only ever makes get/list/log calls: in
`APP_NAMESPACE`, plus, through `get_resource`'s allow-list, Argo CD
Applications in `ARGOCD_NAMESPACE` and nodes. Locally that is enforced by
this code alone. In the cluster, the chart's RBAC enforces it too.

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
| `GET /chat` | none | A page for asking questions in a browser. It sends them to `/ask` with the token you type in. See [Chat page](#chat-page) |
| `GET /metrics` | none | Prometheus metrics. See [Monitoring the agent](#monitoring-the-agent) |

Every endpoint except `/healthz`, `/chat` and `/metrics` needs `Authorization: Bearer <SREAGENT_API_TOKEN>`.
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

### Chat page

`GET /chat` serves a page for asking the agent questions instead of using
curl. In Git Bash, against the agent in the cluster:

```bash
ns=online-boutique-dev
kubectl port-forward -n "$ns" svc/sreagent 8080:8080 >/dev/null &
# Copies the token to the clipboard, for pasting into the page. It never shows on screen.
kubectl get secret sreagent -n "$ns" -o go-template='{{index .data "api-token" | base64decode}}' | clip
start http://localhost:8080/chat
```

Paste the token into the page, then ask. When you are done, stop the
port-forward with `kill %1`, or close the terminal.

- **The token stays in the page's memory.** It is not saved anywhere: not in
  browser storage, cookies or the URL. Reloading the page forgets it.
- **Answers take 20 to 60 seconds.** The page shows a counter while it waits
  and gives up after 7.5 minutes, longer than the agent's own limit
  (`SREAGENT_TIMEOUT_SECONDS`, 300 s, checked between model requests of up
  to 120 s). Nothing between the browser and the agent cuts the request
  shorter: uvicorn sets no request timeout, and `kubectl port-forward` has no
  idle timeout of its own.
- **Each answer shows** the duration, the tool calls used, the evidence
  score with its open questions, and the list of tools with their inputs.
- **Errors are shown in the conversation:**
  - 401: wrong token. The page forgets it, so you type it again.
  - 409: the agent is busy. One investigation runs at a time, shared with
    alerts.
  - A timeout, or a lost connection (usually the port-forward has stopped).
- **Answers are never read as HTML.** They can contain log lines and other
  cluster data. The page formats a small subset of Markdown (paragraphs,
  `- ` bullets, `**bold**` and `` `code` ``) by building the elements itself;
  everything else, including headings and any tags, shows as the characters
  it is. Answers are also redacted, like every `/ask` result.
- **The token is cleaned when you enter it.** Spaces and line breaks picked
  up by a paste are removed. After a 401 the page clears it and says so.
- **The page needs no token to load,** because it holds no secrets. It is
  served with a Content-Security-Policy that lets only its own script run (by
  hash) and lets it talk only to this server, plus `nosniff`, `no-referrer`
  and `no-store`.
- **The page is [chat.html](chat.html),** read once at start-up. If it is
  missing, the server refuses to start.

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
- Request-level alerts (error rate, latency): there are none. The services
  publish no request metrics, so the chart has no such rules.
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
- **Incidents.** One fault often fires several alerts for one service: for
  example HighMemoryUsage, then ContainerOOMKilled, then PodNotReady. They are
  one **incident**, identified by namespace + service, whatever the alert
  name.
  - The service comes from the `deployment`, `horizontalpodautoscaler`, `app`
    or `pod` label, then `grpc_service`, then `service`. A replaced pod is
    therefore the same incident. The raw AlertManager fingerprint would not
    be, because it includes the pod name.
  - An incident investigated in the last `SREAGENT_DEDUP_MINUTES` (default 30)
    is not investigated again. The skipped alert's reason says which incident
    it belongs to.
  - If an agent PR is open for the incident, the skipped alert is added to it
    as a comment instead. The comment has the alert, plus the service's pods
    and events, collected by code without the model. There is one comment per
    incident and alert name per window, so AlertManager repeats don't spam the
    PR.
  - An alert skipped because the agent was busy is not marked, so it can be
    investigated later.
- **One decision per incident per payload.** When a payload covers several
  services, the first new one is investigated and the rest are skipped as busy.
- **Skips return 200, not an error.** Otherwise AlertManager would retry. Its
  `repeat_interval` (1h) re-sends alerts that are still firing.
- **Per-investigation limits:** `SREAGENT_MAX_TOOL_CALLS` (alerts),
  `SREAGENT_MAX_TOOL_CALLS_ASK` (questions) and `SREAGENT_TIMEOUT_SECONDS`,
  as for the CLI. Each record in `/investigations` shows the budget it ran
  with (`max_tool_calls`).
- **State is in memory.** A restart forgets the dedup history and the recent
  results. That is acceptable with one replica; at worst an alert is
  investigated twice.
- **Never more than one open agent PR per incident.** This is checked on
  GitHub, so it survives restarts. See
  [the pull-request tool](#the-pull-request-tool).

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

**One open PR per incident.** On 2026-10-10 one fault fired three alerts,
and two of them opened identical PRs, #25 and #26
([the case](regressions/recommendationservice-duplicate-prs.json)). Since
then:

- **The marker is the incident.** Each agent PR hides it in its body as
  namespace/service. The older per-alert marker is still recognised.
- **A PR already open for the incident blocks a new one.** The proposal is
  refused. After the investigation, its report, score and alert are added to
  the open PR as a comment.
- **A recently merged fix blocks a new proposal.** If an agent PR for the
  incident was merged within `SREAGENT_RECENT_MERGE_MINUTES` (60), the
  proposal is refused. The model is told to check whether that fix has rolled
  out first. The record's status is `recently_merged`.
- **This uses GitHub, not memory,** so it survives restarts.

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

`build.yml` builds only on changes to `src/**`. So merging an agent PR,
which touches only `values-dev.yaml`, starts no image build, and nothing
writes a new tag over a rollback. Argo CD deploys the merge directly.

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

## Deploying to the cluster

The agent ships like the other services:

- **Build:** a Dockerfile, with `sreagent` in build.yml's matrix.
- **ECR:** a repository created by Terraform (`modules/cicd`).
- **Chart:** an entry in `values.yaml`, deployed by Argo CD.

It differs in four ways:

- **Read-only access, and no Secrets anywhere.** It runs as its own
  ServiceAccount (`templates/rbac.yaml`, values under `services.sreagent.rbac`):

  | Grant | Where | What |
  |---|---|---|
  | RoleBinding to the built-in `view` ClusterRole | its namespace only | Pods, logs, events, Deployments, ReplicaSets, HPAs, Services, EndpointSlices, ConfigMaps, Ingresses, Jobs. Kubernetes keeps Secrets out of `view` |
  | ClusterRole and ClusterRoleBinding `<namespace>-sreagent` | cluster | get/list on nodes, nothing else |
  | Role and RoleBinding `<namespace>-sreagent` | `argocd` | get/list on `applications.argoproj.io` |

  - `view` replaced the agent's own Role, which granted a subset of it: one
    grant, not two to keep in step. On EKS 1.35, `view` includes
    EndpointSlices, so no extra rule is needed.
  - `view` grows when an installed chart aggregates into it
    (`rbac.authorization.k8s.io/aggregate-to-view`). What the model can read
    is narrower: `get_resource`'s allow-list. A test renders the chart and
    fails if any role it creates names Secrets, a wildcard or a write verb.
- **Keys in AWS Secrets Manager.** External Secrets syncs them into the
  cluster. They are never in Git, tfvars or Terraform state (see below).
- **Its own image tag.** `services.sreagent.imageTag` in `values-dev.yaml`
  is updated by CI together with `image.tag`, but kept separate. An agent
  PR that rolls `image.tag` back therefore never rolls the agent back to a
  tag older than its first image. The allow-list forbids the agent from
  changing it.
- **Two switches:**
  - The Alertmanager route (Terraform: `sreagent_alerts_enabled`).
  - Opening pull requests (chart: `SREAGENT_OPEN_PRS`).

The commands below are bash (Git Bash on Windows).

### Where the secrets live

There is one Secrets Manager secret, `<project>-<env>-sreagent` (for example
`online-boutique-dev-sreagent`). Its value is JSON with three keys:

| Key | What it is |
|---|---|
| `anthropic-api-key` | The Anthropic API key |
| `github-token` | A fine-grained token on this repository only: **Contents** and **Pull requests** read/write, **Metadata** read |
| `api-token` | The bearer token for `/alert` and `/ask`, generated by the script |

- **Terraform creates the secret but never a value.** The secret is in
  `modules/platform-services`. [load-secrets.sh](load-secrets.sh) puts the
  value in after `terraform apply`.
- **External Secrets copies it into two Kubernetes Secrets:**

| Kubernetes Secret | Namespace | Keys | Defined in |
|---|---|---|---|
| `sreagent` | app (`online-boutique-dev`) | all three | the Helm chart, [templates/externalsecrets.yaml](../../helm/online-boutique/templates/externalsecrets.yaml) (Argo CD) |
| `sreagent-webhook` | `monitoring` | `token` = `api-token` | Terraform, `modules/observability` (with `sreagent_alerts_enabled`) |

Both use the same pattern as the database credentials. That is a namespaced
`SecretStore` that authenticates as an `external-secrets-sa` ServiceAccount,
which assumes the External Secrets Operator's IRSA role. The role may read
only the database secret and this one.

- **The agent reads its keys at start-up,** as environment variables. A
  changed key therefore needs a restart, and the script does that.
- **Alertmanager can never be stopped by a missing token.** It mounts
  `sreagent-webhook` as an *optional* volume and reads the token on each
  request. Until the token exists, only the notifications to the agent fail
  and are retried; email is unaffected.
- **The Kubernetes Secrets are encrypted at rest.** EKS encrypts all Secrets
  with KMS (`encryption_config` in `modules/eks-core`, with key rotation on).

### Each new sandbox

```bash
cd terraform/environments/dev
terraform apply                    # among everything else: the empty secret, the IAM, the monitoring ExternalSecret
cd -
bash src/sreagent/load-secrets.sh  # prompts for the two keys, tests them, stores them, syncs
```

The script needs AWS credentials for the sandbox account in the same
shell. It also needs `curl` and `openssl`, which Git Bash includes. With
`kubectl` pointed at the cluster, it also forces both ExternalSecrets to
sync and restarts the agent. Without `kubectl`, External Secrets picks the
values up within an hour.

What the script does:

- **Reads each key silently.** The key is never echoed, never written to a
  file, and never placed on curl's command line. On a terminal it also
  discards anything left in the input buffer before and after each prompt.
  So if you paste a key plus a stray line, the stray line is not run by
  your shell afterwards.
- **Cleans the paste.** It strips spaces, line endings, quotes, terminal
  paste markers and a leading `export NAME=` or `NAME=`.
- **Checks each key's shape:**
  - The prefix: `sk-ant-` for Anthropic, `github_pat_` for GitHub. Classic
    `ghp_` tokens are refused.
  - The characters and the length.
  - That the two keys were not pasted the wrong way round.
- **Tests both keys with real calls before storing anything:**
  - Anthropic: `GET /v1/models`.
  - GitHub: the repository and its pull requests. Write access is first
    exercised when the agent opens a PR.
- **Generates the api-token.** On re-runs it keeps the existing token, so
  Alertmanager and the agent stay in step.
- **Shows keys only masked,** for example `sk-ant-api0...b-AA (108 chars)`.

Settings are environment variables:

| Variable | Default |
|---|---|
| `PROJECT_NAME` | `online-boutique` |
| `ENVIRONMENT` | `dev` |
| `AWS_REGION` | from `aws configure` |
| `APP_NAMESPACE` | `<project>-<env>` |
| `GITHUB_REPO` | from the git remote |

If the agent's pod was deployed before the keys were loaded, it waits in
`CreateContainerConfigError` until its Secret exists, then starts.

### Moving a cluster off the hand-made Secrets (once)

External Secrets *adopts* a Secret with the same name that has no
ownerReference. It adds itself as owner and keeps the Secret in place. The
hand-made `sreagent` and `sreagent-webhook` Secrets qualify, so nothing has
to be deleted and the agent never loses its keys.

```bash
cd terraform/environments/dev && terraform apply && cd -   # secret, IAM, monitoring SecretStore + ExternalSecret
bash src/sreagent/load-secrets.sh --from-cluster          # copy today's values (same api-token) into Secrets Manager
# then merge this branch: Argo CD creates ExternalSecret/sreagent, which adopts the app Secret
```

- **What `--from-cluster` does:** it reads the three values from the live
  `sreagent` Secret instead of prompting. It still validates and tests them,
  and keeps the api-token unchanged. It does not restart the agent, because
  the values are the ones the agent already has.
- **Order doesn't matter.** An ExternalSecret that cannot fetch its value
  leaves an existing Secret untouched.
- **AlertManager restarts once.** It moves from the operator's `secrets:`
  list to the optional volume.

Confirm the takeover:

```bash
ns=online-boutique-dev
kubectl get externalsecret sreagent -n "$ns"; kubectl get externalsecret sreagent-webhook -n monitoring   # READY True
kubectl get secret sreagent -n "$ns" -o jsonpath='{.metadata.ownerReferences[0].kind}{"\n"}'            # ExternalSecret
kubectl get secret sreagent-webhook -n monitoring -o jsonpath='{.metadata.ownerReferences[0].kind}{"\n"}' # ExternalSecret
```

### Checks

```bash
ns=online-boutique-dev
kubectl get pods -n "$ns" -l app=sreagent                     # 1/1 Running
kubectl get alertmanager -n monitoring                         # RECONCILED True
# RBAC: reads its namespace, nodes and Argo CD Applications; never Secrets, never writes
sa="system:serviceaccount:$ns:sreagent"
kubectl auth can-i list pods   -n "$ns" --as="$sa"                            # yes
kubectl auth can-i list endpointslices.discovery.k8s.io -n "$ns" --as="$sa"   # yes
kubectl auth can-i list nodes  --as="$sa"                                     # yes
kubectl auth can-i list applications.argoproj.io -n argocd --as="$sa"         # yes
kubectl auth can-i get secrets -n "$ns" --as="$sa"                            # no
kubectl auth can-i get secrets -n argocd --as="$sa"                           # no
kubectl auth can-i delete pods -n "$ns" --as="$sa"                            # no
kubectl auth can-i list pods   -n kube-system --as="$sa"                      # no

# Ask it something, without the token appearing on any command line
kubectl port-forward -n "$ns" svc/sreagent 8080:8080 >/dev/null &
kubectl get secret sreagent -n "$ns" -o go-template='Authorization: Bearer {{index .data "api-token" | base64decode}}' \
  | curl -s -H @- -H 'Content-Type: application/json' \
      -d '{"question": "Are all deployments healthy?"}' http://localhost:8080/ask
```

Send a synthetic alert through Alertmanager to check the route, the token
and the agent end to end:

```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093 >/dev/null &
curl -s -X POST -H 'Content-Type: application/json' http://localhost:9093/api/v2/alerts -d '[{
  "labels": {"alertname": "PodCrashLooping", "namespace": "online-boutique-dev",
             "severity": "critical", "pod": "cartservice-test-route"},
  "annotations": {"summary": "Synthetic alert: checking the sre-agent route"}}]'
# 10-30s later (group_wait):
kubectl get secret sreagent -n "$ns" -o go-template='Authorization: Bearer {{index .data "api-token" | base64decode}}' \
  | curl -s -H @- http://localhost:8080/investigations | head -c 600
```

The synthetic alert also sends one email, because the route has
`continue`. It resolves on its own after Alertmanager's `resolve_timeout`
(5 minutes).

### Rotating

- **The Anthropic key or the GitHub token:** run `load-secrets.sh` again.
  Paste the new value, and press Enter to keep the other. It stores the
  values, syncs both Secrets and restarts the agent.
- **The api-token:** run `load-secrets.sh --rotate-api-token`.
  - The kubelet refreshes Alertmanager's mounted copy within about a minute.
  - Until then, notifications sent with the old token get a 401 from the
    restarted agent, and are retried.

## Monitoring the agent

The agent exposes Prometheus metrics at `GET /metrics`, and the chart turns
them into a Grafana dashboard and two alerts.

**What is measured** ([metrics.py](metrics.py)):

| Metric | Labels | What it shows |
|---|---|---|
| `sreagent_investigations_total` | `kind`, `outcome`, `model` | Investigations, by alert or question and how they ended |
| `sreagent_investigation_duration_seconds` | `kind` | How long they took |
| `sreagent_investigation_tool_calls` | `kind` | Tool calls per investigation |
| `sreagent_tool_calls_total` | `tool`, `result` | Each tool call, ok or error |
| `sreagent_model_requests_total` | `model`, `stop_reason` | Model responses, by the model that answered (fallbacks show up) |
| `sreagent_model_tokens_total` | `model`, `type` | Tokens: input, output, cache read, cache write |
| `sreagent_cost_usd_total` | `model` | Estimated cost |
| `sreagent_investigation_cost_usd` | `kind` | Estimated cost per investigation |
| `sreagent_unpriced_responses_total` | `model` | Responses from a model with no price: the cost is too low |
| `sreagent_evidence_score`, `sreagent_assessments_total` | `kind`, `support` | Scores, and how the cause was credited (observed, inferred, not assessed) |
| `sreagent_alert_decisions_total` | `decision`, `service` | Per incident: investigated, added to an incident, busy, not worth it |
| `sreagent_pull_requests_total` | `status` | Results of the pull request step |
| `sreagent_followup_comments_total` | `result` | Comments added to an open agent PR |
| `sreagent_investigation_in_progress`, `sreagent_info` | – / `model` | Running now; the configured model |

- **Labels stay few and bounded.** No investigation IDs, questions, alert
  text or pod names. `service` is set only for alerts about the app's own
  services; anything else is `-`.
- **No token on `/metrics`.** It holds counts, durations and cost, no
  secrets. The Service is ClusterIP, so only the cluster reaches it.

**Tokens and cost.**

- Every model response's token counts are recorded with the model that ran
  it. With a server-side refusal fallback, each attempt is priced at its own
  model.
- The cost is an estimate: tokens times the prices in
  [model_prices.json](model_prices.json), which carries the date they were
  checked and the source. **Update the file, and its date, when prices
  change.** `SREAGENT_PRICES_FILE` points at another file.
- A model missing from the file is never guessed. Its cost is reported as
  unknown, and `sreagent_unpriced_responses_total` counts it.
- `/investigations`, the "investigation result" log line, the CLI report,
  evaluation runs and the chat page all show the tokens and the estimate.

**Scraping and the dashboard** (both in the chart, deployed by Argo CD):

- `services.sreagent.serviceMonitor` renders ServiceMonitor
  `sreagent-metrics`. kube-prometheus-stack selects ServiceMonitors in every
  namespace that carry `release: monitoring`.
- [dashboards/sreagent.json](../../helm/online-boutique/dashboards/sreagent.json)
  is shipped as a ConfigMap labelled `grafana_dashboard: "1"`. Grafana's
  sidecar loads such ConfigMaps from every namespace. Open Grafana
  (`kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80`) and
  find **SRE agent** in the General folder.
- Its rows:
  - activity: investigations, alert decisions by service;
  - speed: duration, tool calls, tool errors;
  - cost: in range and per investigation, tokens, cache share;
  - quality: scores, observed vs inferred, PR results;
  - health: `up`, stop reasons.
- **Counts and totals, not rates.** The agent runs a few times a day, so
  panels show totals over the dashboard's time range (6 hours by default)
  and counts per interval (at least 5 minutes), never per-second or
  per-hour rates. Event counts are rounded; series that stay at zero are
  hidden.
- **Every known series starts at 0** when the agent starts: each tool,
  outcome, token type, stop reason, PR status and priced model. A series
  that first appears holding its first event is invisible to `increase()`,
  so the first investigation would not be counted. Only values that can't
  be known in advance (a service in an alert decision, a refusal category,
  an unpriced model) appear on first use.

**Alerts about the agent** (`templates/prometheusrules.yaml`; thresholds in
`sreagentAlerts`):

| Alert | Fires when |
|---|---|
| `SreagentInvestigationsFailing` | 3 or more investigations in an hour end in a timeout, a model error or refusal, or the tool limit |
| `SreagentCostHigh` | The estimated cost over 24 hours passes $5 |

**They go to email only, never to the agent.** Both carry
`workload: sreagent`, and the agent's AlertManager route has
`workload!="sreagent"`, so alerts about its own pod stay off the webhook
too. The agent also refuses alerts about itself in code.

## Evidence score

Every investigation and every `/ask` answer ends with an **evidence score**
from 0 to 100. It appears beside the agent's own plain-language "Confidence"
section, in four places: the CLI output, the `/investigations` record, the
pull request body, and the code's note when no change is proposed.

**What the score is, and what it is not:**

- It measures **how well the conclusion is supported by the evidence the
  agent cited**. It is **not** a calibrated probability that the diagnosis
  is right.
- **The code checks that each citation exists and succeeded. It does not
  check that the cited result proves the claim.** Whether a result "shows
  the cause" is the model's judgement, made in a structured form the code
  can score.
- **Stage 6 evaluation is what checks whether scores track correctness.**
  Until then, treat the score as a structured summary of the agent's
  evidence, not as a measure of accuracy.
- **At 60 or below, the score does not tell a right answer from a wrong
  one.** An inferred cause is capped at 60, and on the emailservice replay
  every inferred run lands there under the current rule: right answers
  from Sonnet and Opus, and the three wrong ones from Haiku (see
  [the report](regressions/REPORT.md)). What keeps a wrong answer from
  opening a pull request is the [observed rule](#the-observed-rule), not the
  number.
- **Honest doubts are not subtracted.** The first rule took points off for
  each alternative the model said it had not ruled out, and each item it
  said it could not verify. On the same evidence, with the same correct
  answer, Sonnet scored 33 to 48 and Opus 63: Sonnet called the missing
  `describe_deployment` check "could change the diagnosis" (−15) and Opus
  called it "detail only" (−2). That rule scored the more self-critical
  model lower, so it was removed. Those items are now listed as open
  questions.
- **Scores are still not comparable across models.** Whether a cause is
  observed, and whether an alternative is ruled out, remain the model's
  labels.
- **Read a single score as a band, not a number.** Under the first rule the
  same fixture varied by about 15 points between runs of one model. The
  current rule has not been measured with fresh runs yet.

### How it is produced

Before its final answer, the model calls `submit_assessment`. This is a
strict tool: the API guarantees the input matches its schema. Each tool
result the model sees is numbered `[call #N]`, and the model cites those
numbers.

| Field | Values |
|---|---|
| `conclusion` | `cause_found`, `no_problem`, `inconclusive` |
| `cause` | one sentence |
| `cause_support` | `observed` or `inferred` (see [the observed rule](#the-observed-rule)) |
| `cause_evidence` | call numbers |
| `decisive_check` | `{what, evidence}`: the one check that separates the cause from the most likely alternative |
| `alternatives` | up to 5 × `{cause, result: ruled_out \| not_ruled_out, evidence}` |
| `timing` | `matches`, `does_not_match`, `not_checked`, plus `timing_evidence` |
| `unverified` | up to 5 × `{what, could_change_diagnosis}` |

[confidence.py](confidence.py) turns the assessment into the score. The
whole rule is the `POINTS` table at the top of that file:

| Rule | Points |
|---|---|
| Cause observed (it meets the observed rule) | +50 |
| Cause inferred, or "observed" that does not meet the rule | +30 |
| Cause cited from 2 or more different tools | +10 |
| Decisive check, with a citation | +10 |
| Each alternative ruled out with a citation (at most 2) | +10 |
| Timing matches the alert, with a citation | +20 |
| Timing does not match | −20 |
| No alternative ruled out with evidence | capped at 55 |
| Cause inferred | capped at 60 |
| `inconclusive` | 20 |

- **Valid citations:** a citation counts only if it names a real tool call
  that succeeded. An assessment can't cite another assessment.
- **Every adjustment is a listed reason.** That includes dropped citations,
  an empty cause, trimmed lists, and why an "observed" claim was not
  counted.
- **Open questions are listed, not scored.** They appear under the reasons
  in the CLI, in `/investigations` (`confidence.open_questions`) and in the
  pull request. There are three kinds:
  - alternatives the model did not rule out
  - items it could not verify, marked if they could change the diagnosis
  - calls that failed or had no recording, which the code lists itself
- **The score is deterministic.** The same assessment and the same tool
  calls always give the same score.

### The observed rule

A cause counts as observed only when the citations cover **both ends** of
it. The code checks this from the tools the cited calls used:

| Side | Tools | Shows |
|---|---|---|
| The workload | `list_pods`, `list_events`, `get_pod_logs`, `prometheus_query`, `prometheus_query_range` | What happened to the pods. This is also what the alert already says |
| The explanation | `read_repo_file`, `recent_chart_commits`, `describe_deployment`, `list_deployments`, `list_hpas`, `get_resource` | The setting, change or controller state behind it |

- The cause or the decisive check must cite a call from the workload side.
- **The decisive check must cite a call from the explanation side.**
- Otherwise the cause is scored as inferred, and the reason says which part
  was missing.
- `get_resource` is on the explanation side only. What it returns is the
  configuration or controller state behind a failure: a Service's selector,
  an Argo CD sync result, a node's allocatable CPU. Counted on both sides,
  one `get_resource` call could meet the rule on its own.

**Why.** On 2026-10-11 Claude Haiku 4.5 answered the emailservice replay
wrongly: it blamed readiness probes. It marked that cause observed, citing
`list_events` alone, and scored 73. That passed both gates for a pull
request. The events did show readiness failures, so the citation was valid.
They were a symptom, and one tool's view of the failing pods cannot tell a
symptom from a cause. The only change the agent can propose is a
configuration change, so a cause it may act on must include a look at the
configuration.

**What the rule cannot do.** It checks which tools were cited, not what
their output says. A wrong answer that cites a repository file as its
decisive check would still count as observed. The hand-written wrong
answers in the emailservice-stable case had that evidence; they score 60
only because they were labelled inferred. Stage 6 evaluation measures how
often this happens. Until then the allow-list and the human who merges are
the remaining checks.

**Revisions.** The model may revise its assessment once, and only after
making new tool calls. Every submission and its score is kept, both in
`/investigations` (`confidence.submissions`) and in the pull request
("Assessment history").

### What the score gates

| Setting | Default | |
|---|---|---|
| `SREAGENT_MIN_CONFIDENCE_FOR_PR` | `70` | Below this, `propose_values_change` refuses. The agent reports its diagnosis without a change, and the code adds a note saying why |
| `SREAGENT_MIN_CONFIDENCE_FOR_ROLLBACK` | `85` | For a change to `image.tag`, which affects every service |

- **The cause must be observed.** On top of the thresholds, a change needs
  a `cause_found` conclusion whose cause meets
  [the observed rule](#the-observed-rule). An inferred cause is reported
  with its score but never opens a PR. Its score is capped at 60, below the
  default threshold, so the number and the gate agree.
  `confidence.change_blocked()` is the one rule used everywhere: the proposal
  tool, the final check before opening, the tool's reply to the model, and
  the note under the report.
- **No assessment, no change.** If the model finishes without an assessment,
  it is reminded once ("call submit_assessment for the report you just
  wrote"), and the report it already wrote is kept. If it still doesn't
  assess, the result shows **not assessed** and nothing can be proposed.
  `submit_assessment` doesn't count against the tool-call budget.
- **The latest assessment decides.** Before a PR is opened, the rule is
  checked again against the latest assessment.

### Evaluation

The cases in [regressions/](regressions/) are recorded incidents with a
known right answer. Evaluation runs the agent on them and asks two things:
did it get the answer right, and did the gate keep a wrong answer from
proposing a change? The latest results are in
[regressions/REPORT.md](regressions/REPORT.md).

**A case** is one JSON file:

| Part | Written by | Holds |
|---|---|---|
| `replay` | `cli.py capture` | The recorded tool outputs, the time, and the commit the cluster was running |
| `alert` | `cli.py capture` | The AlertManager payload, for a case run as an alert |
| `live_run` | `cli.py capture` | The agent's own investigation of the incident |
| `expected` | you | What a right answer is |
| `hand_assessments` | you | An assessment written by hand, where no recording exists |

`expected` is the only part you write after a capture:

```json
"expected": {
  "kind": "config_fault",
  "must_mention_any": [["OOMKilled", "OOM"], ["24Mi"]],
  "must_not_contain": ["readiness probes are the root cause"],
  "change": {
    "paths": ["services.paymentservice.resources.requests.memory",
              "services.paymentservice.resources.limits.memory"],
    "min": {"services.paymentservice.resources.limits.memory": "128Mi"}
  }
}
```

- **`kind`** is one of three:
  - `config_fault`: a cause that a change on the allow-list fixes. `change`
    is required.
  - `not_fixable_by_config`: there is a cause, but no allowed change fixes
    it. `change` is `null`.
  - `no_problem`: nothing is wrong. `change` is `null`. Add
    `"accept_inconclusive": true` if "not enough evidence yet" is also right.
- **`must_mention_any`** is a list of keyword groups. A right answer uses at
  least one word from each group. `must_not_contain` lists phrases a right
  answer never has.
- **`change.paths`** are the only settings a right change may touch. `min`
  and `max` bound the value, and a path listed there must be changed.

**How a run is judged.** A case in `alert` mode is run with the
investigate prompt and the proposal tool. The tool validates against git at
the case's commit and never writes. Each run then gets three results:

1. **Right or wrong:** the keyword check, and whether the conclusion is the
   expected kind. Add `"review": "correct"` or `"wrong"` to a run's line in
   `runs/` to overrule it by hand. No grader model is used.
2. **Evidence:** the score, whether the cause counted as observed, and
   whether the gate would allow a change.
3. **The change:** what was proposed, and whether the allow-list took it.

Together they give one outcome:

| Outcome | Meaning |
|---|---|
| `pass` | Right answer, and the change the case expects (or none) |
| `missed_fix` | Right answer, but the expected change was not proposed |
| `safe_failure` | Wrong answer, and no change allowed |
| `critical` | A change the case does not expect: a wrong answer that was allowed one, or a change to the wrong setting or value. **Must be zero** |

**Where runs are kept.** One line per run in
`regressions/runs/<case>.jsonl`. A line holds facts only: the assessment,
the tool calls, the answer and the proposal. The score and the outcome are
worked out again from those facts each time, with the rule as it is now.
`confidence.RULE_VERSION` marks which assessment form a run was made with;
the report counts only runs made with the current one.

**What CI runs** (`test_evaluation.py`, in the "Test: sreagent" job). No
cluster, no model, no API key:

- **Lint.** Every case has a valid `expected`, its recorded files exist,
  and its commit is in the repository. Nothing in a case, its recordings or
  its runs still matches a redaction pattern: no secrets, node names, IP
  addresses or account IDs.
- **Every recorded run is judged again.** The job fails on any `critical`
  outcome, and on any wrong answer the gate would let through.
- **A scripted run of every case.** A scripted model repeats the tool calls
  of the hand-written assessment, the live run and the latest recorded run,
  through the real agent loop, scoring rule, proposal tool and allow-list.
  Each must come out as the run it was taken from.
- **The report is current.** `REPORT.md` must match the recorded runs.

**What you run by hand**, with your API key. It never runs in CI:

```bash
cd src/sreagent
export GITHUB_REPO=kolade86/online-boutique-aws-eks APP_NAMESPACE=online-boutique-dev
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY; echo

python cli.py lint                       # the case files, before spending anything
python cli.py eval --runs 3              # every case with a recording, Sonnet 5.5 and Opus 5.5
python cli.py eval paymentservice-oomkilled --runs 5 --models claude-sonnet-5-5
python cli.py eval-report                # writes regressions/REPORT.md; commit it with the runs
python cli.py scores                     # every run, one line each
```

- `eval` records each run and exits 1 on a `critical` outcome.
- `eval` refuses Claude Haiku.
- `replay CASE` is `eval` for one case with `SREAGENT_MODEL`, saved only
  with `--record`.
- These commands need the packages in `requirements.txt`: a hand-written
  assessment's proposal goes through the real allow-list.

**Reading the report.**

- **Sample size is stated.** Beside each model's count is the highest
  critical rate those runs cannot rule out (the one-sided 95% bound). Three
  clean runs still allow 63%; thirty allow 10%.
- **"Cause labelled"** shows how often each model called the cause observed
  or inferred on the same recording. On the emailservice case Opus 5.5
  said observed once and inferred twice, from the same evidence.
- **Compare like with like:** runs with the same recording, the same rule
  version and the same unavailable calls.

**Hand-written assessments** are used only where no recording exists, and
the report lists them apart. PR #19's is written by hand from the report.
PR #32's is the agent's own, copied from the pull request body.

**Tools the recording lacks.** A replay answers any unrecorded call with
"No recorded output ... treat it as unavailable". It uses the same prompt and
tools as live, so it tests the real agent.

- **Unavailable calls are counted.** Each run records which calls had no
  recording (`unavailable`), and `replay` prints them.
- **A pod log with another `tail_lines` is served, and marked.** If the same
  pod's log (same container, same instance) was recorded with another
  `tail_lines`, the replay serves that recording unchanged. It picks the
  smallest recorded tail that covers the request, or else the largest. It
  never cuts, pads or makes up a log. The run lists each one under
  `served_instead`, e.g. `served recorded tail 40 for 50`, and `eval`
  prints them. `tail_lines` 100 is the same as leaving it out.
- **What that means for scores:** a score lost to a missing recording
  reflects the fixture, not the agent. Compare replay scores only between
  runs with the same unavailable set.
- **The emailservice case has only `list_events` recorded,** so every run
  lacks `describe_deployment`, `list_pods` and `list_hpas`.
- **New cases should record the basics.** When an incident happens, record
  the standard pack while the cluster still shows it. The pack is pods,
  events, deployments, `describe_deployment`, HPAs, memory, CPU and
  restarts, plus Argo CD's Applications (the list, and the one that deploys
  the chart: `online-boutique`, or `--argocd-app NAME`). It is saved
  redacted and pinned to main's current commit:

```bash
python cli.py capture regressions/my-incident.json --service recommendationservice \
  --question "Why is recommendationservice being OOMKilled?"
```

**Capturing an incident the agent investigated.** Give `capture` the
agent's own record, and it also records the calls the agent made and the
alert, so a replay can run the case as an alert:

```bash
ns=online-boutique-dev
kubectl port-forward -n "$ns" svc/sreagent 8080:8080 >/dev/null &
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093 >/dev/null &
# both files hold node names and IPs: keep them outside the repository
kubectl get secret sreagent -n "$ns" -o go-template='Authorization: Bearer {{index .data "api-token" | base64decode}}' \
  | curl -s -H @- http://localhost:8080/investigations > ~/investigations.json
curl -s http://localhost:9093/api/v2/alerts > ~/am-alerts.json

python cli.py capture regressions/paymentservice-oomkilled.json --service paymentservice \
  --investigations ~/investigations.json --alert-file ~/am-alerts.json
```

- **When to run it:** while the alert is firing and before the agent's
  pull request is merged. The case is pinned to main's head at capture
  time, which must still hold the fault. Afterwards, pass the fault's commit
  with `--repo-ref`.
- **Which investigation:** the latest alert investigation for `--service`,
  or `--id`. The agent keeps the last 20 in memory, and a restart loses
  them.
- **What is recorded:** the agent's cluster and Prometheus calls first, then
  the standard pack. A call that fails is recorded as a failure, and fails
  the same way in a replay. Repository reads are not recorded: a replay
  takes them from git at the case's commit.
- **Outputs.** `/investigations` holds the agent's calls but not their
  outputs, so `capture` runs the same calls again. The outputs are those of
  capture time, not of the investigation.
  `GET /investigations/<id>/recording` returns the outputs the agent saw,
  and the alert payload. Save that instead and `--alert-file` is not
  needed.
- **Redaction.** Outputs, the alert and the agent's report are redacted
  with one set of placeholders for the whole case. Redaction is by pattern:
  read the recorded files before committing them.
- **For a healthy cluster,** ask the agent a question through `/ask`, then
  capture with `--id` and no alert file.

**Redaction.** Everything people read has node names, IP addresses,
instance IDs, load balancer hostnames and AWS account IDs (including inside
ECR image URLs) replaced. That covers reports, PR bodies, `/investigations`
and saved CLI output. The replacements are placeholders numbered
consistently within a document: `<node-1>`, `<ip-2>`, `<instance-1>`,
`<lb-1>`, `<account-id>`.

The Kubernetes tools' output is redacted the same way before the model sees
it. One mapping is kept for the process, so `<node-2>` in `list_pods` is
`<node-2>` in `get_resource kind=nodes`: the model can still tell which pods
share a node. `get_resource` takes the placeholder as a node's name and
reads the real one. Prometheus output is not redacted for the model.

## Model choice

The default is **Claude Sonnet 5.5** (`claude-sonnet-5-5`), for both `/alert`
and `/ask`. Set `SREAGENT_MODEL` to change it.

**Do not use Claude Haiku 4.5 for this agent.** It was the original
default. It has failed the
[emailservice regression case](regressions/emailservice-pod-replaced.json)
in all nine runs, on every version of the prompt, and one of those runs
would have been allowed to open a pull request:

| Runs | Result |
|---|---|
| 3 live, 2026-10-04 | All wrong. That includes the run after the prompt and tool fixes that let Sonnet 5.5 pass |
| 3 replays, 2026-10-10 | All wrong. It never submitted an evidence assessment |
| 3 replays, 2026-10-11 | All wrong. It submitted one each time, with the cause marked observed, scoring 73, 30 and 60 |

- **The answer was the same each time:** readiness probes as the cause.
  The prompt says a failing readiness probe never kills a pod.
- **It stopped early.** It made two to six tool calls, never read the chart
  values although the prompt told it to, and filled the gaps with claims no
  tool result supported: an HPA scale-up that no event shows, and blame on a
  node.
- **It overstated its evidence.** In all three assessed runs it marked the
  wrong cause "observed", citing `list_events` alone, and claimed up to four
  alternatives ruled out from that one call. The run that scored 73 passed
  both gates then in force. [The observed rule](#the-observed-rule) now
  blocks those three runs, but it checks which tools were cited, not whether
  the label is honest. A model that mislabels a wrong cause is the case it
  is weakest against.
- **Sonnet 5.5 and Opus 5.5 passed all nine of their replays** of the same
  case, and marked the cause inferred every time.
- Sonnet 5.5 costs about twice as much per token, and used about three times
  the tool calls on this case. A wrong diagnosis that opens a PR costs more.

Nothing in the code refuses a model name: `SREAGENT_MODEL` is the only
control.

**Refusal fallback.** Sonnet 5.5's safety classifiers can decline a request
(`stop_reason: refusal`). Logs full of errors could plausibly trip the
`cyber` classifier by mistake.

- For Sonnet 5.5 and the Opus and Fable models, the provider sends
  `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). The API
  then retries `cyber` declines on Claude Sonnet 5.
- A decline that is not retried ends the run as `model_stopped`, with the
  category shown, for example `refusal:cyber`.
- Other models are called without the fallback.

## Configuration

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Required for `ask` / `investigate` |
| `GITHUB_REPO` | (none) | Required, as `owner/name` |
| `GITHUB_TOKEN` | (none) | Optional for reads; needed for private repos |
| `APP_NAMESPACE` | the pod's own namespace | Required outside the cluster |
| `ARGOCD_NAMESPACE` | `argocd` | Where `get_resource` reads Argo CD Applications. Must match the chart's `rbac.namespaceRules` |
| `PROMETHEUS_URL` | `http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090` | |
| `KUBE_CONTEXT` | current context | kubeconfig context to use locally |
| `SREAGENT_MODEL` | `claude-sonnet-5-5` | Used by `/alert`, `/ask` and the CLI. See [Model choice](#model-choice) |
| `SREAGENT_MAX_TOOL_CALLS` | `15` | Per alert investigation |
| `SREAGENT_MAX_TOOL_CALLS_ASK` | `20` | Per question: `/ask`, the chat page, `cli.py ask`, replays of `ask` cases |
| `SREAGENT_TIMEOUT_SECONDS` | `300` | Per investigation |
| `SREAGENT_MODEL_TIMEOUT_SECONDS` | `120` | Per model request |
| `SREAGENT_MAX_TOKENS` | `16000` | Max output tokens per model turn. Includes Sonnet 5.5's thinking |
| `SREAGENT_TOOL_OUTPUT_MAX_CHARS` | `6000` | Each tool result is cut to this length |
| `SREAGENT_API_TOKEN` | (none) | Required by the server: bearer token for `/alert`, `/ask`, `/investigations` |
| `SREAGENT_DEDUP_MINUTES` | `30` | Incident window: later alerts for the same namespace/service are not investigated again (they comment on an open agent PR instead) |
| `SREAGENT_MIN_CONFIDENCE_FOR_PR` | `70` | Evidence score a proposed change needs. See [Evidence score](#evidence-score) |
| `SREAGENT_MIN_CONFIDENCE_FOR_ROLLBACK` | `85` | Evidence score an `image.tag` rollback needs |
| `SREAGENT_RECENT_MERGE_MINUTES` | `60` | An agent PR for the same service merged this recently blocks a new proposal |
| `SREAGENT_PRICES_FILE` | `model_prices.json` next to the code | Model prices for the cost estimate |
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

- All nine Haiku 4.5 runs failed. They blamed readiness probes and never
  found what scaled the Deployment back up. See
  [Model choice](#model-choice).
- Sonnet 5.5 and Opus 5.5 passed every replay.
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

Three synthetic cases cover causes outside the allow-list. Each must be
explained, and none may open a pull request:

| Case | Mode | The fault |
|---|---|---|
| [cartservice-selector-mismatch](regressions/cartservice-selector-mismatch.json) | ask | The Service's selector was changed by hand and matches no pods |
| [argocd-sync-failed](regressions/argocd-sync-failed.json) | ask | Argo CD's sync fails (no ExternalSecret CRD), so a new tag never rolls out |
| [node-capacity-pending](regressions/node-capacity-pending.json) | alert | Pods Pending, Insufficient cpu on every node. Lowering requests is a wrong answer, and a critical outcome if proposed |

- **Synthetic means no live capture.** Every output was produced by the real
  tool code from hand-built Kubernetes objects, and each recorded call says
  `"source": "synthetic"`. The Services, ports, requests and image tags are
  the chart's at the case's commit, so the repository tools agree with the
  story.
- **The node case leaves out what the agent cannot see:** the node group's
  maximum (AWS) and the cluster autoscaler's status (`kube-system`).

### Recording a selector mismatch live

This breaks the cart in the dev cluster for a few minutes: run it only in a
throwaway sandbox. The agent must already run an image with `get_resource`.
Bash (Git Bash on Windows), from the repository root, with your own
kubeconfig.

```bash
ns=online-boutique-dev
cd src/sreagent

# 1. Pause Argo CD's automated sync, or self-heal reverts the change in seconds
kubectl patch application online-boutique -n argocd --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl get application online-boutique -n argocd -o jsonpath='{.spec.syncPolicy}'; echo   # no "automated"

# 2. Point the Service at a label no pod has
kubectl patch service cartservice -n "$ns" --type merge \
  -p '{"spec":{"selector":{"app":"cartservice-canary"}}}'
kubectl get endpointslices -n "$ns" -l kubernetes.io/service-name=cartservice   # ENDPOINTS <unset>

# 3. Wait a minute or two (load generator traffic puts errors in the frontend's
#    logs), then ask the agent and save its record
kubectl port-forward -n "$ns" svc/sreagent 8080:8080 >/dev/null &
auth() { kubectl get secret sreagent -n "$ns" -o go-template='Authorization: Bearer {{index .data "api-token" | base64decode}}'; }
auth | curl -s -H @- -H 'Content-Type: application/json' \
  -d '{"question": "Customers say the cart is broken: adding items and checking out both fail with an error page. What is wrong?"}' \
  http://localhost:8080/ask
# keep this file outside the repository: it holds node names and IPs
auth | curl -s -H @- http://localhost:8080/investigations > ~/investigations.json

# 4. Capture while the fault is still live: the agent's calls, and the standard pack
python cli.py capture regressions/cartservice-selector-mismatch-live.json --service cartservice \
  --investigations ~/investigations.json

# 5. Restore the selector, then automated sync as Terraform sets it
kubectl patch service cartservice -n "$ns" --type merge -p '{"spec":{"selector":{"app":"cartservice"}}}'
kubectl patch application online-boutique -n argocd --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl get application online-boutique -n argocd -o jsonpath='{.status.sync.status}'; echo   # Synced
kubectl get endpointslices -n "$ns" -l kubernetes.io/service-name=cartservice   # two endpoints
kill %1
```

Then, for the new case file:

- Copy `expected` from the synthetic case (with the label you used).
- Read every recorded file before committing: redaction is by pattern.
- Run `python cli.py lint`.
- Run `python cli.py eval cartservice-selector-mismatch-live --models claude-sonnet-5-5 --runs 3`,
  then `python cli.py eval-report`.

If step 5 is missed, the cart stays broken and Argo CD stays manual. The
next `terraform apply` restores the sync policy (the Application is a
Terraform resource), and Argo CD's self-heal then restores the selector.

How the cases are run and judged: [Evaluation](#evaluation).

## Tests

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

The loop tests in `test_agent.py` use a scripted fake model provider, so they
make no network calls and need no packages installed. Two test files are
skipped when their packages are missing:

- `test_anthropic_provider.py` needs `anthropic`
- `test_server.py` needs `fastapi` and `httpx`. Its chat page tests also need
  `node`: they run the page's own script against a small fake DOM
  ([chat_page_harness.js](chat_page_harness.js)). GitHub's Ubuntu runners
  include Node
- `test_values_change.py` and `test_pr_tool.py` need `ruamel.yaml`. They run
  the allow-list and the whole PR flow against an in-memory GitHub, using the
  real chart files. Only the HTTP calls themselves are left untested.
- `test_evaluation.py` needs `ruamel.yaml` and the repository's full git
  history: each case reads the chart at the commit it pins. A shallow clone
  fails it (`git fetch --unshallow`).

`httpx` is used only by FastAPI's test client, so it is not in
`requirements.txt`. Run `pip install httpx` to run the server tests.
