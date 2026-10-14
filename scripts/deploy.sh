#!/usr/bin/env bash
# Idempotent deploy to Cloud Run. Runs on your local machine with `gcloud auth login`.
#
#   scripts/deploy.sh            # prompts for anything missing, remembers it in bootstrap.env
#   UPDATE_SECRETS=1 scripts/deploy.sh   # also offer to enter new secret versions
#
# Reads non-secret config from bootstrap.env (gitignored) or prompts for it.
# Secrets are read from hidden prompts and stored only in Secret Manager.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_login

ask PROJECT_ID "GCP project ID"
ask REGION "Cloud Run region" "europe-west1"
ask SERVICE "Cloud Run service name" "slack-guest-reader"
ask SERVICE_ACCOUNT "Runtime service account name" "slack-guest-reader"
ask SLACK_CHANNEL_ALLOWLIST "Slack channel IDs to allow (comma-separated)"
ask ME_USER_ID "Your Slack user ID in that workspace (U…)"
ask ALLOWED_EMAILS "Google account emails allowed to connect (comma-separated)"
ask GOOGLE_CLIENT_ID "Google OAuth client ID"
ask TIMEZONE "Timezone for rendered times (IANA name, e.g. Europe/Prague)" "UTC"
ask ENABLE_EVENTS "Enable the Slack Events receiver (phase 2)? yes/no" "no"

if [[ -d /usr/share/zoneinfo && ! -f "/usr/share/zoneinfo/$TIMEZONE" ]]; then
  die "TIMEZONE '$TIMEZONE' is not an IANA zone name (try Europe/Prague). Fix it in $ENV_FILE"
fi

use_gcloud_config
SA_EMAIL="${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"

info "Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com iam.googleapis.com logging.googleapis.com >/dev/null
ok "APIs enabled"

info "Service account"
if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  ok "$SA_EMAIL exists"
else
  gcloud iam service-accounts create "$SERVICE_ACCOUNT" --display-name="Slack guest reader runtime" >/dev/null
  ok "created $SA_EMAIL"
fi
for role in roles/secretmanager.secretAccessor roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA_EMAIL" \
    --role="$role" --condition=None >/dev/null
done
ok "roles bound (secretmanager.secretAccessor, datastore.user)"

info "Secrets"
ensure_secret SLACK_BOT_TOKEN "Slack bot token (xoxb-…)" required
ensure_secret GOOGLE_CLIENT_SECRET "Google OAuth client secret" required
ensure_secret SLACK_SIGNING_SECRET "Slack signing secret (phase 2, may be empty for now)"
ensure_secret SENTRY_DSN "Sentry DSN (optional error reporting, may be empty)"

PUBLIC_BASE_URL="$(cloud_run_url)"
save_var PUBLIC_BASE_URL "$PUBLIC_BASE_URL"

# Cloud Run refuses to mount a secret that has no version, so check before deploying.
offer_placeholder_bot_token
secret_has_version GOOGLE_CLIENT_SECRET \
  || die "secret GOOGLE_CLIENT_SECRET has no value yet. Run: UPDATE_SECRETS=1 scripts/deploy.sh"
SECRETS="SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET:latest"
if [[ "$ENABLE_EVENTS" == "yes" ]]; then
  if secret_has_version SLACK_SIGNING_SECRET; then
    SECRETS="$SECRETS,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest"
  else
    warn "ENABLE_EVENTS=yes but SLACK_SIGNING_SECRET has no value; deploying without the events receiver"
    warn "store it with UPDATE_SECRETS=1 scripts/deploy.sh and redeploy to enable phase 2"
    ENABLE_EVENTS=no
  fi
fi
if secret_has_version SENTRY_DSN; then
  SECRETS="$SECRETS,SENTRY_DSN=SENTRY_DSN:latest"
  ok "Sentry enabled"
fi
# Joined with '|' because the allowlists themselves contain commas; `^|^` below tells
# gcloud to split on '|' instead of the default ','.
ENV_VARS="SLACK_CHANNEL_ALLOWLIST=${SLACK_CHANNEL_ALLOWLIST}|ME_USER_ID=${ME_USER_ID}"
ENV_VARS="$ENV_VARS|ALLOWED_EMAILS=${ALLOWED_EMAILS}|GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}"
ENV_VARS="$ENV_VARS|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|TIMEZONE=${TIMEZONE}|FIRESTORE_ENABLED=true"
ENV_VARS="$ENV_VARS|GOOGLE_CLOUD_PROJECT=${PROJECT_ID}|LOG_LEVEL=${LOG_LEVEL:-INFO}"

info "Deploying $SERVICE to $REGION (Cloud Build from source)"
gcloud run deploy "$SERVICE" \
  --source "$REPO_ROOT" \
  --region "$REGION" \
  --platform managed \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 2 \
  --cpu 1 --memory 512Mi --concurrency 20 --timeout 60 \
  --cpu-boost \
  --set-env-vars "^|^${ENV_VARS}" \
  --set-secrets "$SECRETS" \
  --quiet
ok "deployed"

if [[ "${SKIP_SMOKE:-0}" == "1" ]]; then
  warn "SKIP_SMOKE=1: not running scripts/smoke.sh"
else
  "$REPO_ROOT/scripts/smoke.sh" "$PUBLIC_BASE_URL" \
    || die "the deployed service failed the smoke test (see docs/DEPLOY.md, 'Smoke test')"
fi

cat <<SUMMARY

Done.

  Service URL:                 $PUBLIC_BASE_URL
  Health check:                $PUBLIC_BASE_URL/health
  OAuth redirect URI (Google): $PUBLIC_BASE_URL/auth/callback
  Slack events request URL:    $PUBLIC_BASE_URL/slack/events
  MCP URL for Claude:          $PUBLIC_BASE_URL/mcp

In Claude: Settings -> Connectors -> Add custom connector -> paste the MCP URL.
SUMMARY
