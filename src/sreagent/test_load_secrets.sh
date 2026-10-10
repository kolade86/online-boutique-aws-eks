#!/usr/bin/env bash
# Tests for load-secrets.sh. No network, no AWS: aws, curl and kubectl are
# replaced by shell functions that record what they were asked to do.
#
#   bash test_load_secrets.sh
#
# Run by test_load_secrets.py, so CI's "Test: sreagent" job includes it.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=load-secrets.sh
source ./load-secrets.sh
set +e   # the script sets -e; the tests check exit codes themselves

PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$*"; }
eq()   { if [[ "$1" == "$2" ]]; then ok; else fail "$3: expected [$2], got [$1]"; fi; }
has()  { if [[ "$1" == *"$2"* ]]; then ok; else fail "$3: [$2] not in output"; fi; }
hasnt(){ if [[ "$1" != *"$2"* ]]; then ok; else fail "$3: [$2] must not appear"; fi; }

# Realistic shapes, fake values
ANTHROPIC="sk-ant-api03-$(printf 'A%.0s' {1..57})_$(printf 'b%.0s' {1..34})-AA"   # 108 chars
GITHUB="github_pat_11ABCDEFG0$(printf 'x%.0s' {1..72})"                            # 93 chars
eq "${#ANTHROPIC}" 108 "fixture length"
eq "${#GITHUB}" 93 "fixture length"

# --- sanitize ------------------------------------------------------------------
eq "$(sanitize "  $ANTHROPIC"$'\r\n')" "$ANTHROPIC" "CRLF and spaces"
eq "$(sanitize $'\e[200~'"$GITHUB"$'\e[201~')" "$GITHUB" "bracketed paste markers"
eq "$(sanitize "\"$GITHUB\"")" "$GITHUB" "double quotes"
eq "$(sanitize "'$GITHUB'")" "$GITHUB" "single quotes"
eq "$(sanitize "export ANTHROPIC_API_KEY=$ANTHROPIC")" "$ANTHROPIC" "export NAME= prefix"
eq "$(sanitize "GITHUB_TOKEN=\"$GITHUB\"")" "$GITHUB" "NAME=\"...\" prefix"
eq "$(sanitize $'\t'"$GITHUB "$'\t')" "$GITHUB" "tabs"
eq "$(sanitize "abcDEF123+/xyz0==")" "abcDEF123+/xyz0==" "base64 padding is not a NAME="
eq "$(strip_ws $' abc/+==\r\n')" "abc/+==" "strip_ws keeps everything but whitespace"

# --- validators ----------------------------------------------------------------
validate_anthropic "$ANTHROPIC" >/dev/null; eq $? 0 "good Anthropic key"
has "$(validate_anthropic "$GITHUB")" "looks like the GitHub token" "swapped keys"
has "$(validate_anthropic "sk-proj-$ANTHROPIC")" "starts with 'sk-ant-'" "wrong prefix"
has "$(validate_anthropic "sk-ant-short")" "length" "too short"
has "$(validate_anthropic "${ANTHROPIC:0:100}\$x")" "characters" "bad characters"
has "$(validate_anthropic "aws s3 ls")" "starts with" "a command pasted by mistake"

validate_github "$GITHUB" >/dev/null; eq $? 0 "good GitHub token"
has "$(validate_github "$ANTHROPIC")" "looks like the Anthropic key" "swapped keys"
has "$(validate_github "ghp_$(printf 'a%.0s' {1..36})")" "classic token" "classic token refused"
has "$(validate_github "github_pat_short")" "length" "too short"
has "$(validate_github "")" "starts with" "empty"

# --- api-token -----------------------------------------------------------------
t1=$(generate_api_token); t2=$(generate_api_token)
eq "${#t1}" 43 "generated token length (32 bytes)"
validate_api_token "$t1" >/dev/null; eq $? 0 "generated token is URL-safe"
[[ "$t1" != "$t2" ]] && ok || fail "two generated tokens are equal"
has "$(validate_api_token "short")" "32-128" "short token rejected"

# --- json, mask, repo ----------------------------------------------------------
json=$(make_json "$ANTHROPIC" "$GITHUB" "$t1")
eq "$(json_get "$json" anthropic-api-key)" "$ANTHROPIC" "json round trip: anthropic"
eq "$(json_get "$json" github-token)" "$GITHUB" "json round trip: github"
eq "$(json_get "$json" api-token)" "$t1" "json round trip: api-token"
eq "$(json_get "$json" missing)" "" "json_get missing key"
m=$(mask "$ANTHROPIC")
hasnt "$m" "$ANTHROPIC" "mask hides the value"
has "$m" "(108 chars)" "mask shows the length"
eq "$(repo_from_remote git@github.com:kolade86/online-boutique-aws-eks.git)" "kolade86/online-boutique-aws-eks" "ssh remote"
eq "$(repo_from_remote https://github.com/kolade86/online-boutique-aws-eks)" "kolade86/online-boutique-aws-eks" "https remote"

# --- end to end with mocks -----------------------------------------------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export GITHUB_REPO="kolade86/online-boutique-aws-eks" AWS_REGION="us-east-1"
SM_VALUE=""          # what Secrets Manager holds
SM_EXISTS=1
ANTHROPIC_STATUS=200

aws() {
  echo "aws $*" >> "$WORK/aws.log"
  case "$1 $2" in
    "sts get-caller-identity") echo "123456789012" ;;
    "configure get") echo "us-east-1" ;;
    "secretsmanager describe-secret") (( SM_EXISTS )) || return 254 ;;
    "secretsmanager get-secret-value") [[ -n "$SM_VALUE" ]] && echo "$SM_VALUE" || return 254 ;;
    "secretsmanager put-secret-value")
      local a; for a in "$@"; do [[ "$a" == "{"* ]] && printf '%s' "$a" > "$WORK/stored.json"; done ;;
  esac
}
curl() {
  local config; config=$(cat)
  echo "curl $*" >> "$WORK/curl-argv.log"
  echo "$config" >> "$WORK/curl-config.log"
  case "$*" in
    *api.anthropic.com*) printf '%s' "$ANTHROPIC_STATUS" ;;
    *) printf '200' ;;
  esac
}
export -f aws curl
run() { ( main "$@" ) >"$WORK/out" 2>&1; echo $?; }

# 1. First run: secret empty, keys pasted messily, token generated
rm -f "$WORK"/*.log "$WORK/stored.json"
rc=$(printf '  export ANTHROPIC_API_KEY=%s\r\n"%s"\n' "$ANTHROPIC" "$GITHUB" | run --no-sync)
out=$(cat "$WORK/out")
eq "$rc" 0 "first run succeeds"
stored=$(cat "$WORK/stored.json" 2>/dev/null)
eq "$(json_get "$stored" anthropic-api-key)" "$ANTHROPIC" "stored the cleaned Anthropic key"
eq "$(json_get "$stored" github-token)" "$GITHUB" "stored the cleaned GitHub token"
tok=$(json_get "$stored" api-token)
validate_api_token "$tok" >/dev/null; eq $? 0 "stored a generated api-token"
hasnt "$out" "$ANTHROPIC" "Anthropic key never printed"
hasnt "$out" "$GITHUB" "GitHub token never printed"
hasnt "$out" "$tok" "api-token never printed"
hasnt "$(cat "$WORK/curl-argv.log")" "sk-ant-" "Anthropic key not on curl's command line"
hasnt "$(cat "$WORK/curl-argv.log")" "github_pat_" "GitHub token not on curl's command line"
has "$(cat "$WORK/curl-config.log")" "x-api-key: $ANTHROPIC" "Anthropic key sent as a header via stdin"
has "$(cat "$WORK/curl-config.log")" "Authorization: Bearer $GITHUB" "GitHub token sent as a header via stdin"
has "$out" "Stored in online-boutique-dev-sreagent" "reports the store"

# 2. Re-run, Enter twice: keeps everything, including the api-token
SM_VALUE="$stored"; rm -f "$WORK/stored.json"
rc=$(printf '\n\n' | run --no-sync)
eq "$rc" 0 "re-run succeeds"
has "$(cat "$WORK/out")" "nothing stored" "unchanged values are not re-stored"
has "$(cat "$WORK/out")" "Keeping the existing api-token" "api-token kept on re-run"
[[ ! -e "$WORK/stored.json" ]] && ok || fail "re-run with no changes stored anyway"

# 3. --rotate-api-token: new token, keys kept
rc=$(printf '\n\n' | run --no-sync --rotate-api-token)
eq "$rc" 0 "rotation succeeds"
new_tok=$(json_get "$(cat "$WORK/stored.json")" api-token)
[[ -n "$new_tok" && "$new_tok" != "$tok" ]] && ok || fail "api-token not rotated"
eq "$(json_get "$(cat "$WORK/stored.json")" anthropic-api-key)" "$ANTHROPIC" "rotation keeps the Anthropic key"

# 4. Three bad pastes: refuses and stores nothing
SM_VALUE=""; rm -f "$WORK/stored.json"
rc=$(printf 'aws s3 ls\n%s\nsk-ant-oops\n' "$GITHUB" | run --no-sync)
[[ "$rc" != 0 ]] && ok || fail "bad input accepted"
has "$(cat "$WORK/out")" "no valid value after 3 attempts" "gives up after 3 attempts"
has "$(cat "$WORK/out")" "looks like the GitHub token" "explains a swapped paste"
[[ ! -e "$WORK/stored.json" ]] && ok || fail "stored after bad input"

# 5. Anthropic rejects the key: stores nothing
ANTHROPIC_STATUS=401
rc=$(printf '%s\n%s\n' "$ANTHROPIC" "$GITHUB" | run --no-sync)
[[ "$rc" != 0 ]] && ok || fail "401 key accepted"
has "$(cat "$WORK/out")" "rejected the key (401)" "reports the 401"
[[ ! -e "$WORK/stored.json" ]] && ok || fail "stored a key Anthropic rejected"
ANTHROPIC_STATUS=200

# 6. Secret not created yet: tells you to run terraform apply
SM_EXISTS=0
rc=$(printf '\n\n' | run --no-sync)
[[ "$rc" != 0 ]] && ok || fail "ran without the secret"
has "$(cat "$WORK/out")" "terraform apply" "points at terraform apply"
SM_EXISTS=1

# 7. --from-cluster: copies the live Secret, keeping a padded legacy api-token as is
LEGACY="AbCd+/EfGh1234567890abcdefghijklmnopqrstuvw="
kubectl() {
  case "$*" in
    "config current-context") echo "test-cluster" ;;
    *anthropic-api-key*) printf '%s\n' "$ANTHROPIC" ;;
    *github-token*) printf '%s' "$GITHUB" ;;
    *api-token*) printf '%s' "$LEGACY" ;;
    "get secret sreagent -n online-boutique-dev") echo "secret/sreagent" ;;
  esac
}
export -f kubectl
rm -f "$WORK/stored.json"
rc=$(run --no-sync --from-cluster </dev/null)
eq "$rc" 0 "--from-cluster succeeds"
eq "$(json_get "$(cat "$WORK/stored.json")" api-token)" "$LEGACY" "legacy api-token kept unchanged"
eq "$(json_get "$(cat "$WORK/stored.json")" github-token)" "$GITHUB" "github token copied"
hasnt "$(cat "$WORK/out")" "$LEGACY" "copied token never printed"

printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
