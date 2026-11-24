#!/usr/bin/env python3
"""Read Slack messages, full threads, follow-up messages, and attachments via Slack Web API.

Token-efficient, read-only alternative to reading Slack in the browser.
Loads only the configured Slack token from the environment or an env file into memory.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TOKEN_KEYS = ("SLACK_READ_TOKEN", "SLACK_USER_TOKEN", "SLACK_TOKEN")
DEFAULT_ENV_FILES = (
    Path("~/.config/slack-read/env").expanduser(),
    Path("~/.slack-read.env").expanduser(),
)
DEFAULT_DM_AFTER = 20
DEFAULT_DM_GAP_MINUTES = 30.0
SLACK_API_BASE = "https://slack.com/api"
ALLOWED_FILE_HOSTS = ("files.slack.com", "files-origin.slack.com", "slack-files.com")
RECOMMENDED_SCOPES = [
    ("channels:history", "read public channel messages & threads"),
    ("groups:history", "read private channel messages & threads"),
    ("im:history", "read 1:1 DM messages & threads"),
    ("mpim:history", "read group DM messages & threads"),
    ("files:read", "download hosted file/image attachments via API"),
    ("users:read", "resolve user and bot IDs to display names"),
    ("channels:read", "resolve public channel names"),
    ("groups:read", "resolve private channel names"),
    ("im:read", "resolve DM metadata"),
    ("mpim:read", "resolve group DM metadata"),
]

ARCHIVE_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*slack\.com/archives/(?P<channel>[A-Z0-9]+)(?:/p(?P<pts>\d{10,20}))?",
    re.IGNORECASE,
)
CLIENT_URL_RE = re.compile(
    r"https?://app\.slack\.com/client/[A-Z0-9]+/(?P<channel>[A-Z0-9]+)(?:/thread/[A-Z0-9]+-(?P<ts>\d+\.\d+))?",
    re.IGNORECASE,
)
CHANNEL_ID_RE = re.compile(r"^[CDGW][A-Z0-9]{6,}$")


class SlackError(RuntimeError):
    def __init__(self, message: str, error_code: str | None = None, needed: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.needed = needed


def _read_env_keys(env_file: Path, wanted_keys: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    if not env_file.is_file():
        return found
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        k = key.strip()
        if sep and k in wanted_keys:
            val = _strip_quotes(value.strip())
            if val:
                found[k] = val
    return found


def load_token(env_file: Path | None = None, token_env: str | None = None) -> str:
    explicit_key = (token_env or os.environ.get("SLACK_READ_TOKEN_ENV") or "").strip() or None
    keys_to_try = (explicit_key,) if explicit_key else DEFAULT_TOKEN_KEYS

    if env_file is None:
        for key in keys_to_try:
            env_val = os.environ.get(key, "").strip()
            if env_val:
                return _strip_quotes(env_val)

    explicit_file_raw = str(env_file) if env_file is not None else os.environ.get("SLACK_READ_ENV_FILE", "").strip()
    candidate_files = (Path(explicit_file_raw).expanduser(),) if explicit_file_raw else DEFAULT_ENV_FILES

    meta_keys = {"SLACK_READ_ENV_FILE", "SLACK_READ_TOKEN_ENV"}
    for candidate in candidate_files:
        if not candidate.is_file():
            continue
        entries = _read_env_keys(candidate, set(keys_to_try) | meta_keys)
        for key in keys_to_try:
            if entries.get(key):
                return entries[key]

        # Optional single-hop pointer in ~/.config/slack-read/env to an existing dotenv file/key
        # so users do not have to duplicate secrets across files.
        target_file_raw = entries.get("SLACK_READ_ENV_FILE")
        target_key = explicit_key or entries.get("SLACK_READ_TOKEN_ENV")
        if target_file_raw or target_key:
            target_path = Path(target_file_raw).expanduser() if target_file_raw else candidate
            target_keys = (target_key,) if target_key else keys_to_try
            if target_path == candidate and target_key:
                pointed = _read_env_keys(candidate, {target_key})
                if pointed.get(target_key):
                    return pointed[target_key]
            elif target_path.is_file():
                pointed = _read_env_keys(target_path, set(target_keys))
                for key in target_keys:
                    if pointed.get(key):
                        return pointed[key]

    searched_keys = ", ".join(keys_to_try)
    searched_files = ", ".join(str(p) for p in candidate_files)
    raise SlackError(
        f"Slack token not found. Checked env vars ({searched_keys}) and files ({searched_files}). "
        "Set SLACK_READ_TOKEN or create ~/.config/slack-read/env with SLACK_READ_TOKEN=xoxp-..."
    )


def _strip_quotes(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def clean_target(raw: str) -> str:
    s = raw.strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
        if "|" in s:
            s = s.split("|", 1)[0]
    return s.rstrip(").,;]>'\"")


def p_ts_to_slack_ts(pts: str) -> str:
    if "." in pts:
        return pts
    if len(pts) > 6:
        return f"{pts[:-6]}.{pts[-6:]}"
    return pts


def parse_target(raw: str, explicit_ts: str | None = None) -> dict[str, Any]:
    cleaned = clean_target(raw)
    parsed = urllib.parse.urlparse(cleaned)
    qs = urllib.parse.parse_qs(parsed.query)

    channel: str | None = None
    target_ts: str | None = explicit_ts
    thread_ts: str | None = qs.get("thread_ts", [None])[0]
    cid: str | None = qs.get("cid", [None])[0]

    m_arch = ARCHIVE_URL_RE.match(cleaned)
    if m_arch:
        channel = m_arch.group("channel").upper()
        if m_arch.group("pts") and not target_ts:
            target_ts = p_ts_to_slack_ts(m_arch.group("pts"))
    else:
        m_client = CLIENT_URL_RE.match(cleaned)
        if m_client:
            channel = m_client.group("channel").upper()
            if m_client.group("ts") and not target_ts:
                target_ts = m_client.group("ts")
        elif CHANNEL_ID_RE.match(cleaned.upper()):
            channel = cleaned.upper()
        else:
            raise SlackError(f"Unrecognized Slack URL or channel ID: {raw}")

    if not channel and cid:
        channel = cid.upper()

    return {
        "raw": cleaned,
        "channel": channel,
        "target_ts": target_ts,
        "thread_ts": thread_ts,
        "is_dm": bool(channel and channel.startswith("D")),
        "is_mpim": bool(channel and channel.startswith("G")),
    }


class SafeSlackFileRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block redirects to HTML workspace login pages and never forward Bearer token off Slack file hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed_new = urllib.parse.urlparse(newurl)
        host = (parsed_new.hostname or "").lower()
        if (host == "slack.com" or host.endswith(".slack.com")) and host not in ALLOWED_FILE_HOSTS:
            raise SlackError(
                f"Redirected to workspace login ({host}) — token likely lacks 'files:read' scope.",
                error_code="missing_scope",
                needed="files:read",
            )
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and host not in ALLOWED_FILE_HOSTS:
            new_req.remove_header("Authorization")
            new_req.remove_header("authorization")
        return new_req


class SlackClient:
    def __init__(self, token: str, timeout: int = 20):
        self._token = token
        self.timeout = timeout
        self.last_scopes: set[str] = set()
        self.users_cache: dict[str, str] = {}
        self.bots_cache: dict[str, str] = {}
        self.channels_cache: dict[str, str] = {}
        self.users_scope_missing = False
        self.channel_scope_missing = False
        self.search_scope_missing = False
        self.files_scope_missing = False
        self.auth_info: dict[str, Any] = {}
        self._file_opener = urllib.request.build_opener(SafeSlackFileRedirectHandler())

    def api_get(self, method: str, params: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{SLACK_API_BASE}/{method}" + (f"?{query}" if query else "")
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    scopes_hdr = resp.headers.get("X-OAuth-Scopes")
                    if scopes_hdr:
                        self.last_scopes = {s.strip() for s in scopes_hdr.split(",") if s.strip()}
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries:
                    wait_s = min(int(e.headers.get("Retry-After", "2")), 15)
                    time.sleep(wait_s)
                    continue
                raise SlackError(f"HTTP {e.code} calling Slack API {method}") from None
            except urllib.error.URLError as e:
                raise SlackError(f"Network error calling Slack API {method}: {e.reason}") from None

            if not data.get("ok"):
                err = data.get("error", "unknown_error")
                needed = data.get("needed")
                raise SlackError(f"Slack API {method} failed: {err}" + (f" (needed: {needed})" if needed else ""), err, needed)
            return data
        raise SlackError(f"Slack API {method} exhausted retries")

    def auth_test(self) -> dict[str, Any]:
        if not self.auth_info:
            self.auth_info = self.api_get("auth.test")
            uid = self.auth_info.get("user_id")
            uname = self.auth_info.get("user")
            if uid and uname:
                self.users_cache[uid] = uname
        return self.auth_info

    def get_channel_label(self, channel_id: str) -> str:
        if channel_id in self.channels_cache:
            return self.channels_cache[channel_id]
        if not self.channel_scope_missing:
            try:
                res = self.api_get("conversations.info", {"channel": channel_id})
                ch = res.get("channel") or {}
                if ch.get("is_im"):
                    other = ch.get("user")
                    label = f"DM with {self.resolve_user(other)} ({channel_id})" if other else f"DM ({channel_id})"
                    self.channels_cache[channel_id] = label
                    return label
                name = ch.get("name")
                if name:
                    label = f"#{name} ({channel_id})"
                    self.channels_cache[channel_id] = label
                    return label
                return channel_id
            except SlackError as e:
                if e.error_code == "missing_scope":
                    self.channel_scope_missing = True
                else:
                    return channel_id
        # Fallback when channels:read/groups:read is missing: use search.messages (if granted)
        if not channel_id.startswith("D") and not self.search_scope_missing:
            try:
                res = self.api_get("search.messages", {"query": f"in:<#{channel_id}>", "count": 1})
                matches = (res.get("messages") or {}).get("matches") or []
                if matches:
                    ch = matches[0].get("channel") or {}
                    name = ch.get("name")
                    if name and name != channel_id:
                        label = f"#{name} ({channel_id})"
                        self.channels_cache[channel_id] = label
                        return label
            except SlackError as e:
                if e.error_code == "missing_scope":
                    self.search_scope_missing = True
        self.channels_cache[channel_id] = channel_id
        return channel_id

    def resolve_bot(self, bot_id: str) -> str:
        if bot_id in self.bots_cache:
            return self.bots_cache[bot_id]
        if not self.users_scope_missing:
            try:
                res = self.api_get("bots.info", {"bot": bot_id})
                bot = res.get("bot") or {}
                bname = bot.get("name")
                if bname and bname != bot_id:
                    label = f"bot:{bname} ({bot_id})"
                    self.bots_cache[bot_id] = label
                    return label
            except SlackError as e:
                if e.error_code == "missing_scope":
                    self.users_scope_missing = True
        label = f"bot:{bot_id}"
        self.bots_cache[bot_id] = label
        return label

    def resolve_user(self, user_id: str | None, msg: dict[str, Any] | None = None) -> str:
        bot_name: str | None = None
        if msg:
            bot_profile = msg.get("bot_profile") or {}
            bot_name = bot_profile.get("name") or msg.get("username") or None
            if not user_id:
                if bot_name:
                    return f"bot:{bot_name}"
                if msg.get("bot_id"):
                    return self.resolve_bot(msg["bot_id"])
                return "unknown"
            # If a bot message also carries a user_id, prefer bot_profile/username immediately
            if bot_name and (msg.get("bot_id") or msg.get("subtype") == "bot_message" or bot_profile):
                label = f"bot:{bot_name} ({user_id})"
                self.users_cache[user_id] = label
                return label
            uprof = msg.get("user_profile") or {}
            pname = uprof.get("display_name") or uprof.get("real_name") or uprof.get("name")
            if pname and pname != user_id:
                label = f"{pname} ({user_id})"
                self.users_cache[user_id] = label
                return label
        if not user_id:
            return "unknown"
        if user_id in self.users_cache:
            return self.users_cache[user_id]
        if user_id == "USLACKBOT":
            self.users_cache[user_id] = "Slackbot (USLACKBOT)"
            return self.users_cache[user_id]
        if not self.users_scope_missing:
            try:
                res = self.api_get("users.info", {"user": user_id})
                u = res.get("user") or {}
                prof = u.get("profile") or {}
                name = prof.get("display_name") or prof.get("real_name") or u.get("name") or user_id
                label = f"{name} ({user_id})" if name != user_id else user_id
                self.users_cache[user_id] = label
                return label
            except SlackError as e:
                if e.error_code == "missing_scope":
                    self.users_scope_missing = True
                else:
                    self.users_cache[user_id] = user_id
                    return user_id
        # Fallback when users:read is missing: resolve handle via search.messages (from:<@UID>)
        if not self.search_scope_missing:
            try:
                res = self.api_get("search.messages", {"query": f"from:<@{user_id}>", "count": 1})
                matches = (res.get("messages") or {}).get("matches") or []
                if matches:
                    uname = matches[0].get("username")
                    if uname and uname != user_id:
                        label = f"{uname} ({user_id})"
                        self.users_cache[user_id] = label
                        return label
            except SlackError as e:
                if e.error_code == "missing_scope":
                    self.search_scope_missing = True
        self.users_cache[user_id] = user_id
        return user_id

    def resolve_user_short(self, user_id: str) -> str:
        full = self.resolve_user(user_id)
        if full == user_id:
            return user_id
        if full.endswith(f" ({user_id})"):
            return full[: -(len(user_id) + 3)]
        return full

    def fetch_replies_all(
        self,
        channel: str,
        ts: str,
        max_pages: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        truncated = False
        for _ in range(max_pages):
            params: dict[str, Any] = {"channel": channel, "ts": ts, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            res = self.api_get("conversations.replies", params)
            messages.extend(res.get("messages") or [])
            cursor = (res.get("response_metadata") or {}).get("next_cursor") or None
            if not res.get("has_more") or not cursor:
                break
        else:
            truncated = True
        return _dedup_sort(messages), truncated

    def fetch_history_paginated(
        self,
        channel: str,
        *,
        oldest: str | None = None,
        latest: str | None = None,
        inclusive: bool = False,
        max_messages: int = 20,
        max_pages: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        has_more_unfetched = False
        for _ in range(max_pages):
            remaining = max_messages - len(messages)
            if remaining <= 0:
                has_more_unfetched = True
                break
            page_limit = min(200, remaining)
            params: dict[str, Any] = {"channel": channel, "limit": page_limit}
            if oldest is not None:
                params["oldest"] = oldest
            if latest is not None:
                params["latest"] = latest
            if inclusive:
                params["inclusive"] = "true"
            if cursor:
                params["cursor"] = cursor
            res = self.api_get("conversations.history", params)
            batch = res.get("messages") or []
            messages.extend(batch)
            cursor = (res.get("response_metadata") or {}).get("next_cursor") or None
            more = bool(res.get("has_more") and cursor)
            if not more:
                break
            if len(messages) >= max_messages:
                has_more_unfetched = True
                break
        else:
            has_more_unfetched = True
        return _dedup_sort(messages), has_more_unfetched

    def download_file(
        self,
        file_obj: dict[str, Any],
        out_dir: Path,
        max_file_bytes: int,
    ) -> dict[str, Any]:
        fid = file_obj.get("id") or "file"
        raw_name = file_obj.get("name") or file_obj.get("title") or fid
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or fid
        size = file_obj.get("size") or 0
        mimetype = file_obj.get("mimetype") or ""
        url = file_obj.get("url_private_download") or file_obj.get("url_private")

        result: dict[str, Any] = {
            "id": fid,
            "name": raw_name,
            "mimetype": mimetype,
            "filetype": file_obj.get("filetype") or "",
            "size": size,
            "mode": file_obj.get("mode") or "",
            "permalink": file_obj.get("permalink") or "",
            "local_path": None,
            "status": "skipped",
            "reason": None,
        }

        if not url:
            result["reason"] = "no private download URL on file object"
            return result
        if size and size > max_file_bytes:
            result["reason"] = f"exceeds max size ({_fmt_size(size)} > {_fmt_size(max_file_bytes)})"
            return result
        if self.files_scope_missing:
            result["reason"] = "missing Slack OAuth scope 'files:read'"
            return result

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with self._file_opener.open(req, timeout=self.timeout) as resp:
                ct = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ct and not mimetype.startswith("text/html"):
                    self.files_scope_missing = True
                    result["reason"] = "received HTML login page — token lacks 'files:read' scope"
                    return result
                data = resp.read(max_file_bytes + 1)
                if len(data) > max_file_bytes:
                    result["reason"] = f"download exceeded max size ({_fmt_size(max_file_bytes)})"
                    return result
        except SlackError as e:
            if e.error_code == "missing_scope":
                self.files_scope_missing = True
            result["reason"] = str(e)
            return result
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.files_scope_missing = True
                result["reason"] = f"HTTP {e.code} — token lacks 'files:read' scope"
            else:
                result["reason"] = f"HTTP {e.code} downloading file"
            return result
        except urllib.error.URLError as e:
            result["reason"] = f"network error downloading file: {e.reason}"
            return result

        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{fid}_{safe_name}"
        dest.write_bytes(data)
        result["local_path"] = str(dest)
        result["status"] = "downloaded"
        return result


def _dedup_sort(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ts: dict[str, dict[str, Any]] = {}
    for m in messages:
        ts = m.get("ts")
        if ts:
            by_ts[ts] = m
    return sorted(by_ts.values(), key=lambda m: float(m["ts"]))


def _fmt_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.1f} MB"


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return ts


def _clean_mrkdwn(text: str, client: SlackClient) -> str:
    if not text:
        return ""
    s = html.unescape(text)

    def repl_user(m: re.Match[str]) -> str:
        uid = m.group(1)
        short = client.resolve_user_short(uid)
        return f"@{short}" if short != uid else f"<@{uid}>"

    s = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", repl_user, s)
    s = re.sub(r"<#([A-Z0-9]+)\|([^>]+)>", r"#\2", s)
    s = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", s)
    s = re.sub(r"<(https?://[^>]+)>", r"\1", s)
    s = re.sub(r"<!(here|channel|everyone)(?:\|[^>]+)?>", r"@\1", s)
    return s.strip()


def _extract_blocks_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype in ("plain_text", "mrkdwn") and node.get("text"):
            parts.append(str(node["text"]))
            return
        if ntype == "text" and node.get("text"):
            parts.append(str(node["text"]))
            return
        if ntype == "link" and node.get("url"):
            label = node.get("text")
            parts.append(f"{label} ({node['url']})" if label else str(node["url"]))
            return
        if ntype == "user" and node.get("user_id"):
            parts.append(f"<@{node['user_id']}>")
            return
        if ntype == "channel" and node.get("channel_id"):
            parts.append(f"<#{node['channel_id']}>")
            return
        if ntype == "emoji" and node.get("name"):
            parts.append(f":{node['name']}:")
            return
        if ntype == "button" and isinstance(node.get("text"), dict) and node["text"].get("text"):
            parts.append(f"[Button: {node['text']['text']}]")
            return
        for key in ("text", "title", "fields", "elements", "accessory", "rows"):
            if key in node:
                walk(node[key])

    for blk in blocks:
        before = len(parts)
        walk(blk)
        if len(parts) > before:
            parts.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _extract_attachments_lines(
    attachments: list[dict[str, Any]],
    client: SlackClient,
    *,
    seen_bodies: set[str] | None = None,
    allow_dedup: bool = True,
) -> tuple[list[str], int]:
    lines: list[str] = []
    deduped = 0
    for idx, att in enumerate(attachments, 1):
        sub: list[str] = []
        if att.get("pretext"):
            sub.append(_clean_mrkdwn(str(att["pretext"]), client))
        if att.get("author_name"):
            sub.append(f"Author: {att['author_name']}")
        title = att.get("title")
        if title:
            link = att.get("title_link")
            sub.append(f"Title: {title}" + (f" ({link})" if link else ""))
        if att.get("text"):
            sub.append(_clean_mrkdwn(str(att["text"]), client))
        elif att.get("fallback") and not title:
            sub.append(_clean_mrkdwn(str(att["fallback"]), client))
        for fld in att.get("fields") or []:
            ft = fld.get("title") or ""
            fv = _clean_mrkdwn(str(fld.get("value") or ""), client)
            if ft or fv:
                sub.append(f"- {ft}: {fv}" if ft else f"- {fv}")
        if att.get("footer"):
            sub.append(f"Footer: {_clean_mrkdwn(str(att['footer']), client)}")
        if sub:
            body = " | ".join(sub)
            norm_body = re.sub(r"\s+", " ", body).strip()
            if seen_bodies is not None and norm_body:
                if allow_dedup and norm_body in seen_bodies:
                    deduped += 1
                    continue
                seen_bodies.add(norm_body)
            lines.append(f"[attachment {idx}] {body}")
    return lines, deduped


def format_message(
    msg: dict[str, Any],
    client: SlackClient,
    *,
    anchor_ts: str | None,
    download_files: bool,
    download_dir: Path,
    max_file_bytes: int,
    seen_attachments: set[str] | None = None,
    dedup_attachments: bool = True,
) -> tuple[dict[str, Any], int]:
    ts = msg.get("ts", "")
    author = client.resolve_user(msg.get("user"), msg)
    text = _clean_mrkdwn(msg.get("text") or "", client)

    extra_blocks = ""
    if msg.get("blocks"):
        btxt = _clean_mrkdwn(_extract_blocks_text(msg["blocks"]), client)
        norm_text = re.sub(r"\s+", " ", text).strip()
        norm_blocks = re.sub(r"\s+", " ", btxt).strip()
        if norm_blocks and (not norm_text or (len(norm_blocks) > len(norm_text) + 20 and norm_blocks not in norm_text)):
            extra_blocks = btxt

    has_other_content = bool(text or extra_blocks or (msg.get("files") or []))
    att_lines, deduped_atts = _extract_attachments_lines(
        msg.get("attachments") or [],
        client,
        seen_bodies=seen_attachments if dedup_attachments else None,
        allow_dedup=bool(dedup_attachments and has_other_content),
    )
    reactions = [f":{r.get('name')}: x{r.get('count', 1)}" for r in (msg.get("reactions") or []) if r.get("name")]

    file_records: list[dict[str, Any]] = []
    for fobj in msg.get("files") or []:
        if download_files:
            file_records.append(client.download_file(fobj, download_dir, max_file_bytes))
        else:
            file_records.append(
                {
                    "id": fobj.get("id") or "",
                    "name": fobj.get("name") or fobj.get("title") or "",
                    "mimetype": fobj.get("mimetype") or "",
                    "filetype": fobj.get("filetype") or "",
                    "size": fobj.get("size") or 0,
                    "mode": fobj.get("mode") or "",
                    "permalink": fobj.get("permalink") or "",
                    "local_path": None,
                    "status": "not_downloaded",
                    "reason": "--no-download",
                }
            )

    record = {
        "ts": ts,
        "time": _fmt_ts(ts),
        "thread_ts": msg.get("thread_ts"),
        "is_anchor": bool(anchor_ts and ts == anchor_ts),
        "author": author,
        "user_id": msg.get("user"),
        "subtype": msg.get("subtype"),
        "edited": bool(msg.get("edited")),
        "reply_count": msg.get("reply_count") or 0,
        "text": text,
        "extra_blocks": extra_blocks,
        "attachments": att_lines,
        "reactions": reactions,
        "files": file_records,
    }
    return record, deduped_atts


def _default_download_root() -> Path:
    custom = os.environ.get("SLACK_READ_DOWNLOAD_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    if os.name == "posix":
        return Path("/tmp/slack-read")
    return Path(tempfile.gettempdir()) / "slack-read"


def fetch_target(
    client: SlackClient,
    target_spec: dict[str, Any],
    *,
    after: int | None,
    before: int,
    limit: int,
    expand_threads: bool,
    download_files: bool,
    download_dir: Path | None,
    max_file_bytes: int,
    max_pages: int,
    after_gap_minutes: float | None = None,
    dedup_attachments: bool = True,
) -> dict[str, Any]:
    channel = target_spec["channel"]
    target_ts = target_spec["target_ts"]
    explicit_thread_ts = target_spec["thread_ts"]
    warnings: list[str] = []
    notes: list[str] = []

    explicit_after = after is not None
    effective_after = after if explicit_after else (DEFAULT_DM_AFTER if target_spec["is_dm"] and target_ts else 0)

    if after_gap_minutes is not None:
        effective_gap_minutes: float | None = after_gap_minutes if after_gap_minutes > 0 else None
    elif not explicit_after and target_spec["is_dm"] and target_ts:
        effective_gap_minutes = DEFAULT_DM_GAP_MINUTES
    else:
        effective_gap_minutes = None

    out_dir = download_dir or (_default_download_root() / f"{channel}-{(target_ts or 'latest').replace('.', '_')}")
    channel_label = client.get_channel_label(channel)

    top_messages: dict[str, dict[str, Any]] = {}
    threads_by_root: dict[str, list[dict[str, Any]]] = {}
    root_ts: str | None = None
    has_more_after = False
    has_more_before = False
    stopped_at_gap: dict[str, Any] | None = None

    if target_ts:
        # 1) Fetch linked message & its full thread (recovering root_ts if target_ts is a reply)
        query_ts = explicit_thread_ts or target_ts
        replies, trunc = client.fetch_replies_all(channel, query_ts, max_pages=max_pages)
        if trunc:
            warnings.append(f"Thread {query_ts} hit --max-pages ({max_pages}); output may be truncated.")
        if replies:
            discovered_root = replies[0].get("thread_ts") or replies[0].get("ts")
            if discovered_root and discovered_root != query_ts:
                replies, trunc2 = client.fetch_replies_all(channel, discovered_root, max_pages=max_pages)
                if trunc2:
                    warnings.append(f"Thread {discovered_root} hit --max-pages ({max_pages}); output may be truncated.")
                root_ts = discovered_root
            else:
                root_ts = replies[0].get("ts") or query_ts
            top_messages[root_ts] = replies[0]
            if len(replies) > 1:
                threads_by_root[root_ts] = replies[1:]
        else:
            warnings.append(f"Message {target_ts} not returned by conversations.replies.")
            root_ts = query_ts

        # 2) Fetch messages before anchor if requested
        if before > 0 and root_ts:
            prev_msgs, has_more_before = client.fetch_history_paginated(
                channel, latest=root_ts, inclusive=False, max_messages=before, max_pages=max_pages
            )
            for m in prev_msgs:
                if m.get("ts") and m["ts"] != root_ts:
                    top_messages[m["ts"]] = m

        # 3) Fetch messages after anchor if requested (e.g., DM follow-ups or explicit --after)
        if effective_after > 0 and root_ts:
            # Request +1 because inclusive=true includes root_ts itself
            next_msgs, has_more_after = client.fetch_history_paginated(
                channel,
                oldest=root_ts,
                inclusive=True,
                max_messages=effective_after + 1,
                max_pages=max_pages,
            )
            prev_activity_ts = float(root_ts)
            root_msg = top_messages.get(root_ts) or {}
            if root_msg.get("latest_reply"):
                prev_activity_ts = max(prev_activity_ts, float(root_msg["latest_reply"]))
            for r in threads_by_root.get(root_ts, []):
                if r.get("ts"):
                    prev_activity_ts = max(prev_activity_ts, float(r["ts"]))

            for m in next_msgs:
                ts = m.get("ts")
                if not ts or ts in top_messages or float(ts) <= float(root_ts):
                    continue
                if effective_gap_minutes is not None:
                    gap_seconds = float(ts) - prev_activity_ts
                    if gap_seconds > effective_gap_minutes * 60.0:
                        stopped_at_gap = {
                            "gap_minutes": round(gap_seconds / 60.0, 1),
                            "limit_minutes": effective_gap_minutes,
                        }
                        has_more_after = False
                        break
                top_messages[ts] = m
                prev_activity_ts = max(prev_activity_ts, float(ts), float(m.get("latest_reply") or ts))
    else:
        # Channel-only target: fetch latest `limit` messages
        recent_msgs, has_more_before = client.fetch_history_paginated(
            channel, max_messages=limit, max_pages=max_pages
        )
        for m in recent_msgs:
            if m.get("ts"):
                top_messages[m["ts"]] = m

    # 4) Expand every sub-thread with replies so nothing is skipped
    if expand_threads:
        for ts, m in sorted(top_messages.items(), key=lambda kv: float(kv[0])):
            if ts in threads_by_root:
                continue
            if (m.get("reply_count") or 0) > 0 or m.get("latest_reply"):
                sub, trunc = client.fetch_replies_all(channel, ts, max_pages=max_pages)
                if trunc:
                    warnings.append(f"Sub-thread {ts} hit --max-pages ({max_pages}); output may be truncated.")
                if len(sub) > 1:
                    threads_by_root[ts] = sub[1:]

    # 5) Format chronologically
    ordered_top = sorted(top_messages.values(), key=lambda m: float(m["ts"]))
    formatted_items: list[dict[str, Any]] = []
    total_replies = 0
    total_files = 0
    downloaded_files = 0
    total_deduped_attachments = 0

    for m in ordered_top:
        thread_seen_attachments: set[str] = set()
        fm, deduped = format_message(
            m,
            client,
            anchor_ts=target_ts,
            download_files=download_files,
            download_dir=out_dir,
            max_file_bytes=max_file_bytes,
            seen_attachments=thread_seen_attachments,
            dedup_attachments=dedup_attachments,
        )
        total_deduped_attachments += deduped
        total_files += len(fm["files"])
        downloaded_files += sum(1 for f in fm["files"] if f["status"] == "downloaded")

        reply_items: list[dict[str, Any]] = []
        for rm in threads_by_root.get(m["ts"], []):
            frm, r_deduped = format_message(
                rm,
                client,
                anchor_ts=target_ts,
                download_files=download_files,
                download_dir=out_dir,
                max_file_bytes=max_file_bytes,
                seen_attachments=thread_seen_attachments,
                dedup_attachments=dedup_attachments,
            )
            total_deduped_attachments += r_deduped
            reply_items.append(frm)
            total_replies += 1
            total_files += len(frm["files"])
            downloaded_files += sum(1 for f in frm["files"] if f["status"] == "downloaded")

        fm["replies"] = reply_items
        formatted_items.append(fm)

    # Infer 1:1 DM counterpart label when im:read is missing
    if target_spec["is_dm"] and channel_label == channel:
        self_uid = client.auth_info.get("user_id")
        others: list[str] = []
        for fm in formatted_items:
            for item in [fm, *(fm.get("replies") or [])]:
                uid = item.get("user_id")
                author = item.get("author") or ""
                if uid and uid == self_uid:
                    continue
                if author and author != "unknown" and author not in others:
                    others.append(author)
        if len(others) == 1:
            channel_label = f"DM with {others[0]} ({channel})"
        elif others:
            channel_label = f"DM with {', '.join(others[:3])} ({channel})"
        else:
            channel_label = f"DM ({channel})"

    if stopped_at_gap:
        gap_m = int(round(stopped_at_gap["gap_minutes"]))
        lim_m = int(round(stopped_at_gap["limit_minutes"]))
        notes.append(
            f"Stopped follow-up messages at a {gap_m}m gap (limit: {lim_m}m). "
            f"Pass --after {effective_after} to read past the gap."
        )
    if has_more_after:
        warnings.append(
            f"More newer messages exist after this window (--after {effective_after}). Rerun with a higher --after value to read further."
        )
    if client.files_scope_missing and total_files > downloaded_files:
        warnings.append(
            "Attachment downloads were blocked by missing Slack OAuth scope 'files:read' on the configured Slack token."
        )

    return {
        "target": target_spec["raw"],
        "channel": channel,
        "channel_label": channel_label,
        "target_ts": target_ts,
        "root_ts": root_ts,
        "mode": {
            "before": before,
            "after": effective_after,
            "after_gap_minutes": effective_gap_minutes,
            "expand_threads": expand_threads,
            "download_files": download_files,
            "dedup_attachments": dedup_attachments,
        },
        "counts": {
            "top_messages": len(formatted_items),
            "thread_replies": total_replies,
            "total_messages": len(formatted_items) + total_replies,
            "files": total_files,
            "files_downloaded": downloaded_files,
            "attachments_deduped": total_deduped_attachments,
        },
        "stopped_at_gap": stopped_at_gap,
        "notes": notes,
        "warnings": warnings,
        "messages": formatted_items,
    }


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    c = report["counts"]
    lines.append(f"=== Slack: {report['channel_label']} ===")
    lines.append(f"Source: {report['target']}")
    dedup_part = (
        f", {c['attachments_deduped']} duplicate unfurl(s) omitted"
        if c.get("attachments_deduped")
        else ""
    )
    lines.append(
        f"Fetched: {c['total_messages']} messages ({c['top_messages']} top-level, {c['thread_replies']} thread replies), "
        f"{c['files']} files ({c['files_downloaded']} downloaded){dedup_part} | "
        f"window: before={report['mode']['before']}, after={report['mode']['after']}"
    )
    for w in report.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    for n in report.get("notes") or []:
        lines.append(f"Note: {n}")
    lines.append("")

    for msg in report["messages"]:
        _append_msg_lines(lines, msg, indent="")
        for reply in msg.get("replies") or []:
            _append_msg_lines(lines, reply, indent="  ↳ ")

    if report["target_ts"] and report["mode"]["after"] == 0:
        lines.append("")
        lines.append("Tip: pass --after 20 to include subsequent channel messages and their sub-threads, or --before 10 for prior context.")
    return "\n".join(lines).rstrip() + "\n"


def _append_msg_lines(lines: list[str], msg: dict[str, Any], indent: str) -> None:
    tags: list[str] = []
    if msg["is_anchor"]:
        tags.append("LINKED")
    if msg["subtype"]:
        tags.append(msg["subtype"])
    if msg["edited"]:
        tags.append("edited")
    if not indent and msg["reply_count"]:
        tags.append(f"{msg['reply_count']} replies")
    tag_str = f" [{' | '.join(tags)}]" if tags else ""

    lines.append(f"{indent}[{msg['time']}] {msg['author']} (ts:{msg['ts']}){tag_str}")
    body_indent = indent + "  " if not indent else "      "

    if msg["text"]:
        for ln in msg["text"].splitlines():
            lines.append(f"{body_indent}{ln}")
    elif not msg["extra_blocks"] and not msg["attachments"] and not msg["files"]:
        lines.append(f"{body_indent}(empty message)")

    if msg["extra_blocks"]:
        lines.append(f"{body_indent}[blocks]")
        for ln in msg["extra_blocks"].splitlines():
            lines.append(f"{body_indent}  {ln}")

    for att in msg["attachments"]:
        lines.append(f"{body_indent}{att}")

    for f in msg["files"]:
        meta = f"{f['name']} ({f['mimetype'] or f['filetype'] or 'file'}, {_fmt_size(f['size'])}, id:{f['id']})"
        if f["local_path"]:
            lines.append(f"{body_indent}[file] {meta} -> saved: {f['local_path']}")
        else:
            reason = f" ({f['reason']})" if f["reason"] else ""
            link = f" | permalink: {f['permalink']}" if f["permalink"] else ""
            lines.append(f"{body_indent}[file] {meta}{reason}{link}")

    if msg["reactions"]:
        lines.append(f"{body_indent}[reactions] {', '.join(msg['reactions'])}")


def cmd_check_scopes(client: SlackClient, as_json: bool) -> int:
    auth = client.auth_test()
    granted = sorted(client.last_scopes)
    present = []
    missing = []
    for scope, desc in RECOMMENDED_SCOPES:
        entry = {"scope": scope, "purpose": desc}
        if scope in client.last_scopes:
            present.append(entry)
        else:
            missing.append(entry)

    payload = {
        "team": auth.get("team"),
        "url": auth.get("url"),
        "user": auth.get("user"),
        "user_id": auth.get("user_id"),
        "granted_scopes": granted,
        "recommended_present": present,
        "recommended_missing": missing,
    }
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Authenticated: {payload['user']} ({payload['user_id']}) @ {payload['team']} ({payload['url']})")
    print(f"Granted scopes ({len(granted)}): {', '.join(granted) if granted else '(none returned)'}")
    print("\nRecommended scopes present:")
    for item in present:
        print(f"  + {item['scope']}: {item['purpose']}")
    print("\nRecommended scopes missing:")
    if not missing:
        print("  (none — all recommended scopes are granted)")
    else:
        for item in missing:
            print(f"  - {item['scope']}: {item['purpose']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slack-read",
        description="Read Slack messages, threads, follow-up messages, and attachments via the Slack Web API.",
    )
    p.add_argument("targets", nargs="*", help="Slack message/channel URL(s) or channel ID(s)")
    p.add_argument("--ts", help="Explicit Slack timestamp (e.g. 1700000000.123456) when passing a channel ID")
    p.add_argument(
        "--after",
        type=int,
        default=None,
        help="Number of subsequent top-level messages to include (default: 20 for DMs, 0 for channels)",
    )
    p.add_argument(
        "--after-gap-minutes",
        type=float,
        default=None,
        help="Stop follow-up messages when the gap exceeds N minutes (default: 30 for automatic DM follow-ups; 0 disables)",
    )
    p.add_argument("--before", type=int, default=0, help="Number of preceding top-level messages to include (default: 0)")
    p.add_argument("--limit", type=int, default=20, help="Messages to fetch for channel-only targets (default: 20)")
    p.add_argument("--no-threads", action="store_true", help="Do not expand sub-threads on fetched messages")
    p.add_argument("--no-download", action="store_true", help="Do not attempt to download attached files")
    p.add_argument(
        "--no-dedup-attachments",
        action="store_true",
        help="Keep identical repeated link-unfurl attachments within a thread",
    )
    p.add_argument("--download-dir", type=Path, default=None, help="Directory for downloaded attachments")
    p.add_argument("--max-file-mb", type=float, default=25.0, help="Maximum single attachment size to download in MB (default: 25)")
    p.add_argument("--max-pages", type=int, default=50, help="Maximum pagination pages per API query (default: 50)")
    p.add_argument("--env-file", type=Path, default=None, help="Env file to read the Slack token from (default: ~/.config/slack-read/env)")
    p.add_argument("--token-env", default=None, help="Environment variable name holding the Slack token (default: SLACK_READ_TOKEN)")
    p.add_argument("--json", action="store_true", dest="as_json", help="Emit structured JSON instead of compact text")
    p.add_argument("--check-scopes", action="store_true", help="Check token auth and compare granted vs recommended OAuth scopes")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.check_scopes and not args.targets:
        parser.print_help(sys.stderr)
        return 2

    try:
        token = load_token(env_file=args.env_file, token_env=args.token_env)
        client = SlackClient(token)
        if args.check_scopes:
            return cmd_check_scopes(client, args.as_json)

        # Warm auth cache so authenticated user's own ID resolves cleanly even without users:read
        client.auth_test()

        reports = []
        max_bytes = int(args.max_file_mb * 1024 * 1024)
        for raw_target in args.targets:
            spec = parse_target(raw_target, explicit_ts=args.ts if len(args.targets) == 1 else None)
            report = fetch_target(
                client,
                spec,
                after=args.after,
                before=args.before,
                limit=args.limit,
                expand_threads=not args.no_threads,
                download_files=not args.no_download,
                download_dir=args.download_dir,
                max_file_bytes=max_bytes,
                max_pages=args.max_pages,
                after_gap_minutes=args.after_gap_minutes,
                dedup_attachments=not args.no_dedup_attachments,
            )
            reports.append(report)

        if args.as_json:
            print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2))
        else:
            for idx, rep in enumerate(reports):
                if idx > 0:
                    print("\n" + "=" * 60 + "\n")
                print(render_text(rep), end="")
        return 0
    except SlackError as e:
        print(f"slack-read error: {e}", file=sys.stderr)
        if e.error_code == "missing_scope" and e.needed:
            print(
                f"Hint: add '{e.needed}' to your Slack app's User Token Scopes, reinstall the app, and update your token.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
