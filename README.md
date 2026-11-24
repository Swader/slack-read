# slack-read

Read Slack messages, full threads, follow-up messages, sub-threads, and attachments from the terminal or an AI coding agent using the Slack Web API.

`slack-read` is a read-only, zero-dependency Python CLI plus an agent skill, prompt hook (Codex / Claude Code), and Pi/Testy extension. It replaces slow, token-heavy browser scraping of Slack's virtualized DOM with one deterministic API call.

## Why use it

- **Saves tokens and turns**: fetches a complete thread in one command instead of scrolling screenshots or DOM snapshots.
- **No skipped messages**: recovers the parent `thread_ts` from reply links, paginates every `next_cursor`, and expands sub-threads automatically.
- **Readable identity**: resolves user, bot, mention (`@name`), and channel names.
- **Signal over noise**:
  - For 1:1 DMs (`D...`), fetches up to 20 follow-up messages after the linked message and stops at a 30-minute inactivity gap (override with `--after` or `--after-gap-minutes`).
  - For channels (`C...`, `G...`), fetches the full linked thread only (`--after 0`) unless you pass `--after` or `--before`.
  - Deduplicates identical repeated link-unfurl attachments within a thread (disable with `--no-dedup-attachments`).
- **Downloads attachments**: saves hosted images and files locally via `files:read` without leaking the bearer token off Slack file hosts.
- **Read-only**: never posts to Slack. Keep using the Slack UI or browser when you need to send a message as yourself.

## Requirements

- Python 3.10+ (standard library only, no third-party packages)
- A Slack **User OAuth Token** (`xoxp-...`)

## Slack App & OAuth Scopes

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) (or reuse a personal/internal dev app), add the following **User Token Scopes** under **OAuth & Permissions**, and install the app to your workspace:

| Scope | Purpose |
| --- | --- |
| `channels:history` | Read public channel messages and threads |
| `groups:history` | Read private channel messages and threads |
| `im:history` | Read 1:1 DM messages and threads |
| `mpim:history` | Read group DM messages and threads |
| `files:read` | Download hosted file and image attachments |
| `users:read` | Resolve user and bot IDs to display names |
| `channels:read` | Resolve public channel names |
| `groups:read` | Resolve private channel names |
| `im:read` | Resolve DM metadata |
| `mpim:read` | Resolve group DM metadata |

Verify your token and scopes at any time:

```bash
slack-read --check-scopes
```

## Configure your token

`slack-read` reads only the configured token key into memory and never prints, logs, or exports other keys from your env file.

Choose any of the following setups:

1. **Dedicated config file** (recommended):
   ```bash
   mkdir -p ~/.config/slack-read && chmod 700 ~/.config/slack-read
   printf 'SLACK_READ_TOKEN=xoxp-your-token-here\n' > ~/.config/slack-read/env
   chmod 600 ~/.config/slack-read/env
   ```
2. **Pointer to an existing dotenv file** (avoids duplicating secrets):
   ```bash
   mkdir -p ~/.config/slack-read && chmod 700 ~/.config/slack-read
   cat <<'EOF' > ~/.config/slack-read/env
   SLACK_READ_ENV_FILE=~/.path/to/your.env
   SLACK_READ_TOKEN_ENV=MY_EXISTING_SLACK_TOKEN_VAR
   EOF
   chmod 600 ~/.config/slack-read/env
   ```
3. **Environment variable**: export `SLACK_READ_TOKEN` (also checks `SLACK_USER_TOKEN` and `SLACK_TOKEN`), or override per run with `--env-file` and `--token-env`.

## Installation

Clone the repo and symlink the CLI and skill for whichever tools you use:

```bash
git clone https://github.com/Swader/slack-read.git ~/.agents/skills/slack-read

# 1. CLI on PATH
mkdir -p ~/.local/bin
ln -sfn ~/.agents/skills/slack-read/scripts/slack_read.py ~/.local/bin/slack-read

# 2. Optional: expose skill to Claude Code / Cursor
mkdir -p ~/.claude/skills ~/.cursor/skills
ln -sfn ~/.agents/skills/slack-read ~/.claude/skills/slack-read
ln -sfn ~/.agents/skills/slack-read ~/.cursor/skills/slack-read
```

### Auto-trigger when a Slack URL is pasted

- **Pi / Testy** (`~/.pi/agent/extensions/`):
  ```bash
  mkdir -p ~/.pi/agent/extensions
  ln -sfn ~/.agents/skills/slack-read/extensions/slack-read.js ~/.pi/agent/extensions/slack-read.js
  ```
- **Codex** (`~/.codex/hooks.json`) or **Claude Code** (`~/.claude/settings.json`): add a `UserPromptSubmit` hook pointing to `hooks/slack_prompt_hook.py`:
  ```json
  {
    "hooks": {
      "UserPromptSubmit": [
        {
          "hooks": [
            {
              "type": "command",
              "command": "python3 ~/.agents/skills/slack-read/hooks/slack_prompt_hook.py"
            }
          ]
        }
      ]
    }
  }
  ```

## Usage

```bash
# Read a linked message and its full thread (DMs also fetch up to 20 follow-ups within a 30m gap)
slack-read "https://your-workspace.slack.com/archives/D0123456789/p1700000000123456"

# Include surrounding channel messages and all their sub-threads
slack-read --before 10 --after 20 "https://your-workspace.slack.com/archives/C0123456789/p1700000000123456"

# Read the latest 25 messages (plus threads) in a channel
slack-read --limit 25 "https://your-workspace.slack.com/archives/C0123456789"

# Read by channel ID and explicit timestamp
slack-read --ts 1700000000.123456 C0123456789

# Emit structured JSON or skip file downloads
slack-read --json --no-download "https://your-workspace.slack.com/archives/C0123456789/p1700000000123456"
```

### Key flags

| Flag | Default | Description |
| --- | --- | --- |
| `--after N` | `20` (DM) / `0` (channel) | Subsequent top-level messages to include (disables default DM gap cutoff unless `--after-gap-minutes` is also set) |
| `--after-gap-minutes N` | `30` (auto DM) | Stop follow-up messages when the gap exceeds `N` minutes (`0` disables) |
| `--before N` | `0` | Preceding top-level messages to include |
| `--limit N` | `20` | Messages to fetch for channel-only targets |
| `--no-threads` | off | Do not expand sub-threads on fetched messages |
| `--no-download` | off | Do not download attached files |
| `--no-dedup-attachments` | off | Keep identical repeated link-unfurl attachments within a thread |
| `--download-dir PATH` | `/tmp/slack-read/<ch>-<ts>` | Override download directory (also `SLACK_READ_DOWNLOAD_DIR`) |
| `--max-file-mb N` | `25` | Maximum single attachment size to download |
| `--max-pages N` | `50` | Maximum pagination pages per API query |
| `--env-file PATH` | `~/.config/slack-read/env` | Env file to read the Slack token from |
| `--token-env NAME` | `SLACK_READ_TOKEN` | Environment variable name holding the Slack token |
| `--json` | off | Emit structured JSON instead of compact text |
| `--check-scopes` | off | Check token authentication and compare granted vs recommended scopes |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
