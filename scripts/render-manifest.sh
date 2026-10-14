#!/usr/bin/env bash
# Render the Slack app manifests with this deployment's service URL.
#
#   scripts/render-manifest.sh            # uses PUBLIC_BASE_URL from bootstrap.env
#   PUBLIC_BASE_URL=https://... scripts/render-manifest.sh
#
# Writes slack/manifest.local.yaml (phase 2, with the real /slack/events URL). The
# file matches the gitignored *.local.* pattern, so the real URL never enters git.
# Send it to the admin together with docs/ADMIN_REQUEST.md.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env

[[ -n "${PUBLIC_BASE_URL:-}" ]] || die "PUBLIC_BASE_URL is not set; run scripts/deploy.sh first or export it"
OUT="$REPO_ROOT/slack/manifest.local.yaml"
# Substitute the URL and drop the header comments that explain the placeholder.
sed -e "s#https://<your-service>#${PUBLIC_BASE_URL}#g" -e "/<your-service>/d" \
  "$REPO_ROOT/slack/manifest.yaml" > "$OUT"
grep -q '<your-service>' "$OUT" && die "placeholder still present in $OUT"
ok "wrote $OUT (events request URL: ${PUBLIC_BASE_URL}/slack/events)"
echo "Reminder: Slack verifies that URL when the manifest is saved, so the service must"
echo "be deployed with ENABLE_EVENTS=yes and the signing secret stored first. For the"
echo "first install without events, send slack/manifest-phase1.yaml instead."
