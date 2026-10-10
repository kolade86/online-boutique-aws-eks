#!/usr/bin/env bash
# Load the SRE agent's secrets into AWS Secrets Manager.
#
#   bash src/sreagent/load-secrets.sh                  # prompt for the keys
#   bash src/sreagent/load-secrets.sh --from-cluster   # copy today's Kubernetes Secret
#   bash src/sreagent/load-secrets.sh --rotate-api-token
#
# Run it after `terraform apply` (Terraform creates the empty secret
# <project>-<env>-sreagent). External Secrets then syncs the values into the
# Kubernetes Secrets "sreagent" (app namespace) and "sreagent-webhook"
# (monitoring).
#
# Safety:
# - Keys are read silently, never echoed, never written to a file, and never
#   passed to curl on its command line.
# - Pasted input is cleaned: line endings, spaces, quotes, terminal
#   bracketed-paste markers and a leading "NAME=" or "export NAME=".
# - Each key's prefix and length are checked, and it is tested with a real API
#   call before anything is stored.
# - api-token is generated here, and kept on re-runs unless --rotate-api-token.
#
# Settings (environment variables): PROJECT_NAME (online-boutique),
# ENVIRONMENT (dev), AWS_REGION (aws configure), APP_NAMESPACE
# (<project>-<env>), GITHUB_REPO (from the git remote).

set -euo pipefail

# Git Bash: don't let MSYS rewrite arguments that look like paths.
export MSYS_NO_PATHCONV=1

ANTHROPIC_PREFIX="sk-ant-"
GITHUB_PREFIX="github_pat_"

log() { printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# --- pure helpers (unit-tested in test_load_secrets.sh) -----------------------

# Clean one pasted value: bracketed-paste markers, CR/LF, whitespace, quotes,
# and a leading "export NAME=" / "NAME=".
sanitize() {
  local v="$1" esc=$'\e'
  v="${v//${esc}\[200~/}"
  v="${v//${esc}\[201~/}"
  v="${v//$'\r'/}"
  v="${v//$'\n'/}"
  v="${v//[[:space:]]/}"
  # "export ANTHROPIC_API_KEY=sk-..." (spaces already gone) or "NAME=value".
  # Upper-case names only, so base64 '=' padding is never mistaken for one.
  v="${v#export}"
  if [[ "$v" =~ ^[A-Z_][A-Z0-9_]*=(.+)$ ]]; then
    v="${BASH_REMATCH[1]}"
  fi
  v="${v//\"/}"
  v="${v//\'/}"
  printf '%s' "$v"
}

# Values read from the cluster: only whitespace can be stray there.
strip_ws() {
  local v="$1"
  v="${v//[[:space:]]/}"
  printf '%s' "$v"
}

# Throw away whatever is waiting in the terminal's input buffer - for example
# the rest of a multi-line paste, which the shell would otherwise run as
# commands after this script exits. Only on a real terminal.
flush_input() {
  [[ -t 0 ]] || return 0
  local junk
  while IFS= read -r -s -t 0.1 junk; do :; done
  return 0
}

# Show enough to recognise a value without revealing it.
mask() {
  local v="$1"
  if (( ${#v} <= 16 )); then
    printf '(%d chars)' "${#v}"
  else
    printf '%s...%s (%d chars)' "${v:0:11}" "${v: -4}" "${#v}"
  fi
}

# validate_anthropic VALUE -> exit 0, or print why not and exit 1
validate_anthropic() {
  local v="$1"
  if [[ "$v" == "$GITHUB_PREFIX"* ]]; then
    echo "that looks like the GitHub token, not the Anthropic key"; return 1
  fi
  if [[ "$v" != "$ANTHROPIC_PREFIX"* ]]; then
    echo "an Anthropic API key starts with '$ANTHROPIC_PREFIX'"; return 1
  fi
  if [[ ! "$v" =~ ^sk-ant-[A-Za-z0-9_-]+$ ]]; then
    echo "contains characters an Anthropic key never has"; return 1
  fi
  if (( ${#v} < 90 || ${#v} > 200 )); then
    echo "length ${#v} is not that of an Anthropic API key (about 108)"; return 1
  fi
}

validate_github() {
  local v="$1"
  if [[ "$v" == "$ANTHROPIC_PREFIX"* ]]; then
    echo "that looks like the Anthropic key, not the GitHub token"; return 1
  fi
  if [[ "$v" == ghp_* || "$v" == gho_* ]]; then
    echo "that is a classic token; use a fine-grained one ('$GITHUB_PREFIX...') limited to this repository"; return 1
  fi
  if [[ "$v" != "$GITHUB_PREFIX"* ]]; then
    echo "a fine-grained GitHub token starts with '$GITHUB_PREFIX'"; return 1
  fi
  if [[ ! "$v" =~ ^github_pat_[A-Za-z0-9_]+$ ]]; then
    echo "contains characters a GitHub token never has"; return 1
  fi
  if (( ${#v} < 80 || ${#v} > 120 )); then
    echo "length ${#v} is not that of a fine-grained GitHub token (93)"; return 1
  fi
}

validate_api_token() {
  local v="$1"
  if [[ ! "$v" =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "must be 32-128 URL-safe base64 characters"; return 1
  fi
}

# 32 random bytes as URL-safe base64 without padding: no '/', '+' or '=' to
# trip shells, URLs or MSYS argument conversion.
generate_api_token() {
  # \r too: Git Bash's openssl ends its output with CRLF
  openssl rand -base64 32 | tr -d '\r\n=' | tr '+/' '-_'
}

# The values have been validated to plain [A-Za-z0-9_-], so no JSON escaping
# is needed - and none is attempted.
make_json() {
  printf '{"anthropic-api-key":"%s","github-token":"%s","api-token":"%s"}' "$1" "$2" "$3"
}

# json_get JSON KEY -> value of a flat string key (our own simple JSON only)
json_get() {
  local json="$1" key="$2"
  if [[ "$json" =~ \"$key\":\"([^\"]*)\" ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  fi
}

# owner/name from a GitHub remote URL (https or ssh)
repo_from_remote() {
  local url="$1"
  url="${url%.git}"
  url="${url#git@github.com:}"
  url="${url#https://github.com/}"
  url="${url#ssh://git@github.com/}"
  [[ "$url" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && printf '%s' "$url"
}

# --- live checks: headers go to curl on stdin (-K -), not on its command line --

# http_status URL HEADER... -> prints the HTTP status code
http_status() {
  local url="$1"; shift
  local config="" h
  for h in "$@"; do
    config+="header = \"${h}\""$'\n'
  done
  # -w prints 000 when no response arrives; ignore curl's exit status
  printf '%s' "$config" | curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -K - "$url" 2>/dev/null || true
}

check_anthropic() {
  local status
  status=$(http_status "https://api.anthropic.com/v1/models?limit=1" \
    "x-api-key: $1" "anthropic-version: 2023-06-01")
  case "$status" in
    200) return 0 ;;
    401) echo "Anthropic rejected the key (401): wrong or revoked" ;;
    403) echo "Anthropic refused the key (403): no access for this key" ;;
    000) echo "could not reach api.anthropic.com" ;;
    *)   echo "unexpected answer from Anthropic: HTTP $status" ;;
  esac
  return 1
}

check_github() {
  local token="$1" repo="$2" status
  status=$(http_status "https://api.github.com/repos/$repo" \
    "Authorization: Bearer $token" "Accept: application/vnd.github+json" "User-Agent: sreagent-load-secrets")
  case "$status" in
    200) ;;
    401) echo "GitHub rejected the token (401): wrong, expired or revoked"; return 1 ;;
    404) echo "the token cannot see $repo (404): give it access to this repository"; return 1 ;;
    000) echo "could not reach api.github.com"; return 1 ;;
    *)   echo "unexpected answer from GitHub for $repo: HTTP $status"; return 1 ;;
  esac
  status=$(http_status "https://api.github.com/repos/$repo/pulls?per_page=1" \
    "Authorization: Bearer $token" "Accept: application/vnd.github+json" "User-Agent: sreagent-load-secrets")
  if [[ "$status" != 200 ]]; then
    echo "the token cannot read pull requests on $repo (HTTP $status): grant 'Pull requests'"; return 1
  fi
}

# --- interactive input ----------------------------------------------------------

# prompt_value LABEL VALIDATOR CURRENT -> prints the accepted value.
# Empty input keeps CURRENT (when there is one).
prompt_value() {
  local label="$1" validator="$2" current="$3" raw value why tries=0
  while (( tries < 3 )); do
    tries=$((tries + 1))
    if [[ -n "$current" ]]; then
      log "$label: press Enter to keep $(mask "$current"), or paste a new one (input is hidden)"
    else
      log "$label: paste it and press Enter (input is hidden)"
    fi
    flush_input
    IFS= read -rs raw || raw=""
    flush_input
    log ""
    value=$(sanitize "$raw")
    raw=""
    if [[ -z "$value" && -n "$current" ]]; then
      printf '%s' "$current"; return 0
    fi
    if why=$($validator "$value"); then
      printf '%s' "$value"; return 0
    fi
    log "  rejected: $why"
  done
  die "$label: no valid value after 3 attempts"
}

# --- main ------------------------------------------------------------------------

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  local from_cluster=0 rotate=0 sync=1 arg
  for arg in "$@"; do
    case "$arg" in
      --from-cluster) from_cluster=1 ;;
      --rotate-api-token) rotate=1 ;;
      --no-sync) sync=0 ;;
      -h|--help) usage; return 0 ;;
      *) die "unknown option: $arg (see --help)" ;;
    esac
  done

  local project="${PROJECT_NAME:-online-boutique}"
  local env="${ENVIRONMENT:-dev}"
  local secret_id="${project}-${env}-sreagent"
  local app_ns="${APP_NAMESPACE:-${project}-${env}}"
  local region="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
  [[ -n "$region" ]] || die "set AWS_REGION (or a default region with 'aws configure')"
  export AWS_REGION="$region"

  local repo="${GITHUB_REPO:-$(repo_from_remote "$(git config --get remote.origin.url 2>/dev/null || true)")}"
  [[ -n "$repo" ]] || die "set GITHUB_REPO=owner/name (could not read it from the git remote)"

  local tool
  for tool in aws curl openssl; do
    command -v "$tool" >/dev/null || die "'$tool' is not installed or not on PATH"
  done

  local account
  account=$(aws sts get-caller-identity --query Account --output text) \
    || die "no working AWS credentials for this shell"
  log "AWS account $account, region $region, secret $secret_id, repo $repo"

  aws secretsmanager describe-secret --secret-id "$secret_id" >/dev/null 2>&1 \
    || die "secret $secret_id does not exist yet - run 'terraform apply' first (module.platform_services)"

  # Current values, if any (kept in memory only)
  local current_json="" cur_anthropic="" cur_github="" cur_token=""
  current_json=$(aws secretsmanager get-secret-value --secret-id "$secret_id" \
                   --query SecretString --output text 2>/dev/null || true)
  if [[ -n "$current_json" && "$current_json" != "None" ]]; then
    cur_anthropic=$(json_get "$current_json" anthropic-api-key)
    cur_github=$(json_get "$current_json" github-token)
    cur_token=$(json_get "$current_json" api-token)
  fi
  current_json=""

  local anthropic github token why
  if (( from_cluster )); then
    command -v kubectl >/dev/null || die "--from-cluster needs kubectl"
    log "Reading the current values from Secret 'sreagent' in namespace $app_ns ($(kubectl config current-context))"
    kubectl get secret sreagent -n "$app_ns" >/dev/null || die "cannot read Secret sreagent in $app_ns"
    # A missing key renders as "<no value>", which the validators reject
    anthropic=$(strip_ws "$(kubectl get secret sreagent -n "$app_ns" -o 'go-template={{index .data "anthropic-api-key" | base64decode}}')")
    github=$(strip_ws "$(kubectl get secret sreagent -n "$app_ns" -o 'go-template={{index .data "github-token" | base64decode}}')")
    token=$(strip_ws "$(kubectl get secret sreagent -n "$app_ns" -o 'go-template={{index .data "api-token" | base64decode}}')")
    why=$(validate_anthropic "$anthropic") || die "the cluster's anthropic-api-key: $why"
    why=$(validate_github "$github") || die "the cluster's github-token: $why"
    if ! why=$(validate_api_token "$token"); then
      # A token made before this script (e.g. standard base64 with '+/=')
      # still works; keep it so Alertmanager and the agent stay in step.
      [[ "$token" =~ ^[A-Za-z0-9+/=_-]{32,128}$ ]] || die "the cluster's api-token: $why"
      log "  note: keeping the cluster's api-token as is (not URL-safe base64, but valid)"
    fi
  else
    anthropic=$(prompt_value "Anthropic API key" validate_anthropic "$cur_anthropic") || exit 1
    github=$(prompt_value "GitHub fine-grained token" validate_github "$cur_github") || exit 1
    if (( rotate )) || [[ -z "$cur_token" ]]; then
      token=$(generate_api_token) || die "could not generate an api-token (openssl)"
      log "Generated a new api-token $(mask "$token")"
    else
      token="$cur_token"
      log "Keeping the existing api-token $(mask "$token") (--rotate-api-token to replace it)"
    fi
  fi
  why=$(validate_api_token "$token") || [[ "$token" =~ ^[A-Za-z0-9+/=_-]{32,128}$ ]] || die "api-token: $why"

  log "Testing the Anthropic key $(mask "$anthropic") ..."
  why=$(check_anthropic "$anthropic") || die "$why"
  log "  ok"
  log "Testing the GitHub token $(mask "$github") on $repo ..."
  why=$(check_github "$github" "$repo") || die "$why"
  log "  ok (repository and pull requests readable; write access is first used when the agent opens a PR)"

  local changed=0
  if [[ "$anthropic" != "$cur_anthropic" || "$github" != "$cur_github" || "$token" != "$cur_token" ]]; then
    changed=1
    # The JSON is passed as an argument: the AWS CLI cannot read a secret
    # from stdin, and a temp file would put it on disk. It is visible to
    # your own processes only for the second the command runs.
    aws secretsmanager put-secret-value --secret-id "$secret_id" \
      --secret-string "$(make_json "$anthropic" "$github" "$token")" >/dev/null \
      || die "could not store the secret"
    log "Stored in $secret_id"
  else
    log "Secrets Manager already holds these values; nothing stored"
  fi
  anthropic=""; github=""; cur_anthropic=""; cur_github=""

  if (( ! sync )) || ! command -v kubectl >/dev/null || ! kubectl get ns "$app_ns" >/dev/null 2>&1; then
    log "Not syncing (no --no-sync, kubectl or cluster access). External Secrets picks the values up within its refresh interval (1h)."
    return 0
  fi

  log "Syncing on kubectl context $(kubectl config current-context)"
  local now es ns
  now=$(date +%s)
  for es in "$app_ns/sreagent" "monitoring/sreagent-webhook"; do
    ns="${es%%/*}"
    if kubectl get externalsecret "${es#*/}" -n "$ns" >/dev/null 2>&1; then
      kubectl annotate externalsecret "${es#*/}" -n "$ns" force-sync="$now" --overwrite >/dev/null
      if kubectl wait externalsecret "${es#*/}" -n "$ns" --for=condition=Ready --timeout=90s >/dev/null 2>&1; then
        log "ExternalSecret $es synced"
      else
        log "WARNING: ExternalSecret $es is not Ready - check: kubectl describe externalsecret ${es#*/} -n $ns"
      fi
    else
      log "ExternalSecret $es not found yet (Argo CD / terraform apply creates it); it will sync when created"
    fi
  done

  if (( changed )) && kubectl get deploy sreagent -n "$app_ns" >/dev/null 2>&1; then
    # The agent reads its secrets as environment variables at start-up
    kubectl rollout restart deploy/sreagent -n "$app_ns" >/dev/null
    kubectl rollout status deploy/sreagent -n "$app_ns" --timeout=180s >&2 \
      || log "WARNING: the agent did not become ready - kubectl logs -n $app_ns deploy/sreagent"
  fi
  log "Done."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
