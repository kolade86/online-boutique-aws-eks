#!/bin/bash
# Clear a stale Terraform state lock, safely.
#
# State is locked with a lock file next to the state in S3 (use_lockfile in
# backend.tf): s3://<bucket>/terraform.tfstate.tflock. This script shows who
# holds the lock, for which operation and since when; refuses while a
# terraform process runs on this machine; and only then, after you type
# "unlock", runs `terraform force-unlock` with that lock's ID.
#
# A lock held by a CI run (Terraform Plan or Apply, "runner@...") is real:
# wait for the run to finish, or cancel it, rather than unlocking.
#
# Usage, from anywhere in the repository (Git Bash on Windows, or Linux/macOS):
#   scripts/tf-unlock.sh [environment]        # default: dev

set -euo pipefail

ENVIRONMENT="${1:-dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT}/terraform/environments/${ENVIRONMENT}"
KEY="terraform.tfstate"                    # backend.tf

[ -d "${TF_DIR}" ] || { echo "No such environment: ${TF_DIR}" >&2; exit 1; }

# The bucket: backend.hcl (written by bootstrap-backend.sh), else this account's
BUCKET=""
if [ -f "${TF_DIR}/backend.hcl" ]; then
  BUCKET="$(sed -n 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${TF_DIR}/backend.hcl" | head -n 1)"
fi
if [ -z "${BUCKET}" ]; then
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
  BUCKET="online-boutique-${ENVIRONMENT}-${ACCOUNT}-terraform-state"
fi
LOCK="s3://${BUCKET}/${KEY}.tflock"

# 1. The lock, if there is one
if ! LOCK_JSON="$(aws s3 cp "${LOCK}" - 2>/dev/null)"; then
  echo "No lock held: ${LOCK} does not exist."
  exit 0
fi
field() {   # one string field of the lock's JSON (no jq needed)
  printf '%s' "${LOCK_JSON}" | tr -d '\n' |
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}
ID="$(field ID)"
WHO="$(field Who)"
OPERATION="$(field Operation)"
CREATED="$(field Created)"
VERSION="$(field Version)"
if [ -z "${ID}" ]; then
  echo "Could not read a lock ID from ${LOCK}:" >&2
  printf '%s\n' "${LOCK_JSON}" >&2
  exit 1
fi

AGE="unknown"
if CREATED_S="$(date -u -d "${CREATED}" +%s 2>/dev/null)"; then
  SECONDS_HELD=$(( $(date -u +%s) - CREATED_S ))
  AGE="$(( SECONDS_HELD / 3600 ))h $(( SECONDS_HELD % 3600 / 60 ))m"
fi

echo "State lock: ${LOCK}"
echo "  ID:        ${ID}"
echo "  Held by:   ${WHO:-unknown}"
echo "  Operation: ${OPERATION:-unknown} (Terraform ${VERSION:-?})"
echo "  Since:     ${CREATED:-unknown} (${AGE} ago)"
case "${WHO}" in
  runner@*) echo "  This looks like a GitHub Actions runner: check the Terraform Plan and Apply runs first." ;;
esac
echo ""

# 2. Refuse while terraform runs here: its lock is not stale. terraform-ls
#    (the editor's language server) is a different process and does not count.
terraform_running() {
  if command -v tasklist > /dev/null 2>&1; then          # Windows, from Git Bash
    tasklist //FI "IMAGENAME eq terraform.exe" //NH 2> /dev/null | grep -qi '^terraform\.exe'
  elif command -v pgrep > /dev/null 2>&1; then
    pgrep -x terraform > /dev/null
  else
    ps -eo comm= | grep -qx terraform
  fi
}
if terraform_running; then
  echo "A terraform process is running on this machine: let it finish (or stop it) first." >&2
  exit 1
fi

# 3. Confirm, then unlock with that ID
if [ ! -d "${TF_DIR}/.terraform" ]; then
  echo "terraform/environments/${ENVIRONMENT} is not initialised: run terraform init there first." >&2
  exit 1
fi
read -r -p "Type unlock to clear lock ${ID}: " ANSWER
if [ "${ANSWER}" != "unlock" ]; then
  echo "Not unlocked."
  exit 1
fi
terraform -chdir="${TF_DIR}" force-unlock -force "${ID}"
