import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from usage_stats import UsageStatsService


def write_jsonl(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")


class UsageStatsMultiSourceTests(unittest.TestCase):
    def make_service(self, root: Path, registry: dict) -> UsageStatsService:
        settings = {
            "usageStats": {
                "enabled": True,
                "dailyEnabled": False,
                "staleDays": 30,
                "maxFiles": 20,
                "scope": "all",
                "includeSystem": True,
            }
        }
        return UsageStatsService(
            stats_file=root / "usage.json",
            sessions_dir=root / "codex" / "sessions",
            archived_sessions_dir=root / "codex" / "archived_sessions",
            pi_sessions_dir=root / "pi" / "sessions",
            session_index_file=root / "codex" / "session_index.jsonl",
            read_settings=lambda: settings,
            write_settings=lambda value: settings.update(value),
            read_registry_state=lambda: registry,
            append_audit=lambda _action, _details: None,
            safe_skill_name=lambda name: name,
        )

    def test_merges_codex_and_pi_and_deduplicates_forked_pi_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timestamp = datetime.now(timezone.utc).isoformat()
            registry = {
                "skills": {
                    "demo": {"enabled": True, "managed": True, "category": "测试"},
                    "nested": {"enabled": True, "managed": True, "category": "测试"},
                    "commanded": {"enabled": True, "managed": True, "category": "测试"},
                    "not-read": {"enabled": True, "managed": True, "category": "测试"},
                }
            }
            codex_call = {
                "type": "response_item",
                "timestamp": timestamp,
                "payload": {
                    "type": "function_call",
                    "name": "read",
                    "call_id": "shared-call",
                    "arguments": json.dumps({"path": "/repo/skills/demo/SKILL.md"}),
                },
            }
            write_jsonl(
                root / "codex" / "sessions" / "shared-session.jsonl",
                [
                    {"type": "session_meta", "payload": {"id": "shared-session"}},
                    codex_call,
                ],
            )

            pi_header = {
                "type": "session",
                "version": 3,
                "id": "shared-session",
                "timestamp": timestamp,
                "cwd": "/repo",
            }
            pi_message = {
                "type": "message",
                "id": "assistant-entry",
                "timestamp": timestamp,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "shared-call",
                            "name": "read",
                            "arguments": {"path": "/home/user/.pi/agent/skills/demo/SKILL.md"},
                        },
                        {
                            "type": "toolCall",
                            "id": "parallel-call",
                            "name": "multi_tool_use.parallel",
                            "arguments": {
                                "tool_uses": [
                                    {
                                        "recipient_name": "functions.read",
                                        "parameters": {"path": "/home/user/.pi/agent/skills/nested/SKILL.md"},
                                    },
                                    {
                                        "recipient_name": "functions.edit",
                                        "parameters": {"path": "/repo/file", "newText": "skills/not-read/SKILL.md"},
                                    },
                                ]
                            },
                        },
                    ],
                },
            }
            pi_items = [
                pi_header,
                pi_message,
                {
                    "type": "message",
                    "id": "skill-command-entry",
                    "timestamp": timestamp,
                    "message": {
                        "role": "user",
                        "content": '<skill name="commanded" location="/home/user/.pi/agent/skills/commanded/SKILL.md">\nBody\n</skill>',
                    },
                },
                {"type": "session_info", "id": "title", "timestamp": timestamp, "name": "Pi 技能测试"},
            ]
            original_path = root / "pi" / "sessions" / "project" / "pi-one.jsonl"
            write_jsonl(original_path, pi_items)
            fork_header = {**pi_header, "id": "fork-session", "parentSession": str(original_path)}
            write_jsonl(root / "pi" / "sessions" / "project" / "pi-fork.jsonl", [fork_header, *pi_items[1:]])
            independent_message = {
                **pi_message,
                "id": "independent-entry",
                "message": {**pi_message["message"], "content": [pi_message["message"]["content"][0]]},
            }
            write_jsonl(
                root / "pi" / "sessions" / "project" / "pi-independent.jsonl",
                [{**pi_header, "id": "independent-session"}, independent_message],
            )

            payload = self.make_service(root, registry).review({"scope": "all", "maxFiles": 20})
            entries = {item["name"]: item for item in payload["entries"]}

            self.assertEqual(payload["version"], 2)
            self.assertEqual(entries["demo"]["confirmedEvidenceCount"], 3)
            self.assertEqual(entries["demo"]["confirmedSessionCount"], 3)
            self.assertEqual({item["source"] for item in entries["demo"]["evidence"]}, {"codex", "pi"})
            self.assertEqual(entries["nested"]["confirmedEvidenceCount"], 1)
            self.assertEqual(entries["nested"]["evidence"][0]["title"], "Pi 技能测试")
            self.assertEqual(entries["commanded"]["confirmedEvidenceCount"], 1)
            self.assertEqual(entries["commanded"]["evidence"][0]["type"], "skill-command-load")
            self.assertEqual(entries["not-read"]["confirmedEvidenceCount"], 0)
            self.assertEqual(payload["scan"]["sources"]["codex"]["scannedFiles"], 1)
            self.assertEqual(payload["scan"]["sources"]["pi"]["scannedFiles"], 3)

    def test_version_one_cache_is_marked_outdated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = {"skills": {}}
            service = self.make_service(root, registry)
            service.stats_file.write_text('{"version": 1, "reviewedAt": "2026-01-01T00:00:00Z"}\n', encoding="utf-8")

            payload = service.read_stats()

            self.assertTrue(payload["outdated"])
            self.assertEqual(payload["version"], 2)
            self.assertIn("Codex 与 Pi", payload["evidencePolicy"])


if __name__ == "__main__":
    unittest.main()