#!/usr/bin/env bash
# Shared helpers for bootstrap.sh, deploy.sh and teardown.sh. Sourced, not executed.
# Nothing here prints secret values.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/bootstrap.env}"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m ✗\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "'$1' is not installed"; }

# Load non-secret values collected by earlier runs (gitignored file).
load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  # Values already set in the environment win, so `ENABLE_EVENTS=yes scripts/deploy.sh`
  # overrides what an earlier run remembered.
  local line name value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    name="${line%%=*}"; value="${line#*=}"
    if [[ -z "${!name+x}" ]]; then export "$name=$value"; fi
  done < "$ENV_FILE"
}

# save_var NAME VALUE — persist a non-secret value to bootstrap.env (idempotent).
save_var() {
  local name="$1" value="$2"
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  if grep -q "^${name}=" "$ENV_FILE"; then
    sed -i.bak "s|^${name}=.*|${name}=${value}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  fi
  export "$name=$value"
}

# ask VAR "prompt" [default] — prompt unless already set in the environment.
ask() {
  local var="$1" prompt="$2" default="${3:-}" value
  if [[ -n "${!var:-}" ]]; then return; fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value; value="${value:-$default}"
  else
    while [[ -z "${value:-}" ]]; do read -r -p "$prompt: " value; done
  fi
  save_var "$var" "$value"
}

# ask_secret VAR "prompt" — hidden input, never saved to disk or echoed.
ask_secret() {
  local var="$1" prompt="$2" value
  if [[ -n "${!var:-}" ]]; then return; fi
  read -r -s -p "$prompt (input hidden, Enter to skip): " value; echo
  export "$var=$value"
}

confirm() {
  local answer
  read -r -p "$1 [y/N]: " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

gcloud_active_account() { gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null; }

# use_gcloud_config — run every gcloud call in the named configuration without
# touching the user's active one. CLOUDSDK_ACTIVE_CONFIG_NAME is honoured by gcloud and
# lives only in this process, so the shell's configuration is unchanged afterwards.
use_gcloud_config() {
  ask GCLOUD_CONFIG "Named gcloud configuration to use" "slack-guest-reader"
  if gcloud config configurations describe "$GCLOUD_CONFIG" >/dev/null 2>&1; then
    ok "gcloud configuration '$GCLOUD_CONFIG' exists"
  else
    gcloud config configurations create "$GCLOUD_CONFIG" --no-activate >/dev/null 2>&1
    ok "created gcloud configuration '$GCLOUD_CONFIG'"
  fi
  export CLOUDSDK_ACTIVE_CONFIG_NAME="$GCLOUD_CONFIG"
  # `config set` only writes the property; it warns on stderr when the project does not
  # exist yet (bootstrap creates it right after), so keep both quiet.
  if [[ -n "${ACCOUNT:-}" ]]; then gcloud config set account "$ACCOUNT" >/dev/null 2>&1; fi
  if [[ -n "${PROJECT_ID:-}" ]]; then gcloud config set project "$PROJECT_ID" >/dev/null 2>&1; fi
}

require_login() {
  need_cmd gcloud
  local account; account="$(gcloud_active_account)"
  [[ -n "$account" ]] || die "Not logged in. Run: gcloud auth login"
  info "Active gcloud account: $account"
}

# secret_exists NAME
secret_exists() { gcloud secrets describe "$1" --project="$PROJECT_ID" >/dev/null 2>&1; }

# secret_has_version NAME — true when at least one enabled version exists.
secret_has_version() {
  [[ -n "$(gcloud secrets versions list "$1" --project="$PROJECT_ID" --filter='state:ENABLED' \
      --format='value(name)' --limit=1 2>/dev/null)" ]]
}

# ensure_secret NAME "prompt" [required]
# Creates the secret if missing and adds a version from hidden input when a value
# is given. Existing secrets are left untouched unless UPDATE_SECRETS=1.
ensure_secret() {
  local name="$1" prompt="$2" required="${3:-}"
  if secret_exists "$name"; then
    if [[ "${UPDATE_SECRETS:-0}" != "1" ]]; then ok "secret $name exists"; return; fi
  else
    gcloud secrets create "$name" --project="$PROJECT_ID" --replication-policy=automatic >/dev/null
    ok "created secret $name"
  fi
  local value
  while true; do
    read -r -s -p "$prompt (input hidden, Enter to skip): " value; echo
    if [[ -z "$value" ]]; then
      [[ -z "$required" ]] || warn "$name has no value yet; the service will not start until it does"
      return
    fi
    secret_looks_valid "$name" "$value" && break
    warn "that does not look like a $name (paste the whole value, nothing is echoed); try again or Enter to skip"
  done
  printf '%s' "$value" | gcloud secrets versions add "$name" --project="$PROJECT_ID" --data-file=- >/dev/null
  unset value
  ok "stored a new version of $name"
}

# offer_placeholder_bot_token — when the admin has not issued the Slack app yet, store an
# obviously fake xoxb- value so the service starts and the OAuth flow and the Claude
# connector can be tested. Slack tools return invalid_auth until the real token replaces it.
offer_placeholder_bot_token() {
  secret_has_version SLACK_BOT_TOKEN && return 0
  echo "SLACK_BOT_TOKEN has no value. Without it the container refuses to start."
  if confirm "Store a placeholder token so you can test deploy + OAuth before the admin sends the real one?"; then
    printf 'xoxb-PLACEHOLDER-replace-with-the-real-bot-token-0000' \
      | gcloud secrets versions add SLACK_BOT_TOKEN --project="$PROJECT_ID" --data-file=- >/dev/null
    ok "stored a placeholder SLACK_BOT_TOKEN (replace it later with UPDATE_SECRETS=1 scripts/deploy.sh)"
  else
    die "secret SLACK_BOT_TOKEN has no value yet. Run: UPDATE_SECRETS=1 scripts/deploy.sh"
  fi
}

# secret_looks_valid NAME VALUE — cheap shape checks so a stray keystroke or a wrong
# token type is caught at the prompt instead of at container start. Prints nothing.
secret_looks_valid() {
  local name="$1" value="$2"
  case "$name" in
    SLACK_BOT_TOKEN) [[ "$value" == xoxb-* && ${#value} -ge 20 ]] ;;
    *) [[ ${#value} -ge 8 ]] ;;
  esac
}

cloud_run_url() {
  local number; number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  printf 'https://%s-%s.%s.run.app' "$SERVICE" "$number" "$REGION"
}
