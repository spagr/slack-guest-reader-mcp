# Decisions

Short ADR-style notes. Newest at the bottom. Each entry names the docs or versions
it relied on, because FastMCP's auth API, the MCP authorization spec and Slack's
rate-limit policy all move quickly.

## 2026-10-13: FastMCP 4.1 `GoogleProvider` as the OAuth proxy

- Verified against the installed `fastmcp==4.1.0` sources
  (`fastmcp/server/auth/providers/google.py`, `fastmcp/server/auth/oauth_proxy/proxy.py`)
  and https://gofastmcp.com (OAuth Proxy, Google provider, Storage Backends pages).
- `GoogleProvider` already implements the OAuth-proxy pattern the brief asks for:
  MCP-compliant metadata (`/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource/mcp`), dynamic client registration, PKCE,
  a consent page, and FastMCP-issued JWTs that reference the upstream Google token.
  Verified by a local smoke test (401 with `resource_metadata` on `/mcp`, both
  metadata documents, `/register` accepting Claude's callback URI).
- The email allowlist is not a provider feature, so `AllowlistedGoogleProvider`
  overrides two documented hooks:
  - `_extract_upstream_claims(idp_tokens)` runs when the Google code is exchanged
    (and on refresh). It decodes the `id_token` Google returned directly over TLS
    (OIDC Core 3.1.3.7 allows skipping signature validation in that case), falls
    back to Google's `tokeninfo` endpoint, and raises `TokenError("invalid_grant")`
    for unverified or non-allowlisted emails. `access_denied` is not a valid token
    error code in the MCP SDK's `TokenError`, hence `invalid_grant`.
  - `load_access_token(token)` runs on every request and re-checks the email from
    the claims `GoogleTokenVerifier` populates, so removing someone from
    `ALLOWED_EMAILS` takes effect immediately (modulo the 5-minute verifier cache).
- `require_authorization_consent="remember"` keeps FastMCP's confused-deputy
  protection but skips the consent page on repeat logins.
- Google scopes requested: `openid` and `email` only (normalised by FastMCP to the
  full `userinfo.email` URI).

## 2026-10-13: OAuth state in Firestore through `py-key-value-aio`

- FastMCP persists clients, transactions and upstream tokens via the
  `AsyncKeyValue` protocol from `py-key-value-aio` (0.4.x), which ships a
  `FirestoreStore`. We use it with the `wrappers-encryption` extra
  (`FernetEncryptionWrapper`) so upstream Google tokens are encrypted at rest, as
  the FastMCP docs insist for custom storage.
- The Fernet key and FastMCP's JWT signing key are derived from
  `GOOGLE_CLIENT_SECRET` with HKDF unless `STORAGE_ENCRYPTION_KEY` /
  `JWT_SIGNING_KEY` are set. Consequence, documented in the README: rotating the
  Google client secret logs every connector out (they re-authorise once).
- Application state (user-name cache, cursors, my-thread index, events, meta) uses
  our own small `Store` interface with an in-memory fake for tests and a thin
  Firestore implementation. The Firestore implementation has not run against a real
  database yet (cloud session has no credentials); see `docs/HANDOFF.md`.

## 2026-10-13: Stateless Streamable HTTP

- `mcp.http_app(path="/mcp", stateless_http=True)` so that Cloud Run can scale to
  zero and run more than one instance without session affinity. Claude's connectors
  work with stateless Streamable HTTP.

## 2026-10-13: Slack rate limits for internal apps

- Checked https://docs.slack.dev/apis/web-api/rate-limits,
  https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps
  and https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity (June 2025).
- `conversations.history` and `conversations.replies` are capped at 1 request per
  minute and 15 objects per page for apps distributed outside the Marketplace.
  Internal, customer-built apps (created inside the workspace, not distributed)
  keep Tier 3 limits. That is why the admin must create the app from the manifest
  inside their own workspace rather than installing one we distribute.
- The client still honours `Retry-After` on 429 and keeps page sizes at 200, so it
  degrades gracefully if the limits change again.

## 2026-10-13: Search is server-side substring matching

- `search.messages` requires a user token, which this project forbids. The tool
  fetches recent history (bounded: 500 messages and 30 active threads per channel,
  14 days by default) and matches case-insensitively. Good enough for "find the
  thread about X last week"; not a full-text index.

## 2026-10-13: Notifications cursor semantics

- `get_notifications` without `since` returns what is new since the previous call
  (oldest per-channel cursor) and advances the cursors. With `since` it does not
  touch the cursors... except that the poller always records the newest `ts` seen;
  this is intentional so that "what's new" never re-reports something the
  assistant already showed. Pass `since` explicitly to re-read a window.
- Old threads can receive new replies: the poller scans parents back to
  `THREAD_LOOKBACK_DAYS` and only fetches replies for threads whose `latest_reply`
  is newer than `since`. The first time a thread is seen, the whole thread is read
  once so the operator's earlier replies are discovered (`my_threads` index).
- Polling cannot timestamp reactions; the operator's own messages newer than
  `since` that carry reactions from others are reported as `reaction` with the
  reaction names and counts (best effort, as the brief allows).

## 2026-10-13: Events receiver answers 500 to make Slack retry

- `/slack/events` processes synchronously under a 2.5 s budget (Slack wants an ack
  within 3 s). If processing fails or the budget runs out, it returns 500 so Slack
  retries (up to 3 times); storage is idempotent by `event_id`, so retries are safe.
  A background task was rejected because Cloud Run may throttle CPU after the
  response is sent.

## 2026-10-13: Google OAuth client is created by hand

- Searched the gcloud reference. `gcloud iam oauth-clients` manages Workforce
  Identity Federation clients and `gcloud iap oauth-clients` creates IAP clients;
  neither creates the standard "Web application" client the consent-screen flow
  needs, and Google has deprecated programmatic creation of those. `bootstrap.sh`
  therefore pauses with Console deep links for the new project and continues once
  the client ID and secret are pasted in.

## 2026-10-13: Docker base and build

- `python:3.12-slim` with uv copied from `ghcr.io/astral-sh/uv:0.11.32`; no
  BuildKit cache mounts so that Cloud Build's plain `docker build` works. Non-root
  user, only the virtualenv and `src/` are copied into the runtime stage.

## 2026-10-13: Health endpoint is `/health`, not `/healthz`

- On the first real deploy, `GET /healthz` on the Cloud Run URL (both the
  deterministic `*.run.app` and the legacy `*.a.run.app` form) returned Google's own
  HTML 404 page with no request log entry on the revision, while `/health` and
  `/healthz/` reached the container. Google's frontend answers the exact path
  `/healthz` itself. The liveness route was therefore renamed to `/health` in
  `app.py`, the tests, `deploy.sh` and the docs. CLAUDE.md was updated to match.

## 2026-10-13: Scripts never touch the user's active gcloud configuration

- `bootstrap.sh` used to `configurations activate` the named configuration and
  `deploy.sh` wrote `core/project` into whatever configuration was active. The
  scripts now create the configuration with `--no-activate` and select it through
  `CLOUDSDK_ACTIVE_CONFIG_NAME`, which lives only in the script's process.

## 2026-10-13: Placeholder bot token before the Slack app exists

- The operator usually deploys and tests the OAuth flow before the workspace admin
  has created the Slack app. `Settings` requires an `xoxb-` token, so `deploy.sh`
  offers to store `xoxb-PLACEHOLDER-…` in Secret Manager; the service starts and
  every Slack call fails with `invalid_auth` until the real token replaces it.
  Startup does not call `auth.test`, deliberately, so the placeholder is harmless.

## 2026-10-14: OAuth key-value store sanitizes Firestore document IDs

- On the first real OAuth attempt from Claude, `/authorize` returned 500. Claude
  identifies itself with a Client ID Metadata Document, so `client_id` is a URL
  (`https://claude.ai/oauth/mcp-oauth-client-metadata`). FastMCP's OAuth proxy stores
  the client under that key, `py-key-value-aio`'s Firestore store used it verbatim as
  the document ID, and Firestore rejected the `/` characters with
  `INVALID_ARGUMENT ... lacks a collection id`. The store now gets
  `FirestoreV1KeySanitizationStrategy` for keys and collections (replaces `/`,
  appends a short hash). Existing documents written before this change had valid IDs
  and are unaffected.

## 2026-10-14: Startup CPU boost

- Cold start was about 10 s (instance start to Uvicorn ready), which matched the
  timeout of the "Checking the server" step in Claude's connector dialog and made the
  first attempt fail. `deploy.sh` now passes `--cpu-boost`; `--min-instances=1` was
  rejected because it leaves the free tier.

## 2026-10-14: Sentry, opt-in, with content never sent

- `sentry-sdk` (2.x) is initialised only when `SENTRY_DSN` is set. The Starlette
  integration is used (FastMCP runs on Starlette under uvicorn); Sentry's Cloud
  Functions `GcpIntegration` from the onboarding page does not apply to Cloud Run.
- Privacy settings are fixed in code, not configurable, through the SDK's
  `data_collection` option (`send_default_pii` is deprecated in sentry-sdk 2.72):
  no user info, cookies, request bodies, query strings or stack-frame locals,
  because bodies and locals can hold Slack message text, which this server must
  never emit. Tracing is off by default (`SENTRY_TRACES_SAMPLE_RATE=0`). Docs relied
  on: docs.sentry.io/platforms/python/configuration/options (data_collection) and
  /platforms/python/data-management/data-collected.
- The DSN lives in Secret Manager like the other secrets; `deploy.sh` mounts it only
  when a version exists.
