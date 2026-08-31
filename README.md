# Online Boutique on AWS EKS

A cloud-native microservices storefront running on Amazon EKS, provisioned with
Terraform and deployed continuously by Argo CD.

The application is derived from Google's
[Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo)
demo, re-platformed from GCP onto AWS: EKS instead of GKE, ECR instead of
Artifact Registry, ElastiCache instead of Memorystore, and ALB instead of
Google's load balancers.

---

## Architecture

```
                          Internet
                             |
                    ALB (internet-facing)
                             |
                      frontend (Go, HTTP)
                             |
       +---------+-----------+-----------+---------+---------+
       |         |           |           |         |         |
   product-   currency-    cart-    recommend-  shipping-   ad-
   catalog     service    service    ation       service   service
                             |
                      ElastiCache Redis
                             |
                       checkoutservice
                             |
              +--------------+--------------+
              |              |              |
          payment-       email-        (catalog,
          service        service      currency, cart,
                                        shipping)
```

All inter-service traffic is gRPC. Only the frontend speaks HTTP.

### Services

| Service | Language | Port | Protocol |
|---|---|---|---|
| frontend | Go | 8080 | HTTP |
| cartservice | C# (.NET 9) | 7070 | gRPC |
| checkoutservice | Go | 5050 | gRPC |
| productcatalogservice | Go | 3550 | gRPC |
| currencyservice | Node.js 24 | 7000 | gRPC |
| paymentservice | Node.js 24 | 50051 | gRPC |
| shippingservice | Go | 50051 | gRPC |
| emailservice | Python | 8080 | gRPC |
| recommendationservice | Python | 8080 | gRPC |
| adservice | Java (Gradle) | 9555 | gRPC |
| loadgenerator | Python (Locust) | – | HTTP |
| shoppingassistantservice | Python (Flask) | 80 | **not deployed** |

The shopping assistant depends on Gemini, AlloyDB and Google Secret Manager, so
it cannot run on AWS. It is disabled in the chart and excluded from CI — the
source is kept as reference work. See
[`src/shoppingassistantservice/README.md`](src/shoppingassistantservice/README.md).

---

## Repository layout

```
src/                        Service source, one Dockerfile each
helm/online-boutique/       Helm chart — the deployed desired state
terraform/
  modules/                  8 reusable modules
  environments/dev/         Dev environment composition
.github/workflows/          CI: build + test
k8s/                        Legacy Kustomize manifests (reference only)
bootstrap-backend.sh        Creates the S3 + DynamoDB state backend
```

---

## Infrastructure

Eight Terraform modules under [`terraform/modules/`](terraform/modules/):

| Module | What it provisions |
|---|---|
| `networking` | VPC (10.0.0.0/16), 2 public + 2 private subnets across 2 AZs, NAT, security groups |
| `eks-core` | EKS cluster, managed node group, OIDC/IRSA, core add-ons (CoreDNS, kube-proxy, VPC CNI, EBS/EFS CSI) |
| `data-persistence` | RDS PostgreSQL 17, ElastiCache Redis, EFS, KMS keys, Secrets Manager |
| `platform-services` | AWS Load Balancer Controller, External Secrets Operator, metrics-server, cluster-autoscaler, storage classes |
| `observability` | kube-prometheus-stack, CloudWatch logs and alarms, SNS alerting |
| `cicd` | ECR repositories, GitHub OIDC provider, CI IAM roles |
| `argocd` | Argo CD install, ALB ingress, and the Application definition |
| `bastion` | SSM-accessible bastion for private cluster access |

Dev environment defaults ([`terraform.tfvars`](terraform/environments/dev/terraform.tfvars)):
EKS 1.35, 3–12 × `t3.large` nodes (desired 4), `db.t3.micro` Postgres, single-node
Redis, region `us-east-1`.

---

## GitOps deployment flow

Argo CD is the **only** thing that deploys. GitHub Actions builds and publishes
images, then records which tag to run; Argo CD applies it to the dev cluster.

```
  feature branch
       |
       v
  pull request  ------------->  Unit Tests
       |                        (unit-test.yml)
       v
  merge to main
       |
       v
  Build Images  (build.yml)
       |
       +--> build 11 images, Trivy scan + SBOM
       |
       +--> push images to ECR
       |
       +--> update image.tag in
       |    helm/online-boutique/values-dev.yaml
       |
       +--> commit that tag to main
                |
                v
       Argo CD detects the git change
                |
                v
       renders the Helm chart
                |
                v
       auto-sync / self-heal / prune
                |
                v
          EKS (dev cluster)
```

Tests gate the pull request; the build runs only after the merge.

`values-dev.yaml` records the immutable image tag Argo CD is instructed to
deploy, so `git log` on that file is an auditable history of what ran and when.
Rolling back means restoring a previously known-good tag in git — Argo CD sees
the desired state change and reconciles the cluster back to that version.
Changing the cluster directly is not a rollback: self-heal will revert it.

**Why the tag lives in git, not Terraform.** `image.registry`, `image.prefix`
and `redis.addr` are injected by Terraform as Argo CD Helm parameters, because
they carry the AWS account ID and the ElastiCache endpoint. `image.tag`
deliberately is not: Argo CD Helm `parameters` override `valueFiles`, so setting
the tag in Terraform would pin it permanently and no future release would ever
roll out.

Image tags are immutable and traceable: `vYYYYMMDD-HHMMSS-<short-sha>`.

### Workflows

| Workflow | Trigger | Does |
|---|---|---|
| `unit-test.yml` | PRs touching `src/**` or `helm/**` | Go, .NET, Jest, unittest, Gradle tests |
| `build.yml` | push to `main`, or manual | Build 11 images, Trivy scan, SBOM, push to ECR, commit the tag |
| `terraform-checks.yml` | PRs touching `terraform/**` | fmt, validate, tflint — no AWS access |
| `terraform-plan.yml` | after Checks succeeds (`workflow_run`) | `terraform plan` to the job summary |
| `terraform-apply.yml` | manual (`workflow_dispatch`) | Terraform plan + apply against AWS |
| `helm-deploy.yml.disabled` | – | Retired. Argo CD replaced it |

`build.yml` has no hardcoded AWS account, region, or project name. It resolves a
`workflow_dispatch` input first, then a repo variable, and fails fast if neither
is set — so the repo can be pointed at any throwaway sandbox account.

**Required repository variables** (Settings → Secrets and variables → Actions):

| Variable | Example |
|---|---|
| `AWS_ACCOUNT_ID` | your 12-digit account ID |
| `AWS_REGION` | `us-east-1` |
| `PROJECT_NAME` | `online-boutique` |

---

## Terraform CI/CD

Infrastructure has its own pipeline, separate from the application one above:

Three workflows, deliberately separate: static checks never touch AWS, the plan
runs only after they pass, and applying is always a manual act.

```
  Terraform change -> pull request (terraform/**)
       |
       v
  Terraform Checks          fmt -check / validate / tflint
       |                    no AWS credentials at all
       |
       v  workflow_run, only when conclusion == success
       |
  Terraform Plan            OIDC -> AWS, plan to the job summary
       |                    checks out the exact commit Checks passed
       v
  review the plan, merge the PR
       |
       v
  Terraform Apply           manual: Actions -> Run workflow
       |                    checkout main, init, fresh plan, apply it
       v
      AWS
```

**Merging never applies anything.** After merge you trigger the apply yourself.

| Workflow | Trigger | Does |
|---|---|---|
| `terraform-checks.yml` | PRs to `main` touching `terraform/**` | fmt, validate, tflint. **Never authenticates to AWS** |
| `terraform-plan.yml` | `workflow_run` after Checks | `terraform plan` on the PR commit, into the job summary |
| `terraform-apply.yml` | `workflow_dispatch` (manual) | checkout `main`, init, fresh plan, apply that plan |

### How the chain is enforced

`workflow_run` fires on *completion* — success **or** failure — so the gate is
an explicit condition on the plan job:

```yaml
if: >-
  github.event.workflow_run.conclusion == 'success' &&
  github.event.workflow_run.head_repository.full_name == github.repository
```

The first clause is the dependency: a failed or cancelled Checks run still
reaches Plan, and is skipped there. The second is a security control — see
below.

Plan checks out `github.event.workflow_run.head_sha`, **not** the default
`workflow_run` ref, which is `main`. Without that, Plan would silently plan
main's code while appearing to validate the PR. A step immediately after
checkout asserts `git rev-parse HEAD` equals that SHA and fails loudly if not.

> **Security note on `workflow_run`.** Unlike `pull_request`, a `workflow_run`
> workflow executes in the **base repository's** context with access to secrets
> and OIDC — including for pull requests opened from forks. Since Plan checks
> out PR code and `terraform plan` executes provider binaries (and can run
> `external` data sources), a fork PR could otherwise assume the AWS role.
> The `head_repository.full_name == github.repository` condition refuses fork
> PRs for exactly this reason. Plan those locally instead.

Because Plan is triggered by `workflow_run` rather than by the PR, its result
appears in the **Actions** tab rather than as a status check on the PR. Only
`Terraform Checks` shows up inline, so use Checks for branch protection and
open the Plan run to read the diff.

### Running an apply

Actions → **Terraform Apply** → *Run workflow*. Type `apply` in the confirm box
(a typo-guard, since this changes real infrastructure).

It always checks out `main`, never the branch you launched it from, and applies
a plan file it just generated — so what executes is exactly what is printed
immediately above it in the log. A `terraform-apply` concurrency group prevents
two runs racing for the state lock.

### Local Terraform still works

CI is the normal path, not the only one. The same commands work locally as a
fallback — same remote state, same lock table, same provider versions via the
committed `.terraform.lock.hcl`:

```bash
cd terraform/environments/dev
terraform init
terraform plan
terraform apply
```

### AWS authentication

Both workflows use **GitHub OIDC** — there are no AWS access keys stored in the
repository. Each run exchanges a short-lived GitHub identity token for
temporary AWS credentials via `sts:AssumeRoleWithWebIdentity`, assuming
`online-boutique-terraform-role`.

That role is separate from the application CI role (which pushes images to
ECR). Its trust policy accepts exactly two subjects on this repository:

- `repo:<owner>/<repo>:pull_request` — the plan workflow
- `repo:<owner>/<repo>:ref:refs/heads/main` — the manual apply workflow

A push to any other branch, or a tag, cannot assume it.

> **Security note.** The Terraform role currently has `AdministratorAccess`,
> because this configuration manages IAM roles, KMS keys, EKS, RDS and VPC
> resources. That is a deliberate portfolio-stage trade-off, not a
> recommendation. Tightening it to least privilege — along with an approval
> environment on apply and drift detection — is the natural next step.

The role is also mapped into the cluster's `aws-auth` ConfigMap as
`system:masters`, because this configuration manages ~24 Kubernetes, Helm and
kubectl resources; without that, `apply` would fail once the cluster exists.

---

## Deploying from scratch

**Prerequisites:** Terraform ≥ 1.0, AWS CLI configured, `kubectl`, `helm`, and a
GitHub repository Argo CD can read.

**1. Create the Terraform state backend** (once per account)

```bash
./bootstrap-backend.sh
```

Creates the S3 bucket and DynamoDB lock table referenced by
[`backend.tf`](terraform/environments/dev/backend.tf).

**2. Provision the infrastructure**

```bash
cd terraform/environments/dev
terraform init
terraform plan  -out=tfplan
terraform apply tfplan
```

Expect 20–30 minutes; the EKS cluster and RDS instance dominate.

**3. Configure kubectl**

```bash
aws eks update-kubeconfig --region us-east-1 --name online-boutique-dev-cluster
```

**4. Build the first images**

Argo CD deploys whatever tag is in `values-dev.yaml`, so images must exist
before it can sync successfully. Merge to `main`, or run **Build Images**
manually with `services: all`. Until then, pods sit in `ImagePullBackOff` —
expected on a fresh cluster, not a fault.

**5. Watch Argo CD converge**

```bash
terraform output argocd_url                 # UI
terraform output argocd_login_info          # admin credentials
terraform output argocd_cli_login_command   # CLI
```

---

## Access

### Storefront

```bash
kubectl get ingress -n online-boutique-dev
```

The ALB hostname is the storefront URL.

### Argo CD

Exposed on an internet-facing ALB over HTTP. Get the URL and password from
Terraform outputs; the username is `admin`.

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

The CLI **must** use `--grpc-web`:

```bash
argocd login <alb-hostname> --username admin --plaintext --grpc-web
argocd app list --grpc-web
```

gRPC needs HTTP/2, and an ALB only negotiates HTTP/2 over TLS. `--grpc-web`
tunnels the same API over HTTP/1.1. The flag is required on *every* command,
not just login.

### Monitoring

Prometheus, Grafana and Alertmanager run in-cluster with **no ingress** — there
is no public monitoring endpoint. Reach them with port-forward:

```bash
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093
```

`terraform output` exposes these commands with the namespace filled in.
Prometheus keeps 30 days on a 50Gi gp3 volume. Alerts route to SNS → email.
CloudWatch covers logs and the AWS-managed services (RDS, Redis, ALB) that
Prometheus cannot see.

---

## Local development

```bash
# Render the chart without deploying
helm template online-boutique ./helm/online-boutique \
  -f helm/online-boutique/values-dev.yaml \
  --set image.registry=<ECR> --set image.prefix=online-boutique-dev \
  --set redis.addr=<REDIS>:6379

# Run one service's tests
cd src/frontend && go test ./...
cd src/currencyservice && npm test
```

Do **not** `helm install` against the cluster. Argo CD runs with self-heal
enabled and will revert anything applied out of band.

---

## Conventions

- Set `enabled: false` on a service in `values.yaml` to exclude it entirely —
  no Deployment, Service, HPA, PDB or ServiceMonitor is rendered. Omitting
  `enabled` means enabled.
- Health checks: HTTP `/_healthz` on the frontend, gRPC health v1 elsewhere.
- HPAs target 70% CPU. Critical services run ≥ 2 replicas.
- Terraform: one module per concern, composed in `environments/<env>/main.tf`.
  Run `terraform fmt -recursive` before committing.
- No AWS account IDs in the repository. They come from repo variables in CI and
  from `aws_caller_identity` in Terraform.

---

## Known limitations

- **Argo CD is served over HTTP**, not HTTPS — ACM cannot issue a certificate
  for an ALB's `*.elb.amazonaws.com` name, so TLS needs a domain. The module
  takes `domain_name` and `acm_certificate_arn`; setting both switches it to
  HTTPS:443 with an HTTP→HTTPS redirect.
- **Single environment.** Only `dev` exists. `staging` and `prod` appear as
  workflow inputs but have no Terraform composition.
- **The shopping assistant is not deployed** (see above).
- **Dev-grade sizing.** Single-AZ RDS, one Redis node, no cross-region backup.
