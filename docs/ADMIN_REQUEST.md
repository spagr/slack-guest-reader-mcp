# Request to the workspace admin (template)

Copy, adjust the bracketed parts, and send through whatever channel you normally use
for admin requests. Keep the token hand-over out of plain Slack messages.

---

Subject: Read-only Slack app for my AI assistant (agreed channels only)

Hi [admin name],

I work with your team in [channel names] as an external collaborator. I'd like to let my
AI assistant read those channels so I can ask it things like "what's new for me?" or
"summarise this thread" without scrolling through history. To do that properly, and
only with your knowledge, I'd need a small Slack app installed in your workspace.

**What it can do**

- Read messages, thread replies and reactions in the channels you invite it to.
- Resolve member names (so "U0123" becomes a name in the output).

**What it cannot do**

- Post, react, edit, delete or pin anything. It has no write scopes at all.
- Join channels on its own, read direct messages, or read any channel it isn't
  explicitly invited to. On my side there is an additional allowlist of channel IDs,
  so even an accidental invite elsewhere would not expose that channel.
- Download file contents. It only sees file names and links.
- Act for anyone but me: access on my side is locked to my own Google account.

**Setup (about 5 minutes)**

1. Go to https://api.slack.com/apps -> "Create New App" -> "From a manifest",
   pick this workspace and paste the manifest I've attached
   (`slack/manifest-phase1.yaml`). It lists exactly the scopes above:
   `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`,
   `reactions:read`. [Later, once events are enabled on my side, I may ask you to
   add the Event Subscriptions request URL from `slack/manifest.local.yaml`; Slack
   verifies that URL when it is saved, so it only works after my server has your
   signing secret.]
2. Click "Install to Workspace" and approve.
3. In each agreed channel, run `/invite @Guest Reader` (the app's bot user).
4. Under "OAuth & Permissions", copy the **Bot User OAuth Token** (starts with `xoxb-`),
   and under "Basic Information" the **Signing Secret**. Please send both via
   [1Password share / Bitwarden Send / an encrypted message], not in a Slack message
   or email.

**Revoking**

Any time: remove the app under "Manage apps" in the workspace settings, or just
`/remove @Guest Reader` from a channel. Either stops the access immediately.

The data stays yours: my side keeps no archive, only a short-lived cache of
notifications (deleted after 30 days) and member display names (24 hours).

Thanks,
[your name]
