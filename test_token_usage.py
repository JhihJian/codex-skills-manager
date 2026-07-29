import tempfile
import unittest
from pathlib import Path

from token_usage import calculate_skill_token_usage, estimate_token_count


class SkillTokenUsageTests(unittest.TestCase):
    def test_counts_only_enabled_skill_files_and_prefers_codex_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            codex = root / "codex"
            library.mkdir()
            codex.mkdir()
            (library / "SKILL.md").write_text("library content", encoding="utf-8")
            (codex / "SKILL.md").write_text("codex content\n中文", encoding="utf-8")

            payload = calculate_skill_token_usage(
                {
                    "enabled": {"enabled": True, "libraryPath": str(library), "codexPath": str(codex)},
                    "disabled": {"enabled": False, "libraryPath": str(library)},
                },
                allowed_roots=(root,),
            )

            self.assertEqual(payload["enabledSkillCount"], 1)
            self.assertEqual(payload["countedSkillCount"], 1)
            self.assertEqual(payload["totalTokens"], payload["byName"]["enabled"]["tokens"])
            self.assertEqual(payload["totalLazyTokens"], payload["byName"]["enabled"]["lazyTokens"])
            self.assertGreater(payload["totalLazyTokens"], 0)
            self.assertEqual(payload["scope"], "enabled-catalog")
            self.assertEqual(payload["byName"]["enabled"]["characters"], len("codex content\n中文"))
            self.assertFalse(payload["byName"]["disabled"]["counted"])
            self.assertEqual(payload["byName"]["disabled"]["reason"], "未启用")
            self.assertEqual(Path(payload["byName"]["enabled"]["path"]), (codex / "SKILL.md").resolve())

    def test_missing_enabled_file_is_reported_without_failing_calculation(self) -> None:
        payload = calculate_skill_token_usage({"missing": {"enabled": True}}, allowed_roots=())

        self.assertEqual(payload["enabledSkillCount"], 1)
        self.assertEqual(payload["countedSkillCount"], 1)
        self.assertGreater(payload["totalTokens"], 0)
        self.assertEqual(payload["lazyCountedSkillCount"], 0)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["byName"]["missing"]["error"], "未找到 SKILL.md")

    def test_fallback_estimator_is_positive_for_markdown(self) -> None:
        self.assertGreater(estimate_token_count("# Skill\n\n处理 PDF。"), 0)


if __name__ == "__main__":
    unittest.main()
