import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app


class SkillConfirmationTests(unittest.TestCase):
    def make_registry(self, library: Path) -> tuple[dict[str, object], Path]:
        skill_dir = library / "review-demo"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: review-demo\ndescription: Review a demo.\n---\n\n# Demo\n",
            encoding="utf-8",
        )
        registry: dict[str, object] = {
            "skills": {
                "review-demo": {
                    "name": "review-demo",
                    "enabled": True,
                    "system": False,
                    "status": "ok",
                    "libraryPath": str(skill_dir),
                    "codexPath": "",
                    "skillMdPath": str(skill_md),
                }
            }
        }
        return registry, skill_md

    def test_confirming_preserves_enabled_state_and_records_current_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, _ = self.make_registry(library)

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry") as save_registry,
                patch.object(app, "append_audit") as append_audit,
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                result = app.confirm_skill("review-demo")

            entry = registry["skills"]["review-demo"]
            self.assertTrue(entry["enabled"])
            self.assertIn("confirmedAt", entry["confirmation"])
            self.assertEqual(len(entry["confirmation"]["sourceSha256"]), 64)
            self.assertIn("退出待确认队列", result["message"])
            save_registry.assert_called_once_with(registry)
            append_audit.assert_called_once()

    def test_changed_skill_md_requires_confirmation_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, skill_md = self.make_registry(library)
            entry = registry["skills"]["review-demo"]

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry"),
                patch.object(app, "append_audit"),
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                app.confirm_skill("review-demo")
                self.assertEqual(app.skill_confirmation_view("review-demo", entry)["status"], "confirmed")
                skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nUpdated guidance.\n", encoding="utf-8")
                changed = app.skill_confirmation_view("review-demo", entry)

            self.assertEqual(changed["status"], "needs-review")
            self.assertFalse(changed["confirmed"])
            self.assertNotEqual(changed["sourceSha256"], changed["currentSourceSha256"])

    def test_confirmation_hash_uses_exact_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, skill_md = self.make_registry(library)
            entry = registry["skills"]["review-demo"]

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry"),
                patch.object(app, "append_audit"),
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                app.confirm_skill("review-demo")
                original_text = skill_md.read_text(encoding="utf-8")
                skill_md.write_bytes(b"\xef\xbb\xbf" + original_text.replace("\n", "\r\n").encode("utf-8"))
                changed = app.skill_confirmation_view("review-demo", entry)

            self.assertEqual(changed["status"], "needs-review")

    def test_duplicate_confirmation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, _ = self.make_registry(library)

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry") as save_registry,
                patch.object(app, "append_audit") as append_audit,
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                app.confirm_skill("review-demo")
                second = app.confirm_skill("review-demo")

            self.assertEqual(second["message"], "该技能已确认。")
            save_registry.assert_called_once_with(registry)
            append_audit.assert_called_once()

    def test_concurrent_confirmation_requests_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, _ = self.make_registry(library)
            activity_lock = threading.Lock()
            active = 0
            max_active = 0

            def read_registry(*, save: bool = False) -> dict[str, object]:
                nonlocal active, max_active
                self.assertFalse(save)
                with activity_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.02)
                with activity_lock:
                    active -= 1
                return registry

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", side_effect=read_registry),
                patch.object(app, "save_registry") as save_registry,
                patch.object(app, "append_audit") as append_audit,
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: app.confirm_skill("review-demo"), range(2)))

            self.assertEqual(max_active, 1)
            self.assertEqual(save_registry.call_count, 1)
            self.assertEqual(append_audit.call_count, 1)
            self.assertEqual(
                {result["message"] for result in results},
                {"已确认，该技能将退出待确认队列。", "该技能已确认。"},
            )

    def test_audit_failure_rolls_back_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, _ = self.make_registry(library)
            entry = registry["skills"]["review-demo"]

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry") as save_registry,
                patch.object(app, "append_audit", side_effect=OSError("disk full")),
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    app.confirm_skill("review-demo")

            self.assertNotIn("confirmation", entry)
            self.assertEqual(save_registry.call_count, 2)

    def test_unconfirming_preserves_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "skills"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            registry, _ = self.make_registry(library)
            entry = registry["skills"]["review-demo"]
            entry["confirmation"] = {
                "confirmedAt": "2026-08-24T10:00:00+08:00",
                "sourceSha256": "a" * 64,
            }

            with (
                patch.object(app, "sync_registry", return_value=registry),
                patch.object(app, "save_registry"),
                patch.object(app, "append_audit") as append_audit,
                patch.object(app, "registry_view", side_effect=lambda value: value),
            ):
                result = app.unconfirm_skill("review-demo")

            self.assertTrue(entry["enabled"])
            self.assertNotIn("confirmation", entry)
            self.assertIn("重新进入待确认队列", result["message"])
            append_audit.assert_called_once()

    def test_duplicate_unconfirm_is_idempotent(self) -> None:
        registry = {
            "skills": {
                "review-demo": {
                    "name": "review-demo",
                    "enabled": True,
                    "system": False,
                    "status": "ok",
                    "confirmation": {
                        "confirmedAt": "2026-08-24T10:00:00+08:00",
                        "sourceSha256": "a" * 64,
                    },
                }
            }
        }
        with (
            patch.object(app, "sync_registry", return_value=registry),
            patch.object(app, "save_registry") as save_registry,
            patch.object(app, "append_audit") as append_audit,
            patch.object(app, "registry_view", side_effect=lambda value: value),
        ):
            app.unconfirm_skill("review-demo")
            second = app.unconfirm_skill("review-demo")

        self.assertEqual(second["message"], "该技能尚未确认。")
        self.assertEqual(save_registry.call_count, 1)
        self.assertEqual(append_audit.call_count, 1)

    def test_unconfirm_audit_failure_restores_previous_confirmation(self) -> None:
        previous = {
            "confirmedAt": "2026-08-24T10:00:00+08:00",
            "sourceSha256": "a" * 64,
        }
        registry = {
            "skills": {
                "review-demo": {
                    "name": "review-demo",
                    "enabled": True,
                    "system": False,
                    "status": "ok",
                    "confirmation": dict(previous),
                }
            }
        }
        entry = registry["skills"]["review-demo"]
        with (
            patch.object(app, "sync_registry", return_value=registry),
            patch.object(app, "save_registry") as save_registry,
            patch.object(app, "append_audit", side_effect=OSError("disk full")),
            patch.object(app, "registry_view", side_effect=lambda value: value),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                app.unconfirm_skill("review-demo")

        self.assertEqual(entry["confirmation"], previous)
        self.assertEqual(save_registry.call_count, 2)

    def test_system_skill_is_not_confirmable(self) -> None:
        registry = {
            "skills": {
                "system-demo": {
                    "name": "system-demo",
                    "enabled": True,
                    "system": True,
                    "status": "ok",
                }
            }
        }
        with patch.object(app, "sync_registry", return_value=registry):
            with self.assertRaisesRegex(app.ApiError, "不进入人工确认队列"):
                app.confirm_skill("system-demo")
            with self.assertRaisesRegex(app.ApiError, "不进入人工确认队列"):
                app.unconfirm_skill("system-demo")

    def test_only_actionable_enabled_skills_are_pending(self) -> None:
        base = {"enabled": True, "system": False}
        self.assertTrue(app.is_pending_confirmation({**base, "confirmation": {"status": "unconfirmed"}}))
        self.assertTrue(app.is_pending_confirmation({**base, "confirmation": {"status": "needs-review"}}))
        self.assertFalse(app.is_pending_confirmation({**base, "confirmation": {"status": "confirmed"}}))
        self.assertFalse(app.is_pending_confirmation({**base, "confirmation": {"status": "unavailable"}}))
        self.assertFalse(app.is_pending_confirmation({**base, "enabled": False, "confirmation": {"status": "unconfirmed"}}))
        self.assertFalse(app.is_pending_confirmation({**base, "system": True, "confirmation": {"status": "unconfirmed"}}))


if __name__ == "__main__":
    unittest.main()