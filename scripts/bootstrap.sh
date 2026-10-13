#!/usr/bin/env bash
# From an empty Google account to a running server: creates the project, links billing,
# enables APIs, creates Firestore, the runtime service account and the secrets, then
# pauses for the one thing gcloud cannot do (the OAuth client) and hands off to deploy.sh.
#
# Idempotent: every step checks for the resource first. Safe to re-run.
# Runs on your local machine only. Non-secret answers are remembered in bootstrap.env.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env

info "Pre-flight"
need_cmd gcloud
require_login

# Pick the identity before switching configurations: a fresh configuration has no
# account, and the first credentialed account is not necessarily the right one.
if [[ -z "${ACCOUNT:-}" ]]; then
  echo "Credentialed accounts:"
  gcloud auth list --format='value(account)' | sed 's/^/  - /'
fi
ask ACCOUNT "Google account to use for this deployment" "$(gcloud_active_account)"
gcloud auth list --format='value(account)' | grep -qx "$ACCOUNT" \
  || die "'$ACCOUNT' is not logged in. Run: gcloud auth login $ACCOUNT"

ask PROJECT_ID "New or existing GCP project ID (6-30 chars, lowercase, digits, hyphens)"
ask REGION "Region (Cloud Run + Firestore)" "europe-west1"
ask SERVICE "Cloud Run service name" "slack-guest-reader"
ask SERVICE_ACCOUNT "Runtime service account name" "slack-guest-reader"
ask GCLOUD_CONFIG "Named gcloud configuration to use" "slack-guest-reader"

info "gcloud configuration '$GCLOUD_CONFIG'"
if gcloud config configurations describe "$GCLOUD_CONFIG" >/dev/null 2>&1; then
  ok "configuration exists"
else
  gcloud config configurations create "$GCLOUD_CONFIG" >/dev/null
  ok "created configuration"
fi
gcloud config configurations activate "$GCLOUD_CONFIG" >/dev/null
gcloud config set account "$ACCOUNT" >/dev/null
ok "using account $ACCOUNT"

info "Project"
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  ok "project $PROJECT_ID exists"
else
  if [[ -z "${ORG_ID:-}" ]]; then
    ORGS="$(gcloud organizations list --format='value(ID)' 2>/dev/null || true)"
    if [[ -n "$ORGS" ]]; then
      DEFAULT_ORG="$(echo "$ORGS" | head -n1)"
      ask ORG_ID "Organization ID for the new project (empty for none)" "$DEFAULT_ORG"
    else
      save_var ORG_ID ""
    fi
  fi
  if [[ -n "${ORG_ID:-}" ]]; then
    gcloud projects create "$PROJECT_ID" --organization="$ORG_ID" --name="$PROJECT_ID" >/dev/null
  else
    gcloud projects create "$PROJECT_ID" --name="$PROJECT_ID" >/dev/null
  fi
  ok "created project $PROJECT_ID"
fi
gcloud config set project "$PROJECT_ID" >/dev/null

info "Billing"
if [[ "$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null)" == "True" ]]; then
  ok "billing already linked"
else
  echo "Cloud Run, Firestore and Secret Manager fit the free tier at this usage, but a billing"
  echo "account must be linked anyway."
  gcloud billing accounts list --format='table(name,displayName,open)'
  ask BILLING_ACCOUNT "Billing account ID (billingAccounts/XXXXXX-XXXXXX-XXXXXX or just the ID)"
  gcloud billing projects link "$PROJECT_ID" --billing-account="${BILLING_ACCOUNT#billingAccounts/}" >/dev/null
  ok "billing linked"
fi

info "Budget alert"
if [[ "${SKIP_BUDGET:-no}" != "yes" ]]; then
  ask BUDGET_USD "Monthly budget alert in USD (0 to skip)" "5"
  if [[ "$BUDGET_USD" != "0" ]]; then
    BA="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)')"
    gcloud services enable billingbudgets.googleapis.com >/dev/null
    if gcloud billing budgets list --billing-account="$BA" --format='value(displayName)' 2>/dev/null \
        | grep -qx "$SERVICE budget"; then
      ok "budget exists"
    else
      gcloud billing budgets create --billing-account="$BA" --display-name="$SERVICE budget" \
        --budget-amount="${BUDGET_USD}USD" --filter-projects="projects/$PROJECT_ID" \
        --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 >/dev/null \
        && ok "budget alert at \$$BUDGET_USD created" \
        || warn "could not create a budget (needs Billing Account Administrator); continuing"
    fi
  fi
fi

info "APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com iam.googleapis.com logging.googleapis.com >/dev/null
ok "APIs enabled"

info "Firestore (Native mode)"
if gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
  ok "database exists"
else
  gcloud firestore databases create --location="$REGION" --type=firestore-native >/dev/null
  ok "created database in $REGION"
fi
if gcloud firestore fields ttls list --collection-group=events --format='value(name)' 2>/dev/null | grep -q expires_at; then
  ok "TTL policy on events.expires_at exists"
else
  gcloud firestore fields ttls update expires_at --collection-group=events --enable-ttl --async >/dev/null \
    && ok "TTL policy on events.expires_at requested" || warn "could not set the TTL policy; see docs/DEPLOY.md"
fi

info "Secrets (values can be entered now or later with UPDATE_SECRETS=1 scripts/deploy.sh)"
ensure_secret SLACK_BOT_TOKEN "Slack bot token (xoxb-…)" required
ensure_secret SLACK_SIGNING_SECRET "Slack signing secret (phase 2, may be empty)"
ensure_secret GOOGLE_CLIENT_SECRET "Google OAuth client secret (empty if you have not created the client yet)"

PUBLIC_BASE_URL="$(cloud_run_url)"
save_var PUBLIC_BASE_URL "$PUBLIC_BASE_URL"

if [[ -z "${GOOGLE_CLIENT_ID:-}" ]]; then
  cat <<CHECKLIST

==> Manual step: Google OAuth client (gcloud cannot create a standard web OAuth client)

  1. OAuth consent screen / branding:
       https://console.cloud.google.com/auth/overview?project=$PROJECT_ID
     - User type: Internal if this project is in a Google Workspace organization,
       otherwise External (then add your email as a test user; refresh tokens of
       External apps in Testing mode expire after 7 days).
     - App name: anything, e.g. "Slack guest reader". Support email: yours.
     - Scopes: only "openid" and ".../auth/userinfo.email".
  2. Create credentials -> OAuth client ID -> type "Web application":
       https://console.cloud.google.com/auth/clients/create?project=$PROJECT_ID
     - Authorized redirect URI (exactly):
         $PUBLIC_BASE_URL/auth/callback
  3. Copy the client ID and client secret back here.

CHECKLIST
  ask GOOGLE_CLIENT_ID "Google OAuth client ID"
  UPDATE_SECRETS=1 ensure_secret GOOGLE_CLIENT_SECRET "Google OAuth client secret" required
fi

info "Handing off to deploy.sh"
"$REPO_ROOT/scripts/deploy.sh"
