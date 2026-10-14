# slack-guest-reader-mcp

A small, self-hostable **remote MCP server** that gives Claude (web, desktop, mobile)
**read-only** access to a handful of channels in a **Slack workspace you don't own**,
typically a client's or partner's workspace where you are a guest.

- Reads through an official Slack app the **workspace admin creates** from a shipped
  manifest (bot token, six read scopes). No user tokens, cookies, scraping or
  undocumented endpoints, ever.
- Runs on **Google Cloud Run** (scales to zero, fits the free tier), protected by
  **OAuth with Google sign-in** restricted to an email allowlist, so it plugs into
  Claude as a custom connector.
- Answers "what's new for me?" without re-downloading history: mentions, replies to
  your messages, activity in threads you took part in, reactions to your messages.

The data belongs to the workspace owner. Use this only with their knowledge and
consent; `docs/ADMIN_REQUEST.md` is a template for asking.

## Tools

| Tool | What it returns |
|---|---|
| `list_channels()` | Allowlisted channels: id, name, topic, purpose, member count |
| `get_messages(channel, since?, until?, limit=50)` | Top-level messages, newest first. `since`/`until` take ISO 8601 or `"3d"`, `"12h"` |
| `get_thread(channel, thread_ts, limit=100)` | Parent plus replies, oldest first |
| `search_messages(query, channels?, since="14d", limit=30)` | Case-insensitive keyword / `"phrase"` search over recent history, including replies in active threads |
| `get_notifications(since?, limit=50)` | Mentions, replies to you, thread activity, reactions. Without `since`: what's new since the last call |

Every message carries `channel`, `ts`, `thread_ts`, `author` (id + name), `time` in
your `TIMEZONE`, rendered `text` (`@Name`, `#channel`, links with URLs), `reply_count`,
`reactions` (with whether you reacted), file **metadata only**, and a `permalink`.
Every tool has a `limit` with a hard maximum of 200. The channel allowlist is enforced
server-side on every call, even if the bot was invited elsewhere.

## Architecture

```
Claude (web/desktop/mobile)
   │  MCP over HTTPS + OAuth bearer
   ▼
Cloud Run service  ──►  Slack Web API (bot token, read-only)
   │   ▲
   │   └── POST /slack/events  ◄── Slack Events API (phase 2, signed requests)
   ▼
Firestore (oauth state, user cache, cursors, events)
```

Endpoints: `/mcp` (Streamable HTTP, OAuth-protected), the OAuth endpoints and
`/.well-known/*` metadata that the MCP authorisation spec requires, `/slack/events`
(signature-verified, phase 2) and `/health` (not `/healthz`, which Cloud Run's
frontend intercepts).

## Setup, end to end

1. **Slack app** (workspace admin, 5 minutes): create from `slack/manifest.yaml`
   (or `manifest-phase1.yaml` before the server exists), install, `/invite` the bot
   to the agreed channels, hand over the bot token and signing secret through a
   secure channel. Template: `docs/ADMIN_REQUEST.md`.

   The app must be an **internal app created inside their workspace**. Slack caps
   `conversations.history` / `conversations.replies` at 1 request per minute for
   apps distributed outside the Marketplace; internal customer-built apps keep the
   normal Tier 3 limits (Slack docs, June 2025; see `docs/DECISIONS.md`).

2. **Google Cloud** (you, 15 minutes): `scripts/bootstrap.sh` creates the project,
   billing link, budget alert, APIs, Firestore, service account and secrets, then
   pauses for the one manual step: the OAuth consent screen and a "Web application"
   OAuth client with redirect URI `https://<service>/auth/callback`. Choose user type
   **Internal** if the project belongs to a Google Workspace organisation; External
   apps in "Testing" mode expire refresh tokens after 7 days. Then it deploys with
   `scripts/deploy.sh`. Details: `docs/DEPLOY.md`.

3. **Claude**: Settings -> Connectors -> Add custom connector ->
   `https://<service>/mcp`. Sign in with an email from `ALLOWED_EMAILS`.

## Configuration

| Var | Required | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | yes | `xoxb-…`, from Secret Manager |
| `SLACK_SIGNING_SECRET` | phase 2 | enables `/slack/events` |
| `SLACK_CHANNEL_ALLOWLIST` | yes | comma-separated channel IDs |
| `ME_USER_ID` | yes | your Slack user ID in that workspace |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | yes | secret from Secret Manager |
| `ALLOWED_EMAILS` | yes | comma-separated, verified Google emails |
| `PUBLIC_BASE_URL` | yes | Cloud Run URL or custom domain |
| `JWT_SIGNING_KEY`, `STORAGE_ENCRYPTION_KEY` | no | derived from the client secret when unset; rotating the client secret then logs connectors out |
| `FIRESTORE_ENABLED` | no | default `true`; `false` = in-memory store for local dev |
| `FIRESTORE_DATABASE`, `GOOGLE_CLOUD_PROJECT` | no | default `(default)` / from ADC |
| `TIMEZONE` | no | default `UTC` |
| `THREAD_LOOKBACK_DAYS` | no | default `14` |
| `NOTIFY_THREAD_ACTIVITY` | no | default `true` |
| `NOTIFY_GROUP_MENTIONS` | no | default `false` (`@here`/`@channel`) |
| `EVENTS_TTL_DAYS` | no | default `30` |
| `ALERT_WEBHOOK_URL` | no | phase 2: JSON POST for mentions and replies to you |
| `SENTRY_DSN` | no | error reporting to Sentry; unset disables it. Sent without PII, request bodies or local variables |
| `SENTRY_ENVIRONMENT` | no | default `production` |
| `LOG_LEVEL` | no | default `INFO` |

Configuration is validated at startup; a missing or malformed value stops the
process with a readable list of problems. `.env.example` has placeholders.

## Local development

```bash
uv sync --group dev
uv run pre-commit install          # gitleaks, ruff and shellcheck on every commit
cp .env.example .env               # fill in placeholders; .env is gitignored
uv run slack-guest-reader          # http://localhost:8080
```

Checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
`uv run pytest`. The default test run uses a fake Slack API (respx) and an in-memory
store; no credentials are needed. The Firestore paths (`FirestoreStore` and the
OAuth key-value backend) have their own tests against the Firestore emulator, which
are skipped unless `FIRESTORE_EMULATOR_HOST` is set:

```bash
gcloud components install cloud-firestore-emulator      # once; needs Java
gcloud emulators firestore start --host-port=127.0.0.1:8681 \
  --database-mode=firestore-native --project=emulator-test &
FIRESTORE_EMULATOR_HOST=127.0.0.1:8681 uv run pytest -m emulator
```

After a deploy, `scripts/smoke.sh` checks the public surface of the service (see
`docs/DEPLOY.md`).

To poke at the server with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector
```

Open the printed URL, choose transport "Streamable HTTP", URL `http://localhost:8080/mcp`
and click Connect. With a real Google OAuth client whose redirect URI is
`http://localhost:8080/auth/callback` (and `PUBLIC_BASE_URL=http://localhost:8080`),
the Inspector runs the full OAuth flow. Without one, call the tools directly from
Python:

```python
from fastmcp import Client
from slack_guest_reader.app import create_mcp
# see tests/test_tools.py for a complete example with the fake Slack API
```

## Security and privacy

- Read-only by design: no write scopes, no write code paths.
- Allowlists enforced server-side for channels and for emails; emails are re-checked
  on every request, not only at sign-in.
- Logs contain IDs, counts, latencies and error codes. Never message text, names or
  tokens.
- Secrets only via Secret Manager / env. OAuth tokens are encrypted at rest in
  Firestore. Events expire after `EVENTS_TTL_DAYS` through a Firestore TTL policy;
  the user-name cache after 24 hours.
- The service runs as a dedicated service account with only
  `secretmanager.secretAccessor` and `datastore.user`.
- No credentials in git: `.gitignore` from the first commit, gitleaks as a pre-commit
  hook and in CI. See `CLAUDE.md`.

## Notifications: how "what's new for me" works

A message counts if it is newer than `since` and is a **mention** of you (optionally
`@here`/`@channel`), a **reply to your message**, a **reply in a thread you took part
in** (configurable, on by default), or a **reaction** to your message. Your own
messages never count.

- Phase 1 (polling, always available): computed on demand from history and replies.
  Per-channel cursors and an index of your threads live in Firestore, so repeated
  calls fetch only the delta; old threads are re-checked within
  `THREAD_LOOKBACK_DAYS`.
- Phase 2 (Events API, optional): `/slack/events` classifies and stores events as
  they happen. `get_notifications` then reads them from Firestore and polls only the
  gap before the receiver came online.

MCP cannot push to the assistant: notifications are pulled when you ask.

## Repository layout

```
src/slack_guest_reader/   app, config, auth, slack_client, render, users,
                          notifications, events, store, tools, timeparse
tests/                    unit + integration tests (fake Slack via respx)
slack/                    app manifests
scripts/                  bootstrap.sh, deploy.sh, teardown.sh (local machine only)
docs/                     ADMIN_REQUEST, DEPLOY, DECISIONS, HANDOFF
```

License: MIT.
