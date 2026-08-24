import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from session_logs import extract_message_text, source_session_files


class MultiSourceSessionLogTests(unittest.TestCase):
    def test_extracts_pi_user_and_assistant_text_but_not_tool_results(self) -> None:
        user = {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "使用 demo"}]}}
        assistant = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "正在使用 demo skill"},
                    {"type": "toolCall", "name": "read", "arguments": {"path": "SKILL.md"}},
                ],
            },
        }
        tool_result = {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "demo"}]}}

        self.assertEqual(extract_message_text(user), ("使用 demo", "user"))
        self.assertEqual(extract_message_text(assistant), ("正在使用 demo skill", "assistant"))
        self.assertEqual(extract_message_text(tool_result), ("", "toolResult"))

    def test_file_limit_is_applied_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory, names in (
                (root / "codex" / "sessions", ["one", "two"]),
                (root / "pi" / "sessions", ["three", "four"]),
            ):
                directory.mkdir(parents=True)
                for name in names:
                    (directory / f"{name}.jsonl").write_text(json.dumps({"type": "session"}) + "\n", encoding="utf-8")

            files = source_session_files(
                root / "codex" / "sessions",
                root / "codex" / "archived",
                root / "pi" / "sessions",
                limit_per_source=1,
            )

            self.assertEqual(len(files), 2)
            self.assertEqual({item.source for item in files}, {"codex", "pi"})

    def test_skill_context_search_includes_pi_source_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pi_file = root / "pi" / "project" / "session.jsonl"
            pi_file.parent.mkdir(parents=True)
            items = [
                {"type": "session", "version": 3, "id": "pi-session", "timestamp": "2026-08-24T00:00:00Z"},
                {
                    "type": "message",
                    "id": "user-entry",
                    "timestamp": "2026-08-24T00:00:01Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "请完成当前任务"}]},
                },
                {
                    "type": "message",
                    "id": "assistant-entry",
                    "timestamp": "2026-08-24T00:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "read-demo",
                                "name": "read",
                                "arguments": {"path": "/home/user/.pi/agent/skills/demo/SKILL.md"},
                            }
                        ],
                    },
                },
                {"type": "session_info", "id": "title", "timestamp": "2026-08-24T00:00:02Z", "name": "Pi 上下文"},
            ]
            pi_file.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
            registry = {"skills": {"demo": {"frontmatter": {"name": "demo"}}}}

            with (
                patch.object(app, "CODEX_SESSIONS_DIR", root / "codex" / "sessions"),
                patch.object(app, "CODEX_ARCHIVED_SESSIONS_DIR", root / "codex" / "archived"),
                patch.object(app, "PI_SESSIONS_DIR", root / "pi"),
                patch.object(app, "read_registry_state", return_value=registry),
                patch.object(app, "read_session_index", return_value={}),
            ):
                payload = app.search_skill_contexts("demo", {})

            self.assertEqual(payload["matchedSessionCount"], 1)
            self.assertEqual(payload["results"][0]["source"], "pi")
            self.assertEqual(payload["results"][0]["title"], "Pi 上下文")
            self.assertEqual(payload["results"][0]["snippets"][0]["text"], "请完成当前任务")
            self.assertIn("Codex 与 Pi", payload["summary"])

            with (
                patch.object(app, "CODEX_SESSIONS_DIR", root / "codex" / "sessions"),
                patch.object(app, "CODEX_ARCHIVED_SESSIONS_DIR", root / "codex" / "archived"),
                patch.object(app, "PI_SESSIONS_DIR", root / "pi"),
                patch.object(app, "read_registry_state", return_value=registry),
                patch.object(app, "read_session_index", return_value={}),
            ):
                filtered = app.search_skill_contexts("demo", {"q": ["不存在的附加条件"]})
            self.assertEqual(filtered["matchedSessionCount"], 0)

    def test_codex_context_deduplicates_response_and_event_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_file = root / "codex" / "sessions" / "session.jsonl"
            codex_file.parent.mkdir(parents=True)
            items = [
                {"type": "session_meta", "payload": {"id": "codex-session"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-08-24T00:00:00Z",
                    "payload": {
                        "type": "function_call",
                        "name": "read",
                        "call_id": "read-demo",
                        "arguments": json.dumps({"path": "/home/user/.codex/skills/demo/SKILL.md"}),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-08-24T00:00:01Z",
                    "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "任务已完成"}]},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-24T00:00:01Z",
                    "payload": {"type": "agent_message", "message": "任务已完成"},
                },
            ]
            codex_file.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
            registry = {"skills": {"demo": {"frontmatter": {"name": "demo"}}}}

            with (
                patch.object(app, "CODEX_SESSIONS_DIR", root / "codex" / "sessions"),
                patch.object(app, "CODEX_ARCHIVED_SESSIONS_DIR", root / "codex" / "archived"),
                patch.object(app, "PI_SESSIONS_DIR", root / "pi"),
                patch.object(app, "read_registry_state", return_value=registry),
                patch.object(app, "read_session_index", return_value={"codex-session": {"thread_name": "Codex 上下文"}}),
            ):
                payload = app.search_skill_contexts("demo", {})

            self.assertEqual(payload["matchedSessionCount"], 1)
            self.assertEqual(payload["results"][0]["source"], "codex")
            self.assertEqual([item["text"] for item in payload["results"][0]["snippets"]], ["任务已完成"])


if __name__ == "__main__":
    unittest.main()