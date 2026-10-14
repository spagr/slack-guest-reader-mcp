# Deploying to Google Cloud Run

Everything here runs on your local machine with the `gcloud` CLI. Nothing in this
guide needs a GCP-side MCP server, and no step writes a secret to disk in the repo.

## 0. Prerequisites

- A Google account. If it belongs to a Google Workspace organisation, you will be able
  to choose the **Internal** OAuth user type, which is strongly recommended.
- `gcloud` installed (https://cloud.google.com/sdk/docs/install) and `gcloud auth login`.
- From the Slack admin: the bot token (`xoxb-…`) and, for phase 2, the signing secret.
  See `docs/ADMIN_REQUEST.md`.
- Your Slack user ID in that workspace (profile -> "…" -> Copy member ID) and the IDs of
  the agreed channels (channel details -> bottom of the "About" tab).

## 1. From zero: `scripts/bootstrap.sh`

```bash
scripts/bootstrap.sh
```

It is idempotent and asks only for what it cannot find in `bootstrap.env`
(gitignored; holds non-secret answers so re-runs and `deploy.sh` reuse them). In order:

1. Pre-flight: `gcloud` present, logged in, lets you confirm or change the account
   (useful when several accounts are logged in).
2. Creates the project (with `--organization` when you have one) inside a named
   gcloud configuration. The scripts select it with `CLOUDSDK_ACTIVE_CONFIG_NAME`
   for their own process only, so your shell's active configuration and other
   projects are untouched.
3. Links a billing account. Cloud Run, Firestore and Secret Manager fit the free tier
   at this usage, but billing must be linked.
4. Creates a budget alert (default 5 in the billing account's currency; answer `0`
   to skip).
5. Enables the APIs: run, cloudbuild, artifactregistry, secretmanager, firestore, iam,
   logging.
6. Creates the Firestore database (Native mode, your region) and a TTL policy on
   `events.expires_at` (30 days by default via `EVENTS_TTL_DAYS`).
7. Creates the runtime service account with `roles/secretmanager.secretAccessor` and
   `roles/datastore.user` only.
8. Creates the secrets `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
   `GOOGLE_CLIENT_SECRET` and prompts for values with hidden input (Enter to skip).
9. Pauses for the manual OAuth-client step below, then runs `scripts/deploy.sh`.
10. Prints the service URL, the OAuth redirect URI, the Slack events URL and the MCP
    URL for Claude.

## 2. Manual checklist: Google OAuth client

`gcloud` cannot create a standard "Web application" OAuth client, so the script
prints these steps with deep links for your project:

1. **Consent screen / branding**: `https://console.cloud.google.com/auth/overview?project=PROJECT_ID`
   - User type **Internal** if the project is in a Workspace organisation.
     **External** apps left in "Testing" mode expire refresh tokens after 7 days,
     which means re-authorising the connector weekly.
   - App name (any), support email, and only the scopes `openid` and
     `.../auth/userinfo.email`.
2. **Credentials**: `https://console.cloud.google.com/auth/clients/create?project=PROJECT_ID`
   - Type "Web application".
   - Authorised redirect URI exactly `https://<service-url>/auth/callback`
     (the script prints it; Cloud Run URLs are deterministic:
     `https://SERVICE-PROJECTNUMBER.REGION.run.app`).
3. Paste the client ID and the client secret back into the script. The secret goes
   straight into Secret Manager.

## 3. Deploy or redeploy: `scripts/deploy.sh`

```bash
scripts/deploy.sh                     # build from source with Cloud Build and deploy
UPDATE_SECRETS=1 scripts/deploy.sh    # also offer to enter new secret versions
ENABLE_EVENTS=yes scripts/deploy.sh   # override a remembered answer for this run
```

Answers are remembered in `bootstrap.env`. A variable set in the environment wins over
the remembered value for that run; edit the file to change it permanently. The upload
to Cloud Build follows `.gcloudignore`, so `bootstrap.env`, tests and docs stay local.

The service runs with `--min-instances=0`, 1 vCPU, 512 MiB, concurrency 20, 60 s
request timeout, startup CPU boost and `--allow-unauthenticated` (Claude's servers
call it; protection is OAuth on `/mcp` and signature verification on `/slack/events`).

Scaling to zero means the first request after idle pays a cold start. Without CPU
boost that took about 10 s on the first real deploy, which is exactly the timeout of
the "Checking the server" step in Claude's connector dialog; with boost it is a few
seconds. If the dialog still says "Couldn't check the server", the instance is now
warm: cancel and add the connector again.

Environment on the service: `SLACK_CHANNEL_ALLOWLIST`, `ME_USER_ID`, `ALLOWED_EMAILS`,
`GOOGLE_CLIENT_ID`, `PUBLIC_BASE_URL`, `TIMEZONE` (an IANA name such as
`Europe/Prague`, checked before deploying), `FIRESTORE_ENABLED=true`,
`GOOGLE_CLOUD_PROJECT`, plus the secrets mounted as env vars. The script refuses to
deploy while `SLACK_BOT_TOKEN` or `GOOGLE_CLIENT_SECRET` has no version. Answer `yes`
to "Enable the Slack Events receiver" once the signing secret is stored; if it is
still empty the service is deployed without the receiver and a warning says so.

### No bot token yet?

You can deploy and test the Google sign-in and the Claude connector before the
workspace admin has created the Slack app. When `SLACK_BOT_TOKEN` is empty,
`deploy.sh` offers to store an obviously fake placeholder (`xoxb-PLACEHOLDER-…`) so
the container starts. Every Slack tool then returns `invalid_auth` until you store the
real token with `UPDATE_SECRETS=1 scripts/deploy.sh` and redeploy.

### Smoke test

`deploy.sh` ends by running `scripts/smoke.sh` against the new revision (set
`SKIP_SMOKE=1` to skip). You can run it any time, for example after changing the
Google OAuth client or when Claude's connector dialog misbehaves:

```bash
scripts/smoke.sh                              # PUBLIC_BASE_URL from bootstrap.env
scripts/smoke.sh https://your-service.example.com
```

It sends only unauthenticated requests and prints one line per check: `/health` is
200 JSON, `POST /mcp` without a token is 401 with `resource_metadata` in
`WWW-Authenticate`, both OAuth metadata documents are 200 JSON, and `/authorize` with
an unknown URL-shaped `client_id` (how Claude identifies itself, see
`docs/DECISIONS.md`) answers 4xx rather than 5xx. The exit code is non-zero if any
check fails. The first request may take a few seconds on a cold start; raise
`CURL_MAX_TIME` (default 30 s) on a slow link.

### Error reporting with Sentry (optional)

Create a Python project in Sentry (the "Cloud Run" or plain Python platform; not the
Cloud Functions one, whose `GcpIntegration` does not apply) and store its DSN with
`UPDATE_SECRETS=1 scripts/deploy.sh` at the `SENTRY_DSN` prompt. The next deploy
mounts it and the server reports unhandled exceptions and 5xx responses. By design
it sends no PII, no request bodies and no local variables, so Slack message text
never leaves the service. Leave the secret empty to run without Sentry.

## 4. Connect in Claude

Settings -> Connectors -> Add custom connector -> paste `https://<service-url>/mcp`.
Claude discovers the OAuth metadata, registers itself, and sends you through Google
sign-in. Only emails in `ALLOWED_EMAILS` (verified by Google) get through.

## 5. Phase 2: Slack events

1. Store the signing secret (`UPDATE_SECRETS=1 scripts/deploy.sh`) and answer `yes` to
   enabling events; redeploy.
2. Ask the admin to add Event Subscriptions (or create the app from
   `slack/manifest.yaml`, which already contains them) with request URL
   `https://<service-url>/slack/events` and bot events `message.channels`,
   `message.groups`, `reaction_added`. Slack sends a `url_verification` challenge;
   the server answers it only with a valid signature.

## 6. Teardown

```bash
scripts/teardown.sh            # service, secrets, service account
scripts/teardown.sh --project  # the whole project
```

Both require typing the project ID. The Firestore database and the OAuth client are
left for you to delete in the Console (the script prints the links).

## Troubleshooting

- Claude's connector dialog says "Couldn't check the server": almost always a cold
  start that exceeded the dialog's 10 s timeout. Retry; the instance is warm now.
- `Configuration error:` on startup: a required env var is missing or malformed. The
  message names it. `gcloud run services logs read SERVICE --region REGION`.
- Google sign-in succeeds but Claude says it could not finish connecting: the token
  exchange was refused because the email is not in `ALLOWED_EMAILS` or is unverified.
  The log line says `rejected sign-in`. With an **Internal** consent screen only
  accounts of that Workspace organisation can sign in, so `ALLOWED_EMAILS` must list
  one of those, not an address from another domain.
- `redirect_uri_mismatch` from Google: the redirect URI on the OAuth client must be
  exactly `PUBLIC_BASE_URL/auth/callback`.
- Everyone logged out after a redeploy: `FIRESTORE_ENABLED` is false, or the Google
  client secret was rotated (keys derive from it; set `JWT_SIGNING_KEY` and
  `STORAGE_ENCRYPTION_KEY` explicitly if you rotate secrets often).
- Slack `not_in_channel`: the bot was not invited to that channel.
- Slack `missing_scope`: the app was created without one of the six scopes.
