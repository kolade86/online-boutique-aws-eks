# Terraform CI/CD

The infrastructure in this directory has its own pipeline, separate from the
application's build and deploy (see the [root README](../README.md)).

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

## How the chain is enforced

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

## Running an apply

Actions → **Terraform Apply** → *Run workflow*. Type `apply` in the confirm box
(a typo-guard, since this changes real infrastructure).

It always checks out `main`, never the branch you launched it from, and applies
a plan file it just generated — so what executes is exactly what is printed
immediately above it in the log. A `terraform-apply` concurrency group prevents
two runs racing for the state lock.

## State and locking

State lives in the S3 bucket `online-boutique-dev-<account-id>-terraform-state`,
created by [`bootstrap-backend.sh`](../bootstrap-backend.sh) with versioning,
encryption and public access blocked. The account ID in the name means each
AWS account gets its own bucket, so a new sandbox never collides with an old
sandbox's bucket (S3 bucket names are global). `backend.tf` therefore leaves
the bucket out (a partial backend configuration), and the bucket is passed to
`terraform init` instead:

- **Locally:** the bootstrap script writes it to
  `environments/dev/backend.hcl`, which Git ignores, and runs
  `terraform init -reconfigure -backend-config=backend.hcl` itself.
- **In CI:** the Plan and Apply workflows build the name from the
  `AWS_ACCOUNT_ID` repo variable and pass it with `-backend-config`. They set
  no `TF_CLI_ARGS_*`.
- **Terraform Checks** runs `init -backend=false`, so it needs no bucket.

Locking uses S3's native lock file (`use_lockfile = true` in
`environments/dev/backend.tf`): Terraform writes `terraform.tfstate.tflock`
next to the state while it holds the lock. There is no DynamoDB table. Plan
and apply wait up to 5 minutes for a lock another run holds
(`-lock-timeout=5m`), rather than failing at once.

- **A stale lock.** If a run is killed while it holds the lock, the next run
  times out with "Error acquiring the state lock". Run the unlock script from
  anywhere in the repository:

  ```bash
  scripts/tf-unlock.sh
  ```

  - It reads the lock file and shows who holds it, the operation, and how
    long ago it was taken.
  - It refuses while a `terraform` process runs on your machine;
    `terraform-ls`, the editor's language server, doesn't count.
  - Otherwise it asks you to type `unlock`, then runs
    `terraform force-unlock` with that ID.
  - A lock held by `runner@...` is a GitHub Actions run: wait for it, or
    cancel it, rather than unlocking.

- **An interrupted apply.** An apply stopped part-way (a cancelled run, a
  lost connection) can leave resources created in AWS but missing from state.
  The next plan then tries to create them again, and fails because they
  exist. Bring each one into state, then plan again:

  ```bash
  terraform import '<resource address>' <AWS ID>
  ```

## Local Terraform still works

CI is the normal path, not the only one. The same commands work locally as a
fallback — same remote state, same lock file, same provider versions via the
committed `.terraform.lock.hcl`:

```bash
./bootstrap-backend.sh          # once per AWS account: bucket, backend.hcl, init
cd terraform/environments/dev
terraform plan
terraform apply
```

The bootstrap script also prints these lines for `~/.bashrc`. With them, a
plain `terraform init` finds `backend.hcl`, and plan and apply wait for locks:

```bash
terraform() {
  if [ -f backend.hcl ]; then
    TF_CLI_ARGS_init="-backend-config=backend.hcl" command terraform "$@"
  else
    command terraform "$@"
  fi
}
export TF_CLI_ARGS_plan="-lock-timeout=5m"
export TF_CLI_ARGS_apply="-lock-timeout=5m"
```

- **Only where there is a `backend.hcl`.** The function adds
  `-backend-config=backend.hcl` only when the current directory has a
  `backend.hcl`, so other Terraform projects are unaffected. This works in
  Git Bash on Windows.
- **It checks the current directory,** so it doesn't help with `-chdir`. Run
  init from `terraform/environments/dev`.
- **Without the lines,** init the long way:
  `terraform init -reconfigure -backend-config=backend.hcl`.

## AWS authentication

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
