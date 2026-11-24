#!/usr/bin/env python3
"""Unit tests for slack-read CLI, prompt hook, and Pi extension."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import slack_prompt_hook  # noqa: E402
import slack_read  # noqa: E402


class FakeSlackClient(slack_read.SlackClient):
    """In-memory SlackClient stub for deterministic unit tests."""

    def __init__(
        self,
        *,
        replies_map: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        users: dict[str, str] | None = None,
        bots: dict[str, str] | None = None,
        channels: dict[str, dict[str, Any]] | None = None,
        auth_info: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("fake-test-token")
        self.replies_map = replies_map or {}
        self.history_messages = history_messages or []
        self.fake_users = users or {}
        self.fake_bots = bots or {}
        self.fake_channels = channels or {}
        self.auth_info = auth_info or {"user_id": "U000SELF01", "user": "selfuser"}
        self.api_calls: list[tuple[str, dict[str, Any]]] = []

    def api_get(self, method: str, params: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        params = dict(params or {})
        self.api_calls.append((method, params))

        if method == "auth.test":
            return dict(self.auth_info)

        if method == "conversations.info":
            ch_id = params["channel"]
            ch = self.fake_channels.get(ch_id)
            if ch is None:
                raise slack_read.SlackError("channel_not_found", "channel_not_found")
            return {"ok": True, "channel": ch}

        if method == "users.info":
            uid = params["user"]
            if uid in self.fake_users:
                return {"ok": True, "user": {"id": uid, "profile": {"display_name": self.fake_users[uid]}}}
            raise slack_read.SlackError("user_not_found", "user_not_found")

        if method == "bots.info":
            bid = params["bot"]
            if bid in self.fake_bots:
                return {"ok": True, "bot": {"id": bid, "name": self.fake_bots[bid]}}
            raise slack_read.SlackError("bot_not_found", "bot_not_found")

        if method == "conversations.replies":
            key = (params["channel"], params["ts"])
            return {"ok": True, "messages": list(self.replies_map.get(key, [])), "has_more": False}

        if method == "conversations.history":
            oldest = float(params["oldest"]) if params.get("oldest") is not None else None
            latest = float(params["latest"]) if params.get("latest") is not None else None
            inclusive = params.get("inclusive") == "true"
            limit = int(params.get("limit", 200))

            filtered = []
            for m in self.history_messages:
                ts_f = float(m["ts"])
                if oldest is not None:
                    if inclusive and ts_f < oldest:
                        continue
                    if not inclusive and ts_f <= oldest:
                        continue
                if latest is not None:
                    if inclusive and ts_f > latest:
                        continue
                    if not inclusive and ts_f >= latest:
                        continue
                filtered.append(m)

            filtered.sort(key=lambda m: float(m["ts"]))
            has_more = len(filtered) > limit
            return {
                "ok": True,
                "messages": filtered[:limit],
                "has_more": has_more,
                "response_metadata": {"next_cursor": "more" if has_more else ""},
            }

        raise AssertionError(f"Unexpected API method: {method}")


class TestTargetParsing(unittest.TestCase):
    def test_archive_message_url(self) -> None:
        spec = slack_read.parse_target(
            "https://example.slack.com/archives/C0123456789/p1700000000123456"
        )
        self.assertEqual(spec["channel"], "C0123456789")
        self.assertEqual(spec["target_ts"], "1700000000.123456")
        self.assertIsNone(spec["thread_ts"])
        self.assertFalse(spec["is_dm"])

    def test_archive_reply_url_with_thread_ts_and_punctuation(self) -> None:
        spec = slack_read.parse_target(
            "<https://example.slack.com/archives/D0123456789/p1700000100000001?thread_ts=1700000000.000001&cid=D0123456789|link>"
        )
        self.assertEqual(spec["channel"], "D0123456789")
        self.assertEqual(spec["target_ts"], "1700000100.000001")
        self.assertEqual(spec["thread_ts"], "1700000000.000001")
        self.assertTrue(spec["is_dm"])

    def test_client_url(self) -> None:
        spec = slack_read.parse_target(
            "https://app.slack.com/client/T0123456789/C0123456789/thread/C0123456789-1700000000.123456"
        )
        self.assertEqual(spec["channel"], "C0123456789")
        self.assertEqual(spec["target_ts"], "1700000000.123456")

    def test_raw_channel_id_with_explicit_ts(self) -> None:
        spec = slack_read.parse_target("c0123456789", explicit_ts="1700000000.123456")
        self.assertEqual(spec["channel"], "C0123456789")
        self.assertEqual(spec["target_ts"], "1700000000.123456")

    def test_invalid_target_raises(self) -> None:
        with self.assertRaises(slack_read.SlackError):
            slack_read.parse_target("https://example.com/not-slack")


class TestTokenLoading(unittest.TestCase):
    def test_env_var_precedence(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SLACK_READ_TOKEN": '"fake-from-env"', "SLACK_TOKEN": "fake-fallback"},
            clear=True,
        ):
            self.assertEqual(slack_read.load_token(), "fake-from-env")

    def test_explicit_env_file_overrides_ambient_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            env_path.write_text("export SLACK_READ_TOKEN='fake-from-file'\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SLACK_READ_TOKEN": "fake-from-env"}, clear=True):
                self.assertEqual(slack_read.load_token(env_file=env_path), "fake-from-file")

    def test_single_hop_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets_file = Path(tmp) / "secrets.env"
            secrets_file.write_text("MY_CUSTOM_SLACK_VAR=fake-pointed-token\n", encoding="utf-8")
            pointer_file = Path(tmp) / "config.env"
            pointer_file.write_text(
                f"SLACK_READ_ENV_FILE={secrets_file}\nSLACK_READ_TOKEN_ENV=MY_CUSTOM_SLACK_VAR\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(slack_read.load_token(env_file=pointer_file), "fake-pointed-token")

    def test_missing_token_raises_generic_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_file = Path(tmp) / "does-not-exist.env"
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(slack_read.SlackError) as ctx:
                    slack_read.load_token(env_file=missing_file)
                self.assertIn("SLACK_READ_TOKEN", str(ctx.exception))


class TestDMGapAndAttachmentDedup(unittest.TestCase):
    def test_dm_default_30m_gap_stops_noise_but_keeps_active_followups(self) -> None:
        t0 = 1700000000.0
        root = {"ts": f"{t0:.6f}", "user": "U000SELF01", "text": "Initial question"}
        msg_10m = {"ts": f"{t0 + 600:.6f}", "user": "U000PEER01", "text": "Follow-up 10m later"}
        msg_25m = {"ts": f"{t0 + 1500:.6f}", "user": "U000PEER01", "text": "Still active 15m later"}
        msg_100m = {"ts": f"{t0 + 6000:.6f}", "user": "U000PEER01", "text": "Unrelated thread 75m later"}

        client = FakeSlackClient(
            replies_map={("D0123456789", root["ts"]): [root]},
            history_messages=[root, msg_10m, msg_25m, msg_100m],
            users={"U000PEER01": "Alice"},
            channels={"D0123456789": {"is_im": True, "user": "U000PEER01"}},
        )
        spec = slack_read.parse_target("https://example.slack.com/archives/D0123456789/p1700000000000000")

        # Default automatic DM fetch stops at the 75m gap
        report = slack_read.fetch_target(
            client,
            spec,
            after=None,
            before=0,
            limit=20,
            expand_threads=True,
            download_files=False,
            download_dir=None,
            max_file_bytes=1024 * 1024,
            max_pages=5,
        )
        texts = [m["text"] for m in report["messages"]]
        self.assertEqual(texts, ["Initial question", "Follow-up 10m later", "Still active 15m later"])
        self.assertIsNotNone(report["stopped_at_gap"])
        self.assertEqual(report["stopped_at_gap"]["gap_minutes"], 75.0)
        rendered = slack_read.render_text(report)
        self.assertIn("Note: Stopped follow-up messages at a 75m gap (limit: 30m).", rendered)

        # Explicit --after disables the default gap cutoff so all messages are returned
        report_explicit = slack_read.fetch_target(
            client,
            spec,
            after=20,
            before=0,
            limit=20,
            expand_threads=True,
            download_files=False,
            download_dir=None,
            max_file_bytes=1024 * 1024,
            max_pages=5,
        )
        self.assertEqual(len(report_explicit["messages"]), 4)
        self.assertIsNone(report_explicit["stopped_at_gap"])

    def test_thread_attachment_unfurl_dedup_and_opt_out(self) -> None:
        unfurl = {
            "title": "SEC-100 Example Issue",
            "title_link": "https://linear.app/example/issue/SEC-100",
            "text": "Repeated link unfurl body",
        }
        root = {
            "ts": "1700000000.000001",
            "user": "U000PEER01",
            "text": "Tracking https://linear.app/example/issue/SEC-100",
            "reply_count": 2,
            "attachments": [unfurl],
        }
        reply_dup = {
            "ts": "1700000010.000001",
            "thread_ts": root["ts"],
            "user": "U000SELF01",
            "text": "Re-referencing https://linear.app/example/issue/SEC-100",
            "attachments": [unfurl],
        }
        reply_attachment_only = {
            "ts": "1700000020.000001",
            "thread_ts": root["ts"],
            "bot_id": "B0123456789",
            "text": "",
            "attachments": [unfurl],
        }

        client = FakeSlackClient(
            replies_map={("C0123456789", root["ts"]): [root, reply_dup, reply_attachment_only]},
            users={"U000PEER01": "Alice"},
            bots={"B0123456789": "TrackerBot"},
            channels={"C0123456789": {"name": "security"}},
        )
        spec = slack_read.parse_target("https://example.slack.com/archives/C0123456789/p1700000000000001")

        report = slack_read.fetch_target(
            client,
            spec,
            after=None,
            before=0,
            limit=20,
            expand_threads=True,
            download_files=False,
            download_dir=None,
            max_file_bytes=1024 * 1024,
            max_pages=5,
            dedup_attachments=True,
        )
        root_out = report["messages"][0]
        self.assertEqual(len(root_out["attachments"]), 1)
        # Second reply had text + identical unfurl -> deduplicated
        self.assertEqual(len(root_out["replies"][0]["attachments"]), 0)
        # Third reply had ONLY the attachment -> preserved so message does not become empty
        self.assertEqual(len(root_out["replies"][1]["attachments"]), 1)
        self.assertEqual(root_out["replies"][1]["author"], "bot:TrackerBot (B0123456789)")
        self.assertEqual(report["counts"]["attachments_deduped"], 1)
        self.assertIn("1 duplicate unfurl(s) omitted", slack_read.render_text(report))

        # Opt-out keeps all copies
        report_no_dedup = slack_read.fetch_target(
            client,
            spec,
            after=None,
            before=0,
            limit=20,
            expand_threads=True,
            download_files=False,
            download_dir=None,
            max_file_bytes=1024 * 1024,
            max_pages=5,
            dedup_attachments=False,
        )
        self.assertEqual(len(report_no_dedup["messages"][0]["replies"][0]["attachments"]), 1)
        self.assertEqual(report_no_dedup["counts"]["attachments_deduped"], 0)


class TestPromptHookAndPiExtension(unittest.TestCase):
    def test_hook_extracts_and_deduplicates_urls(self) -> None:
        prompt = (
            "Check https://example.slack.com/archives/C0123456789/p1700000000123456, "
            "and https://example.slack.com/archives/C0123456789/p1700000000123456! "
            "Also https://app.slack.com/client/T0123456789/D0123456789."
        )
        urls = slack_prompt_hook.extract_slack_urls(prompt)
        self.assertEqual(
            urls,
            [
                "https://example.slack.com/archives/C0123456789/p1700000000123456",
                "https://app.slack.com/client/T0123456789/D0123456789",
            ],
        )

    def test_hook_cli_emits_hook_specific_output(self) -> None:
        hook_script = REPO_ROOT / "hooks" / "slack_prompt_hook.py"
        payload = json.dumps(
            {"prompt": "Please read https://example.slack.com/archives/D0123456789/p1700000000123456"}
        )
        proc = subprocess.run(
            [sys.executable, str(hook_script)],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        self.assertNotIn("additionalContext", out)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("slack-read", hso["additionalContext"])
        self.assertIn("30-minute gap", hso["additionalContext"])

    def test_hook_ignores_non_slack_prompt(self) -> None:
        hook_script = REPO_ROOT / "hooks" / "slack_prompt_hook.py"
        proc = subprocess.run(
            [sys.executable, str(hook_script)],
            input=json.dumps({"prompt": "Hello world"}),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(proc.stdout, "")

    def test_pi_extension_via_node(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node not installed")
        ext_path = (REPO_ROOT / "extensions" / "slack-read.js").as_uri()
        js = f"const mod = await import({json.dumps(ext_path)});\n" + """
            import assert from 'node:assert/strict';
            const events = new Map();
            mod.default({ on: (event, handler) => events.set(event, handler) });
            const request = (...messages) => events.get('context_with_system')({ messages });
            const user = content => ({ role: 'user', content });
            const system = { role: 'system', content: 'Keep this prompt' };
            const done = { role: 'assistant', stopReason: 'stop' };
            const toolUse = { role: 'assistant', stopReason: 'toolUse' };
            const link = 'https://example.slack.com/archives/C0123456789/p1700000000123456';
            const secondLink = 'https://app.slack.com/client/T0123456789/C0123456789';
            const original = user(`See ${link}.`);
            const hint = url => `Run: slack-read '${url}'`;
            const normal = request(system, original).messages;
            assert.equal(normal[0], system);
            assert.ok(normal[1].content.at(-1).text.includes(hint(link)));
            assert.equal(original.content, `See ${link}.`);

            const toolResult = { role: 'toolResult', content: secondLink };
            const steering = user([{ type: 'text', text: secondLink }, { type: 'image', data: '' }]);
            const steered = request(system, original, toolUse, toolResult, steering).messages;
            assert.ok(steered[1].content.at(-1).text.includes(hint(link)));
            assert.equal(steered[3], toolResult);
            assert.deepEqual(steered[4].content.slice(0, -1), steering.content);
            assert.ok(steered[4].content.at(-1).text.includes(hint(secondLink)));
            assert.equal(steering.content.length, 2);

            const followUp = request(system, original, done, steering, original).messages;
            assert.equal(followUp[1], original);
            assert.ok(followUp[3].content.at(-1).text.includes(hint(secondLink)));
            assert.ok(followUp[4].content.at(-1).text.includes(hint(link)));
            assert.equal(request(system, original, done, user('No links here')), undefined);
            assert.equal(request(system, { ...original, testyContextLabel: 'Earlier progress summary' }, toolResult), undefined);

            const failed = { role: 'assistant', stopReason: 'error' };
            assert.ok(request(system, original, failed).messages[1].content.at(-1).text.includes(hint(link)));
            assert.equal(request(system, original, failed, user('No links here')), undefined);
        """
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", js],
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
