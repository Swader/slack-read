---
name: slack-read
description: |
  Read Slack messages, full threads, follow-up messages, sub-threads, and attachments via the Slack Web API (`slack-read` CLI).
  Use automatically whenever the user pastes a Slack URL (`*.slack.com/archives/...` or `app.slack.com/client/...`) or asks to read a Slack message, thread, DM, or channel.
  Saves tokens and avoids Slack's virtualized DOM skipping messages. Read-only: still use the browser to send messages as the user.
---

# Slack Read (API-first)

Use `slack-read` via `bash` whenever you need to **read** a Slack link, message, thread, DM, or channel. Do not open Slack in the browser just to read messages.

## Quick start

```bash
# Read a pasted Slack URL (fetches full thread + up to 20 DM follow-ups within a 30m gap)
slack-read "https://your-workspace.slack.com/archives/D0123456789/p1700000000123456"

# Include follow-up channel messages and all their sub-threads (or read past a 30m DM gap)
slack-read --after 20 "https://your-workspace.slack.com/archives/C0123456789/p1700000000123456"

# Include prior context as well
slack-read --before 10 --after 20 "https://your-workspace.slack.com/archives/C0123456789/p1700000000123456"

# Read the latest 25 messages (plus threads) in a channel
slack-read --limit 25 "https://your-workspace.slack.com/archives/C0123456789"

# Machine-readable JSON output
slack-read --json "https://your-workspace.slack.com/archives/D0123456789/p1700000000123456"

# Inspect granted vs recommended OAuth scopes
slack-read --check-scopes
```

## Anti-skip & noise-control behavior

- **Reply links**: if a URL points to a reply without `?thread_ts=...`, `slack-read` resolves the root `thread_ts` and fetches the complete parent thread.
- **Full pagination**: follows `response_metadata.next_cursor` across all pages so replies and follow-up messages are not dropped.
- **DM defaults**: `D...` channels default to `--after 20` with a 30-minute inactivity gap cutoff (`--after-gap-minutes 30`) and expand every sub-thread in the window. Pass explicit `--after N` (or `--after-gap-minutes 0`) to read past a gap.
- **Channel defaults**: public/private channels (`C...`, `G...`) default to `--after 0` (full linked thread only); pass `--after N` or `--before N` if the conversation continues in the main channel.
- **Deduplicated unfurls**: identical repeated link-unfurl attachments within a thread are omitted by default when the message has other content (pass `--no-dedup-attachments` to keep all copies).
- **Non-text content**: extracts `blocks`, `attachments`, `reactions`, and `files`, and resolves user, bot, and channel names.

## Configuration, attachments & scopes

- `slack-read` loads only the configured Slack token from `SLACK_READ_TOKEN` (or `SLACK_USER_TOKEN` / `SLACK_TOKEN`), `~/.config/slack-read/env`, or `--env-file` / `--token-env`, and never prints or logs it.
- Hosted attachments download automatically to `/tmp/slack-read/<channel>-<ts>/` (or `SLACK_READ_DOWNLOAD_DIR` / `--download-dir`) once the token has `files:read`. Use the `read` tool on downloaded image paths to inspect screenshots, or pass `--no-download` to skip downloads.
- If a file reports `missing Slack OAuth scope 'files:read'` and inspecting the image is required for the task, note the missing scope and use the browser only for that file's permalink.
- For **sending** messages as the user, continue using the browser.
