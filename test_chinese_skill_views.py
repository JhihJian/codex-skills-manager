import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from token_usage import calculate_skill_token_usage


class ChineseSkillViewTests(unittest.TestCase):
    def make_registry(self, library: Path) -> dict[str, object]:
        skill_dir = library / "translate-demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: translate-demo\ndescription: Read a document.\n---\n\n"
            "# Read documents\n\nUse this skill to inspect documents.\n\n"
            "```bash\ncat source.md\n```\n\n"
            "The final instruction must remain visible.\n",
            encoding="utf-8",
        )
        return {
            "skills": {
                "translate-demo": {
                    "name": "translate-demo",
                    "enabled": True,
                    "status": "ok",
                    "libraryPath": str(skill_dir),
                    "codexPath": "",
                    "skillMdPath": str(skill_dir / "SKILL.md"),
                }
            }
        }

    def snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_generation_is_cached_outside_skill_directories_and_keeps_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            codex = root / "codex"
            cache_file = root / "data" / "chinese-skill-views.sqlite3"
            library.mkdir()
            codex.mkdir()
            registry = self.make_registry(library)
            before_registry = copy.deepcopy(registry)
            before_library = self.snapshot(library)
            before_codex = self.snapshot(codex)
            before_tokens = calculate_skill_token_usage(registry["skills"], allowed_roots=(library, codex))

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "CHINESE_SKILL_VIEW_DB_FILE", cache_file),
                patch.object(app, "append_audit"),
                patch.object(
                    app,
                    "run_codex_chinese_skill_view",
                    return_value="# 阅读文档\n\n使用此技能检查文档。\n\n```bash\ncat source.md\n```\n",
                ) as run_translation,
            ):
                result = app.generate_chinese_skill_view("translate-demo", registry=registry)
                status = app.chinese_skill_view_status("translate-demo", registry["skills"]["translate-demo"])

            self.assertTrue(result["generated"])
            self.assertEqual(status["status"], "ready")
            self.assertTrue(cache_file.exists())
            self.assertEqual(registry, before_registry)
            self.assertEqual(self.snapshot(library), before_library)
            self.assertEqual(self.snapshot(codex), before_codex)
            self.assertFalse((library / "translate-demo" / "SKILL.zh.md").exists())
            self.assertEqual(
                calculate_skill_token_usage(registry["skills"], allowed_roots=(library, codex)),
                before_tokens,
            )
            prompt = run_translation.call_args.args[1]
            self.assertIn("The final instruction must remain visible.", prompt)
            self.assertIn("cat source.md", prompt)

    def test_changed_source_marks_cached_translation_stale_without_returning_old_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            codex = root / "codex"
            cache_file = root / "data" / "chinese-skill-views.sqlite3"
            library.mkdir()
            codex.mkdir()
            registry = self.make_registry(library)

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "CHINESE_SKILL_VIEW_DB_FILE", cache_file),
                patch.object(app, "append_audit"),
                patch.object(app, "run_codex_chinese_skill_view", return_value="# 中文视图\n"),
                patch.object(app, "read_registry_state", return_value=registry),
            ):
                app.generate_chinese_skill_view("translate-demo", registry=registry)
                source = library / "translate-demo" / "SKILL.md"
                source.write_text(source.read_text(encoding="utf-8") + "\nChanged after translation.\n", encoding="utf-8")
                payload = app.get_chinese_skill_view("translate-demo", include_markdown=True)

            self.assertEqual(payload["status"], "stale")
            self.assertNotIn("markdown", payload)

    def test_reading_missing_cache_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            codex = root / "codex"
            cache_file = root / "data" / "chinese-skill-views.sqlite3"
            library.mkdir()
            codex.mkdir()
            registry = self.make_registry(library)

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "CODEX_SKILLS_DIR", codex),
                patch.object(app, "CHINESE_SKILL_VIEW_DB_FILE", cache_file),
            ):
                status = app.chinese_skill_view_status("translate-demo", registry["skills"]["translate-demo"])

            self.assertEqual(status["status"], "missing")
            self.assertFalse(cache_file.exists())


if __name__ == "__main__":
    unittest.main()