# CLAUDE.md — slack-guest-reader-mcp

## What this project is

A small, self-hostable **remote MCP server** that gives an AI assistant (Claude on web, desktop and mobile) **read-only** access to a handful of channels in a **Slack workspace you don't own** — typically a client's or partner's workspace where you are a guest or external collaborator.

Access goes through an official **Slack app created by the workspace admin** (bot token `xoxb-…`). The server never uses user session tokens, cookies, scraping, headless browsers or undocumented endpoints.

It runs on **Google Cloud Run** (scales to zero, fits the free tier). It is protected by **OAuth with Google sign-in** restricted to an email allowlist, so it can be added as a custom connector in Claude.

The repo must be generic and publishable: no company names, workspace IDs, channel IDs, emails, URLs or secrets anywhere in code, tests, docs or git history. Everything deployment-specific comes from env vars and Secret Manager.

## HARD RULE: no credentials in git, ever

This repo is developed partly in a **cloud Claude Code session** (started from a phone) and continued on a local machine via the remote repo. Everything pushed is effectively public. Nothing from the credentials layer may ever reach a commit or the remote.

**What counts as credentials:**
- Slack tokens (`xoxb-`, `xoxp-`, `xoxc-`, `xoxd-`) and Slack signing secrets,
- Google OAuth client secrets,
- service-account key JSON files,
- `gcloud` config and credentials,
- `.env` files and `bootstrap.env`,
- real project IDs, org IDs, billing account IDs, channel IDs, user IDs and allowlisted emails,
- any Cloud Run URL of a real deployment.

**Rules:**
1. **The cloud session gets no credentials at all.** Phases 1 and 2 are built and tested entirely against mocks (`respx`, in-memory store). If a task seems to need a real token, stop and leave a note in `docs/HANDOFF.md` instead. Never ask the user to paste a secret into the session.
2. **`bootstrap.sh`, `deploy.sh` and any real-workspace testing run only on the local machine,** with a `gcloud auth login` session there. Scripts read secrets from prompts or Secret Manager only, never from files in the repo.
3. **`.gitignore` must exist from the very first commit** and cover at least:
   - `.env`, `.env.*` (except `.env.example`),
   - `bootstrap.env`, `*.local.*`,
   - `*credentials*.json`, `*-key.json`, `service-account*.json`,
   - `.gcloud/`, `.config/gcloud/`,
   - `.venv/`, `__pycache__/`.
4. **Secret scanning is mandatory:**
   - a `gitleaks` pre-commit hook (via `pre-commit`) with custom rules for the Slack token prefixes above,
   - a `gitleaks` job in CI that fails the build.
   - Add both in the first commit, before any code.
5. **Placeholders only** in `.env.example`, docs, tests and fixtures. Use obviously fake values such as `xoxb-TEST-TOKEN`, `C0000000000`, `U0000000000`, `example.com`, `my-gcp-project`.
6. **Before every commit, run `git diff --cached` and check for anything matching the list above.** If something slips through, do not just delete it in a new commit. Stop, tell the user, and treat the secret as leaked: rotate it, then rewrite history only with the user's explicit OK.

## Handoff between cloud and local sessions

Keep `docs/HANDOFF.md` up to date at the end of every work session:
- what's done and what's next,
- what is blocked on local-only steps (credentials, deploy, real Slack testing),
- the exact commands to run locally.

Commit it along with the work, so the next session, cloud or local, can pick up without context.

## Goals

1. Let the assistant read messages and threads from an explicit allowlist of channels.
2. Answer "what's new for me?" without re-downloading history. That means mentions of me, replies to my messages, replies in threads I took part in, and reactions to my messages.
3. Be trivially deployable by one person: one `Dockerfile`, one deploy script, a handful of env vars.
4. Make it easy for a workspace admin to create the Slack app: ship a ready **Slack app manifest**.

## Non-goals (do not implement)

- Any write action to Slack: posting, reacting, editing, joining channels.
- User tokens (`xoxp`/`xoxc`), cookies, browser automation, Slack's internal web-client APIs.
- Reading channels outside the allowlist, even if the bot was invited there.
- Downloading file contents. Expose file metadata (name, type, permalink) only.
- A long-term archive. Snapshotting may come later as an optional feature, so keep it out of the core.

## Tech stack

- Python 3.12, dependency management with **uv**, lint/format with **ruff**, type checking with **mypy** (strict on `src/`).
- **FastMCP** (MCP Python framework) with the **Streamable HTTP** transport.
- `httpx` for Slack Web API calls. Do not pull in `slack_sdk` unless it clearly simplifies something.
- **Firestore** (Native mode) for persistent state: OAuth client/token storage, user-name cache, polling cursors and events.
- **Secret Manager** for the Slack bot token, Slack signing secret and Google OAuth client secret.
- Tests: `pytest`, `respx` for mocking Slack HTTP, the Firestore emulator or an in-memory fake behind an interface.

> **Verify before you build.** FastMCP's auth API, the MCP authorization spec and Slack's rate-limit policy change often. Before implementing auth or rate limiting, check the current docs (Context7 or official docs) and note in `docs/DECISIONS.md` which versions and doc pages you relied on.

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

Single container, single service. Endpoints:

- `/mcp`: the MCP Streamable HTTP endpoint (OAuth-protected).
- OAuth endpoints and `/.well-known/*` metadata, as required by the MCP auth spec (provided by the FastMCP auth layer).
- `/slack/events`: Slack Events API receiver (phase 2). This endpoint is **not** OAuth-protected. It must verify the Slack signature instead (`X-Slack-Signature`, `X-Slack-Request-Timestamp`, 5-minute replay window, constant-time compare) and handle the `url_verification` challenge.
- `/health`: liveness check, no data. (Not `/healthz`: Google's frontend answers that
  path itself on Cloud Run and the request never reaches the container.)

## Authentication (MCP side)

Claude custom connectors expect the MCP server to behave as an OAuth authorization server per the MCP spec (metadata discovery, dynamic client registration, PKCE). Google does not support dynamic client registration, so use an **OAuth proxy** pattern. The server exposes the MCP-compliant OAuth endpoints and delegates the user login to Google.

- Prefer FastMCP's built-in Google provider / OAuth proxy if it exists in the current version. Otherwise implement the proxy minimally and document why.
- After Google login, allow access only if the verified email is in `ALLOWED_EMAILS`. Reject unverified emails.
- Persist OAuth clients and tokens in Firestore. The default in-memory storage would log users out on every Cloud Run cold start.
- The README must explain the Google side:
  - creating the OAuth client (type "Web application") and its redirect URI,
  - choosing user type **Internal** when the GCP project belongs to a Google Workspace org. External apps in "Testing" mode expire refresh tokens after 7 days.

## Slack side

### Bot scopes (minimum)

| Scope | Why |
|---|---|
| `channels:history`, `groups:history` | read messages in public / private channels the bot is in |
| `channels:read`, `groups:read` | channel names and info |
| `users:read` | resolve user IDs to names |
| `reactions:read` | reactions on messages, `reaction_added` events |

No write scopes. Ever.

### App manifest

Ship `slack/manifest.yaml` so the admin can do "Create app → From manifest". It must include:

- the scopes above,
- `socket_mode_enabled: false`,
- for phase 2, `event_subscriptions.request_url` pointing at `https://<your-service>/slack/events`, with bot events `message.channels`, `message.groups` and `reaction_added`.

Also ship `docs/ADMIN_REQUEST.md`: a short, polite, generic template email or message to the workspace admin. It should explain what the app reads, what it cannot do and how to revoke it. It should also cover the steps: create from manifest, install, `/invite` the app to the agreed channels, share the bot token and signing secret via a secure channel, not plain Slack.

The app must be created **by the admin inside their workspace** as an internal app. Internal apps keep normal rate limits on `conversations.history` and `conversations.replies`, whereas non-Marketplace distributed apps are heavily throttled. Verify this against current Slack docs and mention it in the README.

### API usage rules

- Endpoints: `conversations.history`, `conversations.replies`, `conversations.info`, `users.info`. Optionally `auth.test` at startup to log the bot/team IDs.
- Always paginate with `cursor` and use `oldest`/`latest` to bound requests.
- On HTTP 429, honor `Retry-After`. Use exponential backoff with jitter for 5xx. Cap the total time per tool call.
- Cache `users.info` results in Firestore with a TTL of 24 h.
- Render messages for the LLM:
  - resolve `<@U…>` to `@Name` and `<#C…|name>` to `#name`,
  - keep link text and URL,
  - convert `ts` to ISO 8601 in `TIMEZONE`,
  - include a permalink when cheap (it can be built from the team domain, channel and ts).
- **Enforce the channel allowlist server-side on every call.** Requests for any other channel return a clear error, even if the bot is a member there.

## MCP tools

All tools are read-only and return compact, LLM-friendly JSON. Each message carries `channel`, `ts`, `thread_ts`, `author` (name + id), `time`, `text`, `reply_count`, `reactions` (name + count + whether ME_USER_ID reacted), `files` (metadata only) and `permalink`. Every tool takes a `limit` with a sane default and a hard maximum, so the assistant can't pull megabytes.

| Tool | Purpose |
|---|---|
| `list_channels()` | Allowlisted channels: id, name, topic, purpose, member count. |
| `get_messages(channel, since?, until?, limit=50)` | Top-level messages, newest first. `since`/`until` accept ISO dates or relative values like `"3d"`. |
| `get_thread(channel, thread_ts, limit=100)` | Parent + replies. |
| `search_messages(query, channels?, since="14d", limit=30)` | Case-insensitive substring/keyword search done server-side over fetched history, because `search.messages` needs a user token. Include matching thread replies for threads active in the window. |
| `get_notifications(since?, limit=50)` | "What's new for me" — see below. |

Tool descriptions must be written for an LLM: state what the tool returns, when to use which tool, and that everything is read-only.

## Notifications logic

`ME_USER_ID` is the operator's Slack user ID in the target workspace. A message counts as a notification for me if it is newer than `since` and matches any of these:

1. **mention**: the text contains `<@ME_USER_ID>`, or a group mention I'm part of (`<!here>`, `<!channel>`; make this configurable, off by default).
2. **reply_to_me**: it is a thread reply and the thread parent's author is me.
3. **thread_activity**: it is a reply in a thread where I have posted before. This is configurable and on by default.
4. **reaction**: a reaction added to a message authored by me.

Ignore my own messages. Each notification includes its `type`, the message, and for replies a short snippet of the parent.

**Phase 1 (polling):** compute notifications on demand from `conversations.history` plus `conversations.replies`.
- Store a per-channel cursor and a cache of my thread `ts` values in Firestore, so repeated calls only fetch the delta.
- Old threads can get new replies. Re-check threads whose `latest_reply` is newer than `since`, within `THREAD_LOOKBACK_DAYS` (default 14).
- Reactions come from the `reactions` field on fetched messages. The polling phase can't timestamp a reaction, so report it as "has reactions" with best effort.

**Phase 2 (Events API):** `/slack/events` stores incoming events (message, `reaction_added`) in Firestore after the same classification. `get_notifications` then reads from Firestore first and falls back to polling for gaps.
- Process events idempotently, keyed by `event_id`.
- Ack within 3 s.
- Drop events from non-allowlisted channels before storing.
- Optional: an outbound alert hook (`ALERT_WEBHOOK_URL`, generic JSON POST) for `mention` and `reply_to_me`.

MCP cannot push to the assistant. Notifications are pulled when the user asks.

## Configuration (env vars)

| Var | Required | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | yes | from Secret Manager |
| `SLACK_SIGNING_SECRET` | phase 2 | from Secret Manager |
| `SLACK_CHANNEL_ALLOWLIST` | yes | comma-separated channel IDs |
| `ME_USER_ID` | yes | operator's Slack user ID in that workspace |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | yes | secret from Secret Manager |
| `ALLOWED_EMAILS` | yes | comma-separated |
| `PUBLIC_BASE_URL` | yes | Cloud Run URL or custom domain, used for OAuth redirects |
| `FIRESTORE_DATABASE` | no | default `(default)` |
| `TIMEZONE` | no | default `UTC` |
| `THREAD_LOOKBACK_DAYS` | no | default `14` |
| `NOTIFY_THREAD_ACTIVITY` | no | default `true` |
| `NOTIFY_GROUP_MENTIONS` | no | default `false` |
| `EVENTS_STALE_HOURS` | no | default `6`; phase 2 safety net: poll as well when no event arrived for this long |
| `ALERT_WEBHOOK_URL` | no | phase 2 |
| `SENTRY_DSN` | no | optional error reporting; no PII, bodies or locals are sent |
| `LOG_LEVEL` | no | default `INFO` |

Validate config at startup with pydantic-settings and fail fast with a readable error. Provide `.env.example` with placeholders only.

## Security & privacy

- Read-only by design: no write scopes, no write code paths.
- Allowlist enforced server-side for both channels and emails.
- **Never log message text, user names or tokens.** Log only IDs, counts, latencies and error codes.
- Secrets only via Secret Manager / env. See "HARD RULE: no credentials in git" for `.gitignore` and secret scanning.
- Run as a dedicated service account with only `secretmanager.secretAccessor` and `datastore.user`.
- Store events in Firestore with a TTL policy (default 30 days, documented).
- The README should state plainly that the data belongs to the workspace owner and should be used with their knowledge and consent.

## Repository layout

```
.
├── CLAUDE.md
├── README.md                # what/why, setup end-to-end, connecting in Claude
├── LICENSE                  # MIT
├── pyproject.toml
├── Dockerfile               # slim, non-root user, uv-based install
├── .env.example
├── .gitignore               # first commit, see HARD RULE
├── .pre-commit-config.yaml  # gitleaks + ruff
├── .gitleaks.toml           # extra rules for Slack token prefixes
├── slack/manifest.yaml
├── docs/
│   ├── ADMIN_REQUEST.md
│   ├── DEPLOY.md            # gcloud steps: APIs, Firestore, secrets, SA, deploy
│   ├── HANDOFF.md           # current state, next steps, local-only TODOs
│   └── DECISIONS.md         # short ADR-style notes, doc versions relied on
├── scripts/
│   ├── bootstrap.sh         # final phase: create GCP project + infra from zero
│   └── deploy.sh            # idempotent gcloud deploy; reads config from env
├── src/slack_guest_reader/
│   ├── __init__.py
│   ├── app.py               # FastMCP server, routes, startup
│   ├── config.py
│   ├── auth.py              # Google OAuth proxy + email allowlist
│   ├── slack_client.py      # httpx client, pagination, retries
│   ├── render.py            # message formatting for the LLM
│   ├── notifications.py     # classification + polling logic
│   ├── events.py            # /slack/events (phase 2)
│   ├── store.py             # storage interface + Firestore impl + in-memory fake
│   └── tools.py             # MCP tool definitions
└── tests/
```

## Deployment

- Cloud Run service. The region is configurable; default to an EU region, e.g. `europe-west1`.
- `--min-instances=0`, small CPU/memory, concurrency around 20, request timeout 60 s.
- `--allow-unauthenticated` at the Cloud Run level is required, because Claude's servers call it. Protection comes from OAuth on `/mcp` and signature verification on `/slack/events`.
- `scripts/deploy.sh`:
  - enables the required APIs,
  - creates the service account and secrets if missing (prompting for values, never echoing them),
  - builds with Cloud Build or `gcloud run deploy --source`,
  - prints the final MCP URL to paste into Claude → Settings → Connectors → Add custom connector.

## Bootstrap from zero (final phase)

This is one of the final phases, done after phase 1 and phase 2 work. Its goal is that a new user with an empty Google account (personal or Google Workspace) can go from nothing to a running server with **one script plus one short manual checklist**. No GCP-side MCP server is needed. Everything runs through the `gcloud` CLI, which Claude Code can call directly.

### `scripts/bootstrap.sh`

The script must be idempotent and safe to re-run. Every step checks whether the resource already exists and skips it if so. All values come from env vars or an interactive prompt, with nothing hardcoded. It does the following, in order:

1. Pre-flight checks:
   - `gcloud` is installed,
   - the user is logged in (`gcloud auth list`); if not, it prints `gcloud auth login` and exits,
   - the active account is shown so the user can confirm it is the right identity.
2. Create the project: `gcloud projects create $PROJECT_ID`.
   - Use `--organization=$ORG_ID` when the account belongs to a Workspace org; detect the org with `gcloud organizations list` and offer it as the default.
   - Set it as the active project in a named gcloud configuration, so the user's other projects aren't touched.
3. Link billing: list billing accounts with `gcloud billing accounts list`, let the user pick one, then run `gcloud billing projects link`. Explain that Cloud Run, Firestore and Secret Manager fit the free tier at this usage, but billing must be linked anyway.
4. Optionally create a budget alert, e.g. 5 USD, via `gcloud billing budgets create`, so a mistake can't get expensive. It is on by default and can be skipped.
5. Enable APIs: `run`, `cloudbuild`, `artifactregistry`, `secretmanager`, `firestore`, `iam`, `logging`.
6. Create the Firestore database in Native mode in the chosen region, and set the TTL policy on the events collection.
7. Create the runtime service account with minimal roles (see Security).
8. Create the secrets as empty placeholders, then prompt for their values (input hidden, never echoed or logged):
   - `SLACK_BOT_TOKEN`
   - `SLACK_SIGNING_SECRET` (may be left empty until phase 2)
   - `GOOGLE_CLIENT_SECRET`
9. Hand off to `scripts/deploy.sh` for the build and deploy.
10. Print a final summary:
    - the service URL,
    - the exact OAuth redirect URI to paste into the Google OAuth client,
    - the `/slack/events` URL for the Slack admin,
    - the MCP URL to add in Claude.

Write `bootstrap.env` (gitignored) with the non-secret values it collected (project ID, region, org, service account), so `deploy.sh` and later runs reuse them.

### Manual checklist (things gcloud can't do)

Creating a standard OAuth consent screen and a "Web application" OAuth client is, as far as we know, not possible through `gcloud`. Verify this against current docs; if an API or CLI path exists, automate it instead. Otherwise the script must **pause and walk the user through it** as a numbered checklist with direct Console deep links for the new project:

1. OAuth consent screen: choose user type **Internal** for a Workspace org, otherwise External. Set the app name, the support email and the scopes `openid` and `email` only.
2. Credentials: create an OAuth client ID of type "Web application", with the redirect URI printed by the script.
3. Paste the client ID and secret back into the script. The secret goes straight into Secret Manager.

The script then continues where it stopped. Document the same steps with screenshots-free, text-only instructions in `docs/DEPLOY.md`.

### Teardown

Add `scripts/teardown.sh`: delete the Cloud Run service, secrets and service account, or the whole project with `gcloud projects delete`. It needs an explicit typed confirmation of the project ID. Never run it automatically.

### Done for this phase

- On a fresh test project, `bootstrap.sh` followed by the manual checklist results in a working MCP connector in Claude.
- A second run of `bootstrap.sh` changes nothing and reports everything as existing.
- `teardown.sh` removes everything the bootstrap created.

## Testing & definition of done

- Unit tests for rendering, the allowlist, notification classification (all four types plus self-message exclusion), pagination, 429 handling and Slack signature verification.
- An integration test of the tools against a mocked Slack via `respx`, plus the in-memory store.
- Local run: `uv run` the server, connect with the MCP Inspector and document how in the README.
- CI (GitHub Actions): ruff, mypy, pytest on push/PR. No secrets needed.
- Done for phase 1 means:
  - tests are green,
  - it deploys with `scripts/deploy.sh`,
  - Claude can connect via OAuth from web and mobile,
  - all tools work against a real workspace,
  - nothing deployment-specific is in the repo.
- Then phase 2 (events), then the final bootstrap phase (see "Bootstrap from zero").

## Working agreements for Claude Code

- Work in small, reviewable commits with conventional commit messages.
- Ask before adding a dependency that isn't listed here.
- If a requirement here conflicts with current official docs, follow the docs. Record the conflict in `docs/DECISIONS.md` and tell me.
- Keep it boring and small. If something feels like it needs a framework, it probably doesn't.
