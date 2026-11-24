#!/usr/bin/env python3
"""UserPromptSubmit hook for Codex and Claude Code.

Detects Slack URLs in the submitted user prompt and injects concise
additionalContext instructing the agent to use the `slack-read` CLI instead of
opening Slack in a browser.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import List

SLACK_URL_RE = re.compile(
    r"https?://(?:(?:[a-z0-9-]+\.)*slack\.com/archives/[A-Z0-9]+(?:/p\d{10,20})?(?:\?[^\s<>\"'`)\]]*)?"
    r"|app\.slack\.com/client/[A-Z0-9]+/[A-Z0-9]+(?:/[^\s<>\"'`)\]]*)?)",
    re.IGNORECASE,
)

TRAILING_PUNCT = ".,;:!?)>]}'\"`"


def extract_slack_urls(text: str) -> List[str]:
    seen = set()
    urls: List[str] = []
    for match in SLACK_URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def build_context(urls: List[str]) -> str:
    quoted = " ".join(shlex.quote(u) for u in urls)
    return (
        "Slack URL(s) detected in the user prompt. Use the `slack-read` CLI "
        "(read-only Slack Web API helper; skill `slack-read`) instead of opening "
        "Slack in a browser to read them.\n"
        f"Run: slack-read {quoted}\n"
        "Defaults: recovers thread root even from reply links, paginates all cursors, "
        "expands sub-threads, deduplicates repeated thread unfurls, and fetches up to "
        "20 follow-up messages for DMs (`D...`) up to a 30-minute gap. "
        "Add `--after 20` or `--before 10` for channel context (or to read past a DM gap) if needed. "
        "Only fall back to the browser to send messages as the user or if a required OAuth scope is missing."
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        if not isinstance(prompt, str) or "slack.com/" not in prompt.lower():
            return 0
        urls = extract_slack_urls(prompt)
        if not urls:
            return 0
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": build_context(urls),
            }
        }
        sys.stdout.write(json.dumps(out) + "\n")
    except Exception:
        # Fail open: never block prompt submission on hook errors.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
