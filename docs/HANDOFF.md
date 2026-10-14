# Handoff

Keep this current at the end of every session (cloud or local).

## State as of 2026-10-13 (cloud session, no credentials; superseded below)

**Done, all against mocks**

- Repo scaffold: `.gitignore`, gitleaks (pre-commit + CI), ruff, strict mypy, CI.
- Phase 1: config, Firestore/in-memory store, Slack client (pagination, 429, 5xx,
  time budget), rendering, user-name cache, notification rules + polling, the five
  MCP tools, Google OAuth proxy with email allowlist, `/health`.
- Phase 2: `/slack/events` with signature verification, `url_verification`,
  idempotent event storage, allowlist filtering, optional alert webhook;
  `get_notifications` merges events with polling.
- Bootstrap phase: `scripts/bootstrap.sh`, `scripts/deploy.sh`,
  `scripts/teardown.sh` written and syntax-checked, **not yet executed anywhere**.
- Docs: README, ADMIN_REQUEST, DEPLOY, DECISIONS, Slack manifests, `.env.example`.
- 71 tests green; ruff, mypy and gitleaks clean. Smoke test of the real server
  (uvicorn, in-memory store) confirmed `/health`, 401 + `resource_metadata` on
  `/mcp`, both OAuth metadata documents and dynamic client registration.

## Local session 2026-10-13 (first real bootstrap)

**Verified on the local machine**

- `uv sync --group dev`, 71 tests, mypy, ruff, pre-commit with gitleaks: green.
- `docker build .` works (388 MB image); the container starts with placeholder env
  and `/health` answers 200. Cloud Build builds the same Dockerfile in under a minute.
- The deployed service answers 401 + `resource_metadata` on `/mcp`, serves both
  OAuth metadata documents, and `/health` returns 200. The health route was renamed
  from `/health`, which Google's frontend intercepts on Cloud Run (HTML 404, request
  never reaches the container).
- `scripts/bootstrap.sh` on a fresh project: project, billing link, APIs, Firestore
  Native + TTL policy, service account, secrets, OAuth-client checklist, deploy.
  A second run reports everything as existing.

**Script bugs found and fixed on that run** (all in this branch)

- `--set-env-vars` split the comma-separated allowlists; now `^|^`-delimited.
- A fresh gcloud configuration had no account; the scripts now ask which account to
  use and select the configuration via `CLOUDSDK_ACTIVE_CONFIG_NAME` only, so the
  user's shell configuration is untouched.
- `load_env` overrode command-line variables; environment now wins.
- Budget `5USD` is rejected on non-USD billing accounts; amount is now currency-less
  and the prompt shows the account currency.
- Cloud Run refuses a secret without a version; `deploy.sh` checks first and deploys
  without the events receiver when the signing secret is still empty.
- A non-IANA `TIMEZONE` only failed at container start; now checked before deploy.
- A stray keystroke was stored as the bot token; secret prompts now validate shape.
- `.gcloudignore` added so `bootstrap.env`, tests and docs never reach Cloud Build.

**Things to know on a real account**

- Personal billing accounts have a quota of linked projects (5 here); the link fails
  with `Cloud billing quota exceeded` until a project is unlinked or the quota raised.
- Budget creation can fail with `INVALID_ARGUMENT` even with the fix if the account
  lacks Billing Account Costs Manager; the script warns and continues.

**Not verified yet**

1. `FirestoreStore` paths against the real database under real traffic. They now
   pass against the Firestore emulator (see the 2026-10-14 section below), which
   does not cover TTL deletion or production IAM.
2. The full OAuth flow from Claude web and mobile. First attempt reached
   `/authorize` and failed on Firestore document IDs (fixed, see DECISIONS); the
   retry after that fix is the next manual step. The same request shape is now
   covered by `tests/test_oauth_authorize.py` against the in-memory backend.
3. All tools against the real workspace: the admin has not created the Slack app yet,
   the deployed service runs with a placeholder `xoxb-PLACEHOLDER-…` token and every
   Slack tool returns `invalid_auth` until the real token is stored.
4. Phase 2 against real Slack retries and the 3 s ack window.
5. `teardown.sh`.

## Local session 2026-10-14 (closing the test gaps)

Two production bugs escaped the mock-only suite (`/healthz`, URL-shaped `client_id`
as a Firestore document ID). Now covered:

- `tests/test_firestore_emulator.py` (marker `emulator`, skipped without
  `FIRESTORE_EMULATOR_HOST`): `FirestoreStore` users cache (TTL, expiry), cursors,
  my-threads `ArrayUnion`, events `create()` idempotency and the `event_ts_num`
  ordered query; `build_oauth_backend` round trips of Claude's
  `https://claude.ai/oauth/mcp-oauth-client-metadata` key plus `/`, `.`, `..` and
  `__x__` edge cases, asserting the document IDs Firestore sees contain no `/`; the
  Fernet-wrapped stack stores nothing in clear text.
- `tests/test_oauth_authorize.py`: `/authorize` with a Client ID Metadata Document
  client through consent to the Google redirect; unknown URL, mismatched document,
  foreign `redirect_uri`, unregistered scope, random client all end in 4xx or an
  OAuth error redirect, never 5xx. Plus both metadata documents and 401 on `/mcp`.
- `scripts/smoke.sh`, run by `deploy.sh` after every deploy (`SKIP_SMOKE=1` skips).
- CI: an `emulator` job (gcloud emulator, fails if the tests were skipped) and a
  `shellcheck` job; shellcheck also runs in pre-commit (`.shellcheckrc` follows the
  sourced `lib.sh`).

Running the emulator tests locally (needs Java and the gcloud component
`cloud-firestore-emulator`):

```bash
gcloud components install cloud-firestore-emulator
gcloud emulators firestore start --host-port=127.0.0.1:8681 \
  --database-mode=firestore-native --project=emulator-test &
FIRESTORE_EMULATOR_HOST=127.0.0.1:8681 uv run pytest -m emulator
```

## Next steps, in order (local machine)

```bash
# Once the admin sends the bot token (see docs/ADMIN_REQUEST.md):
UPDATE_SECRETS=1 scripts/deploy.sh            # paste the token, Enter on the others
# -> deploy.sh ends with scripts/smoke.sh; then in Claude: list_channels, get_messages,
#    get_thread, search_messages, get_notifications

# Phase 2
UPDATE_SECRETS=1 ENABLE_EVENTS=yes scripts/deploy.sh   # paste the signing secret
# -> admin adds Event Subscriptions (slack/manifest.yaml) with https://<service>/slack/events
```

Local run without GCP: `cp .env.example .env`, fill the Slack values and a Google
client with redirect URI `http://localhost:8080/auth/callback`, keep
`FIRESTORE_ENABLED=false`, then `uv run slack-guest-reader`.

## Open questions / things to decide after real-world testing

- Whether to restrict `allowed_client_redirect_uris` to Claude's callback URLs once
  they are known from the logs (`Client registered with redirect_uri`). Left open
  (all URIs allowed, consent page on) so mobile and desktop clients are not broken.
- `max_messages_per_channel` (400) and `max_threads_per_channel` (40) in the poller,
  and the 500/30 limits in `search_messages`: tune after seeing real channel volume
  against the 25 s tool budget.
- The Cloud Run request timeout is 60 s; `get_notifications` has a 40 s budget.

## Rules reminder

No tokens, IDs, emails or URLs of the real deployment in the repo. `bootstrap.env`
and `.env` stay local. Run `git diff --cached` before every commit.
