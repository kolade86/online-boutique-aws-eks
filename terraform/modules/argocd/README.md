# Argo CD Module

Installs Argo CD and registers the Online Boutique chart as an Argo CD
Application, so the cluster is reconciled from git rather than from a CI job.

## How it is installed

Terraform, matching the rest of this repo:

1. `kubernetes_namespace.argocd` creates the `argocd` namespace.
2. `helm_release.argocd` installs the official `argo-cd` chart from
   `https://argoproj.github.io/argo-helm`, pinned by `chart_version`.
   Values come from [`argocd-values.yaml.tftpl`](./argocd-values.yaml.tftpl).
3. `time_sleep.wait_for_argocd` waits for the CRDs and API server to settle.
4. `kubernetes_ingress_v1.argocd_server` publishes the UI through an ALB via
   the AWS Load Balancer Controller (already installed by
   `modules/platform-services`).
5. `kubectl_manifest.online_boutique` creates the Application.

`argocd-server` runs with `server.insecure: true`, so it serves plain HTTP and
TLS terminates at the ALB. Without this the ALB speaks HTTP to an HTTPS backend
and every request ends in a redirect loop. The chart's own ingress is disabled
so Terraform owns the Ingress and can export the ALB hostname as an output.

## Accessing the UI and the CLI

```bash
terraform output argocd_url                 # UI
terraform output argocd_cli_login_command   # ready-made CLI command
terraform output argocd_login_info          # username, URL, password lookup
```

The ALB takes a few minutes to provision; until then the URL output is empty.

### The CLI must use `--grpc-web`

The Argo CD CLI speaks **gRPC, which requires HTTP/2**. An ALB only negotiates
HTTP/2 over TLS via ALPN on an HTTPS listener, and never supports h2c
(cleartext HTTP/2) on an HTTP listener. So this can never work against an
HTTP:80 ALB:

```bash
argocd login <alb-dns> --plaintext          # hangs, then:
                                            # gRPC connection not ready: context deadline exceeded
```

`--grpc-web` tunnels the same API over HTTP/1.1, which the ALB carries fine:

```bash
argocd login <alb-dns> --username admin --plaintext --grpc-web
argocd app list --grpc-web
```

`--grpc-web` is required on **every** subsequent command, not just login.

That the browser works while the CLI does not is expected: the UI is plain
REST over HTTP/1.1, the CLI is gRPC over HTTP/2.

## TLS and hostnames

`domain_name` and `acm_certificate_arn` are both empty by default, which keeps
the ALB on HTTP:80 using its raw `*.elb.amazonaws.com` name.

**HTTPS requires a domain you control.** ACM will not issue a certificate for
an ALB's `*.elb.amazonaws.com` hostname, because AWS owns that zone. There is
no way around this short of owning a domain or accepting a self-signed
certificate (browser warnings plus `--insecure` on every CLI call, which
defeats the purpose).

Once you have a domain, set both variables and the module switches over with
no other changes:

```hcl
argocd_domain_name         = "argocd.example.com"
argocd_acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/..."
```

That adds an HTTPS:443 listener with the certificate, an HTTP:80 -> HTTPS
redirect (`ssl-redirect`), a host rule and TLS block on the Ingress, and sets
`global.domain` / `configs.cm.url` so Argo CD stops emitting links to a
non-resolving address. You then point a DNS record for that hostname at the
ALB. The CLI still uses `--grpc-web`; it just drops `--plaintext`.

## Initial admin password

Username is `admin`. The chart generates the password into a secret:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

Change it after first login, then delete `argocd-initial-admin-secret`.

## What the Application tracks

| Field | Value |
|---|---|
| repo | `repo_url` |
| revision | `target_revision` (default `main`) |
| path | `helm/online-boutique` |
| destination | `app_namespace` on `https://kubernetes.default.svc` |
| sync | **manual** (`enable_automated_sync = false` in dev), prune off |

### Helm parameters are required, not optional

The application chart is **not self-sufficient**. In `values.yaml`,
`image.registry`, `image.prefix` and `redis.addr` are empty because the
GitHub Actions workflow supplies them with `--set` at deploy time. Rendered
with bare defaults the chart produces image references like `/-frontend:latest`
and omits the `redis-config` ConfigMap that `cartservice` mounts, so every pod
would fail.

The module therefore passes `image.registry`, `image.prefix`, `image.tag` and
`redis.addr` as Argo CD Helm parameters, sourced from Terraform (account ID,
region, project/environment, and the ElastiCache endpoint). Without them
Argo CD would deploy a broken application.

## How a release reaches the cluster

Argo CD is the **only** deployer. `helm-deploy.yml` has been retired to
`.disabled`; nothing else writes to the application namespace.

```
push to main -> Unit Tests -> Build Images -> push to ECR
                                                  |
                                    commits image tag to
                                    helm/online-boutique/values-dev.yaml
                                                  |
                                          Argo CD sees the commit
                                                  |
                                              syncs -> rollout
```

The `update-manifest` job in `build.yml` rewrites the `tag:` line in
`values-dev.yaml` and pushes it to `main`. Argo CD is watching `main`, so the
commit is the deploy. That makes every release a git commit: `git log` on that
file is your deployment history, and `git revert` is your rollback.

Two details that make this safe:

- The push uses the default `GITHUB_TOKEN`. Pushes made with it do not trigger
  workflow runs, so the commit cannot re-trigger the build. The commit message
  also carries `[skip ci]` as a second guard.
- The job only runs when **every** service was rebuilt. A partial build
  (`services: frontend`) publishes a tag that only some ECR repositories have,
  and committing it would leave the rest in `ImagePullBackOff`.

### Why image.tag is not a Terraform parameter

`image.registry`, `image.prefix` and `redis.addr` are injected by Terraform as
Argo CD Helm parameters, because they carry the AWS account ID and the
ElastiCache endpoint and should not be committed.

`image.tag` is deliberately **not** among them. Argo CD Helm `parameters`
override `valueFiles`, so setting the tag in Terraform would silently pin it
and no future release would ever roll out - the pipeline would look healthy
and ship nothing.

### Sync policy

Automated sync, self-heal, and **prune** are all on. Argo CD adds, updates,
and deletes so the namespace matches git. Removing a service from
`values.yaml` now removes it from the cluster.

## Deliberately not included

Image Updater, IP allow-listing (`alb.ingress.kubernetes.io/inbound-cidrs`),
and prune are all out of scope for this iteration.

## Security note

The ingress is `internet-facing` with an HTTP:80 listener and no IP
restriction, so the Argo CD UI and API — and the admin login — are reachable
from anywhere, unencrypted. Acceptable for a sandbox; before this is anything
else, add an ACM certificate with an HTTPS listener, and either
`inbound-cidrs` or `ingress_scheme = "internal"`.
