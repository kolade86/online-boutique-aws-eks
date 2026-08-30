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

## Accessing the UI

```bash
terraform output argocd_url            # http://<alb-hostname>
terraform output argocd_login_info     # username, URL, password command
```

The ALB takes a few minutes to provision. Until then the output is empty and
you can reach Argo CD directly:

```bash
kubectl port-forward -n argocd svc/argocd-server 8080:80   # http://localhost:8080
```

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

## Relationship to the GitHub Actions deployment

**Argo CD does not deploy anything on its own yet.** In dev,
`argocd_enable_automated_sync = false`, so the `automated` block is omitted
from the Application's sync policy entirely. Argo CD installs, connects to the
repo, renders the chart, and compares it against the cluster — but it only
writes when told to.

`helm-deploy.yml` therefore remains the sole writer to the cluster. Nothing
fights. Helm has not gone away either: Argo CD renders the same chart. What
will eventually change is *who* runs it.

Expect the Application to sit at **OutOfSync**. That is the intended state, and
it is useful — it is Argo CD showing you the drift between git and the cluster
before it is trusted to act on it. Deploy manually with:

```bash
argocd app sync online-boutique      # or press Sync in the UI
```

**Recommended handover, when ready:**

1. Sync manually a few times and confirm Argo CD reports `Synced` / `Healthy`
   and that the diff matches what you expect.
2. Set `argocd_enable_automated_sync = true`.
3. Stop *running* the Helm Deploy workflow — leave the file in place. From that
   point the two would otherwise fight, and Argo CD wins because it reconciles
   continuously: the workflow deploys a specific tag
   (`v20260829-171752-abc12345`) and Argo CD heals it back to `app_image_tag`
   (default `latest`).
4. Once Argo CD has owned a few releases, consider `enable_prune = true`.

## Deliberately not included

Image Updater, IP allow-listing (`alb.ingress.kubernetes.io/inbound-cidrs`),
and prune are all out of scope for this iteration.

## Security note

The ingress is `internet-facing` with an HTTP:80 listener and no IP
restriction, so the Argo CD UI and API — and the admin login — are reachable
from anywhere, unencrypted. Acceptable for a sandbox; before this is anything
else, add an ACM certificate with an HTTPS listener, and either
`inbound-cidrs` or `ingress_scheme = "internal"`.
