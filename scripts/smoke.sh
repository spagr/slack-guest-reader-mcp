#!/usr/bin/env bash
# Post-deploy smoke test: unauthenticated checks of the public surface of a deployment.
#
#   scripts/smoke.sh                       # uses PUBLIC_BASE_URL from bootstrap.env
#   scripts/smoke.sh https://service.example.com
#
# Checks (one line each; exit code 1 if any fails):
#   GET  /health                                     200, JSON
#   POST /mcp without a token                        401, WWW-Authenticate with resource_metadata
#   GET  /.well-known/oauth-protected-resource/mcp   200, JSON
#   GET  /.well-known/oauth-authorization-server     200, JSON
#   GET  /authorize with an unknown URL client_id    4xx, never 5xx
#
# No credentials are needed or sent. Nothing is written anywhere.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
need_cmd curl

BASE_URL="${1:-${PUBLIC_BASE_URL:-}}"
[[ -n "$BASE_URL" ]] || die "usage: scripts/smoke.sh <base-url>  (or set PUBLIC_BASE_URL in $ENV_FILE)"
BASE_URL="${BASE_URL%/}"
CURL_MAX_TIME="${CURL_MAX_TIME:-30}"   # first request may pay a Cloud Run cold start

failures=0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# request METHOD PATH [curl args...] — sets STATUS, HEADERS (file) and BODY (file).
request() {
  local method="$1" path="$2"; shift 2
  HEADERS="$tmp/headers"; BODY="$tmp/body"
  STATUS="$(curl -sS -o "$BODY" -D "$HEADERS" -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
    -X "$method" "$@" "$BASE_URL$path" 2>"$tmp/curl.err")" || STATUS="000"
}

header() { awk -v name="$1" 'BEGIN{IGNORECASE=1} tolower($1)==tolower(name":") {sub(/^[^:]*: */, ""); sub(/\r$/, ""); print}' "$HEADERS"; }
is_json() { grep -qi '^content-type: *application/json' "$HEADERS" && head -c 1 "$BODY" | grep -q '[{[]'; }
has_resource_metadata() { header WWW-Authenticate | grep -q resource_metadata; }
any() { return 0; }

# check "label" STATUS_REGEX PREDICATE — passes when the status matches and PREDICATE
# (a function above, run against HEADERS/BODY) succeeds.
check() {
  local label="$1" expected="$2" predicate="$3"
  if [[ "$STATUS" =~ ^${expected}$ ]] && "$predicate"; then
    ok "$label"
  else
    local detail="got HTTP $STATUS"
    [[ -s "$tmp/curl.err" ]] && detail="$detail, $(head -c 200 "$tmp/curl.err")"
    warn "$label ($detail)"
    failures=$((failures + 1))
  fi
}

info "Smoke test against $BASE_URL"

request GET /health
check "GET /health -> 200 JSON" 200 is_json

request POST /mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping"}'
check "POST /mcp without token -> 401 with resource_metadata" 401 has_resource_metadata

request GET /.well-known/oauth-protected-resource/mcp
check "GET /.well-known/oauth-protected-resource/mcp -> 200 JSON" 200 is_json

request GET /.well-known/oauth-authorization-server
check "GET /.well-known/oauth-authorization-server -> 200 JSON" 200 is_json

# A Client ID Metadata Document URL that cannot exist: the proxy must answer with an
# OAuth client error, not crash (the first real deploy returned 500 here).
request GET "/authorize?response_type=code&client_id=https%3A%2F%2Fexample.invalid%2Foauth%2Fclient-metadata&redirect_uri=https%3A%2F%2Fexample.invalid%2Fcb&state=smoke&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256"
check "GET /authorize with unknown URL client_id -> 4xx, not 5xx" '4[0-9][0-9]' any

if (( failures > 0 )); then
  die "$failures check(s) failed"
fi
ok "all checks passed"
