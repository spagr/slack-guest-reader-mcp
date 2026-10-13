#!/usr/bin/env bash
# Remove what bootstrap.sh created. Never run automatically; asks for the project ID.
#
#   scripts/teardown.sh            # delete the service, secrets and service account
#   scripts/teardown.sh --project  # delete the whole project instead

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_login

ask PROJECT_ID "GCP project ID"
ask REGION "Cloud Run region" "europe-west1"
ask SERVICE "Cloud Run service name" "slack-guest-reader"
ask SERVICE_ACCOUNT "Runtime service account name" "slack-guest-reader"
use_gcloud_config

echo
warn "This permanently deletes resources in project $PROJECT_ID."
read -r -p "Type the project ID to confirm: " typed
[[ "$typed" == "$PROJECT_ID" ]] || die "confirmation did not match"

if [[ "${1:-}" == "--project" ]]; then
  gcloud projects delete "$PROJECT_ID" --quiet
  ok "project deletion requested (30-day grace period applies)"
  exit 0
fi

if gcloud run services describe "$SERVICE" --region "$REGION" >/dev/null 2>&1; then
  gcloud run services delete "$SERVICE" --region "$REGION" --quiet && ok "deleted service $SERVICE"
else
  ok "service $SERVICE already gone"
fi
for s in SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GOOGLE_CLIENT_SECRET; do
  if secret_exists "$s"; then gcloud secrets delete "$s" --quiet && ok "deleted secret $s"; fi
done
SA_EMAIL="${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts delete "$SA_EMAIL" --quiet && ok "deleted service account"
fi
echo
echo "Kept: the Firestore database (delete it in the Console if you want the OAuth state and"
echo "events gone: https://console.cloud.google.com/firestore/databases?project=$PROJECT_ID)"
echo "and the Google OAuth client (delete it under APIs & Services -> Credentials)."
