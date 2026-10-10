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
| `confidence.py` | The evidence score: the scoring rule and `submit_assessment` |
| `replay.py` | Re-runs a regression case through the model against its recorded tool outputs |
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
cluster, the namespaced read-only Role in the chart enforces it too.

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
- **Per-investigation limits:** `SREAGENT_MAX_TOOL_CALLS` and
  `SREAGENT_TIMEOUT_SECONDS`, as for the CLI.
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

- **Read-only access to its namespace.** It runs as its own ServiceAccount,
  with a namespaced Role giving get/list/watch on pods, pods/log, events,
  deployments, replicasets and HPAs. It cannot read Secrets.
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
# RBAC: may read pods; may not read Secrets, delete, or look outside its namespace
sa="system:serviceaccount:$ns:sreagent"
kubectl auth can-i list pods   -n "$ns" --as="$sa"            # yes
kubectl auth can-i get secrets -n "$ns" --as="$sa"            # no
kubectl auth can-i delete pods -n "$ns" --as="$sa"            # no
kubectl auth can-i list pods   -n kube-system --as="$sa"      # no

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
- **Scores are not comparable across models.** On the same emailservice
  replay (three runs each), every Sonnet and Opus answer passed the keyword
  check, and every one called the cause inferred:

  | Model | Scores |
  |---|---|
  | Claude Haiku 4.5 | not assessed ×3: it never called `submit_assessment`, before the reminder existed |
  | Claude Sonnet 5.5 | 33, 46, 48 |
  | Claude Opus 5.5 | 76, 63, 63 (reported; these runs are not recorded in the case file) |

  The gap comes from self-description, not evidence. Sonnet marked the
  missing `describe_deployment` check as "could change the diagnosis" (−15)
  and listed an alternative as not ruled out (−15). Opus marked the same gap
  "detail only" (−2). So the score penalises a more self-critical model.
- **The same fixture varies by about 15 points between runs** of the same
  model: Opus 63 to 76, Sonnet 33 to 48. Read a single score as a band, not
  a number.

### How it is produced

Before its final answer, the model calls `submit_assessment`. This is a
strict tool: the API guarantees the input matches its schema. Each tool
result the model sees is numbered `[call #N]`, and the model cites those
numbers.

| Field | Values |
|---|---|
| `conclusion` | `cause_found`, `no_problem`, `inconclusive` |
| `cause` | one sentence |
| `cause_support` | `observed` (a cited result shows the cause itself) or `inferred` |
| `cause_evidence` | call numbers |
| `alternatives` | up to 5 × `{cause, result: ruled_out \| not_ruled_out, evidence}` |
| `timing` | `matches`, `does_not_match`, `not_checked`, plus `timing_evidence` |
| `unverified` | up to 5 × `{what, could_change_diagnosis}` |

[confidence.py](confidence.py) turns the assessment into the score. The
whole rule is the `POINTS` table at the top of that file:

| Rule | Points |
|---|---|
| Cause observed, with at least one valid citation | +50 |
| Cause inferred, or "observed" with no valid citation | +30 |
| Cause cited from 2 or more different tools | +10 |
| Each alternative ruled out with a citation (at most 2) | +10 |
| Each alternative not ruled out | −15 |
| Timing matches the alert, with a citation | +20 |
| Timing does not match | −20 |
| Each unverified item that could change the diagnosis | −15 |
| Each detail-only unverified item | −2, at most −6 in total |
| No alternative ruled out with evidence | capped at 55 |
| `inconclusive` | 20 |

- **Valid citations:** a citation counts only if it names a real tool call
  that succeeded. An assessment can't cite another assessment.
- **Every adjustment is a listed reason.** That includes dropped citations,
  an empty cause, and trimmed lists.
- **The score is deterministic.** The same assessment and the same tool
  calls always give the same score.

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
  `cause_support: observed` with valid citations, and a `cause_found`
  conclusion. An inferred cause is reported with its score but never opens a
  PR, whatever the number. Opus scored an inferred cause at 76.
  `confidence.change_blocked()` is the one rule used everywhere: the proposal
  tool, the final check before opening, the tool's reply to the model, and
  the note under the report.
- **No assessment, no change.** If the model finishes without an assessment,
  it is reminded once ("call submit_assessment for the report you just
  wrote"), and the report it already wrote is kept. If it still doesn't
  assess, the result shows **not assessed** and nothing can be proposed.
  `submit_assessment` doesn't count against `SREAGENT_MAX_TOOL_CALLS`.
- **The latest assessment decides.** Before a PR is opened, the rule is
  checked again against the latest assessment.

### The regression cases

`python cli.py scores` lists every scored run in [regressions/](regressions/).

- **Hand-written assessments** are used only where no recorded tool output
  exists, and are marked `hand-written`. They are deterministic, so
  `test_regressions.py` holds the rule to them: every wrong answer below 70,
  every correct one at 70 or above.
- **Model replays.** Where tool output was recorded, the model produces
  the assessment itself. A replay re-runs the case through the real model,
  with the tools answering from the recording. Repository tools read git at
  the commit that was deployed then. Any other call returns "no recorded
  output".

```bash
cd src/sreagent
export GITHUB_REPO=kolade86/online-boutique-aws-eks APP_NAMESPACE=online-boutique-dev
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY; echo
SREAGENT_MODEL=claude-haiku-4-5-20251001 python cli.py replay regressions/emailservice-pod-replaced.json --runs 3 --record
SREAGENT_MODEL=claude-sonnet-5-5          python cli.py replay regressions/emailservice-pod-replaced.json --runs 3 --record
python cli.py scores
```

Each replay run is graded by the case's keyword hints. That is a rough
automatic check, not a review. If an answer that fails the check scores at
or above the PR threshold, `replay` prints a WARNING.

**Tools the recording lacks.** A replay answers any unrecorded call with
"No recorded output ... treat it as unavailable". It uses the same prompt and
tools as live, so it tests the real agent.

- **Unavailable calls are counted.** Each run records which calls had no
  recording (`unavailable`), and `replay` prints them.
- **What that means for scores:** a score lost to a missing recording
  reflects the fixture, not the agent. Compare replay scores only between
  runs with the same unavailable set.
- **The emailservice case has only `list_events` recorded,** so every run
  lacks `describe_deployment`, `list_pods` and `list_hpas`.
- **New cases should record the basics.** When an incident happens, record
  the standard pack while the cluster still shows it. The pack is pods,
  events, deployments, `describe_deployment`, HPAs, memory, CPU and
  restarts, saved redacted and pinned to main's current commit:

```bash
python cli.py capture regressions/my-incident.json --service recommendationservice \
  --question "Why is recommendationservice being OOMKilled?"
```

**Redaction.** Everything people read has node names, IP addresses and AWS
account IDs (including inside ECR image URLs) replaced. That covers
reports, PR bodies, `/investigations` and saved CLI output. The
replacements are placeholders numbered consistently within a document:
`<node-1>`, `<ip-2>`, `<account-id>`. The model still sees the real values,
because which node something ran on can matter to a diagnosis.

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
| `SREAGENT_DEDUP_MINUTES` | `30` | Incident window: later alerts for the same namespace/service are not investigated again (they comment on an open agent PR instead) |
| `SREAGENT_MIN_CONFIDENCE_FOR_PR` | `70` | Evidence score a proposed change needs. See [Evidence score](#evidence-score) |
| `SREAGENT_MIN_CONFIDENCE_FOR_ROLLBACK` | `85` | Evidence score an `image.tag` rollback needs |
| `SREAGENT_RECENT_MERGE_MINUTES` | `60` | An agent PR for the same service merged this recently blocks a new proposal |
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
