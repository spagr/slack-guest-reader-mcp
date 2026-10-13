# Handoff

Keep this current at the end of every session (cloud or local).

## State as of 2026-10-13 (cloud session, no credentials)

**Done, all against mocks**

- Repo scaffold: `.gitignore`, gitleaks (pre-commit + CI), ruff, strict mypy, CI.
- Phase 1: config, Firestore/in-memory store, Slack client (pagination, 429, 5xx,
  time budget), rendering, user-name cache, notification rules + polling, the five
  MCP tools, Google OAuth proxy with email allowlist, `/healthz`.
- Phase 2: `/slack/events` with signature verification, `url_verification`,
  idempotent event storage, allowlist filtering, optional alert webhook;
  `get_notifications` merges events with polling.
- Bootstrap phase: `scripts/bootstrap.sh`, `scripts/deploy.sh`,
  `scripts/teardown.sh` written and syntax-checked, **not yet executed anywhere**.
- Docs: README, ADMIN_REQUEST, DEPLOY, DECISIONS, Slack manifests, `.env.example`.
- 71 tests green; ruff, mypy and gitleaks clean. Smoke test of the real server
  (uvicorn, in-memory store) confirmed `/healthz`, 401 + `resource_metadata` on
  `/mcp`, both OAuth metadata documents and dynamic client registration.

**Not verified (needs the local machine)**

1. `docker build .` (Cloud Build runs it during `gcloud run deploy --source`).
2. `FirestoreStore` in `store.py` and `key_value.aio.stores.firestore` against a real
   database, including the `ArrayUnion` and `create()` (AlreadyExists) paths and the
   `events` query, which needs a single-field index on `event_ts_num` (Firestore
   creates single-field indexes automatically; `expires_at` TTL policy is set by
   `bootstrap.sh`).
3. The full OAuth flow with a real Google client, from Claude web and mobile.
4. All tools against the real workspace, in particular rendering of bot messages,
   files and `latest_reply` on threads.
5. Phase 2 against real Slack retries and the 3 s ack window.
6. `bootstrap.sh` on a fresh project, a second idempotent run, and `teardown.sh`.

## Next steps, in order (local machine)

```bash
git clone <remote> && cd slack-guest-reader-mcp
uv sync --group dev && uv run pre-commit install
uv run pytest && uv run mypy && uv run ruff check .
docker build -t slack-guest-reader .          # verify the image builds

# Phase 1 end-to-end
gcloud auth login
scripts/bootstrap.sh                          # follow the printed checklist
# -> add https://<service>/mcp in Claude -> Settings -> Connectors
# -> try list_channels, get_messages, get_thread, search_messages, get_notifications

# Phase 2
UPDATE_SECRETS=1 scripts/deploy.sh            # enter the signing secret, answer "yes" to events
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
