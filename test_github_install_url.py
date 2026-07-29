import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app

from app import (
    archive_skill_paths_from_names,
    github_blob_skill_directory,
    github_install_target,
    github_skill_md_repo_path,
    github_source_for_skill,
    parse_github_install_url,
)


class GithubInstallUrlTests(unittest.TestCase):
    def test_tree_url_contains_repo_ref_and_path(self) -> None:
        url = "https://github.com/iOfficeAI/OfficeCLI/tree/main/skills"
        self.assertEqual(
            parse_github_install_url(url, "ignored"),
            ("iOfficeAI/OfficeCLI", "main", "skills"),
        )

    def test_tree_url_does_not_need_separate_path_or_ref(self) -> None:
        url = "https://github.com/iOfficeAI/OfficeCLI/tree/main/skills"
        self.assertEqual(
            github_install_target(source=url, repo="", paths=[], ref=""),
            ("iOfficeAI/OfficeCLI", "main", ["skills"]),
        )

    def test_root_skill_blob_url_targets_repo_root(self) -> None:
        url = "https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md"
        self.assertEqual(
            parse_github_install_url(url, "ignored"),
            ("LiamGvchi/gc-minimal-zine-poster", "main", "SKILL.md"),
        )
        self.assertEqual(github_blob_skill_directory(url), "")
        self.assertEqual(
            github_install_target(source=url, repo="", paths=[], ref=""),
            ("LiamGvchi/gc-minimal-zine-poster", "main", ["."]),
        )

    def test_nested_skill_blob_url_targets_containing_directory(self) -> None:
        url = "https://github.com/example/skills/blob/main/skills/quality/SKILL.md"
        self.assertEqual(github_blob_skill_directory(url), "skills/quality")
        self.assertEqual(
            github_install_target(source=url, repo="", paths=[], ref=""),
            ("example/skills", "main", ["skills/quality"]),
        )

    def test_non_skill_blob_url_is_not_treated_as_a_skill(self) -> None:
        self.assertIsNone(github_blob_skill_directory("https://github.com/example/skills/blob/main/README.md"))

    def test_root_skill_blob_source_uses_root_skill_md_for_remote_checks(self) -> None:
        url = "https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md"
        source = github_source_for_skill(
            "gc-minimal-zine-poster",
            {"source": {"type": "github", "source": url, "ref": "main", "path": ["."]}},
        )
        self.assertIsNotNone(source)
        self.assertEqual(source["path"], "")
        self.assertEqual(github_skill_md_repo_path(source["path"]), "SKILL.md")

    def test_root_skill_blob_runs_installer_with_root_path_and_repo_name(self) -> None:
        url = "https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md"
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "skills"
            library.mkdir()
            installer = Path(directory) / "install-skill-from-github.py"
            installer.write_text("", encoding="utf-8")
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                installed = library / "gc-minimal-zine-poster"
                installed.mkdir()
                (installed / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "Installed", "")

            with (
                patch.object(app, "LIBRARY_DIR", library),
                patch.object(app, "INSTALLER_SCRIPT", installer),
                patch.object(app, "codex_health", return_value={"available": True}),
                patch.object(app.subprocess, "run", side_effect=fake_run),
            ):
                installed, details = app.run_installer({"source": url})

        self.assertEqual(installed, ["gc-minimal-zine-poster"])
        self.assertEqual(details["paths"], ["."])
        self.assertIn("--path", commands[0])
        self.assertEqual(commands[0][commands[0].index("--path") + 1], ".")
        self.assertEqual(commands[0][commands[0].index("--name") + 1], "gc-minimal-zine-poster")

    def test_archive_discovery_finds_direct_child_skills(self) -> None:
        paths, is_parent = archive_skill_paths_from_names(
            [
                "OfficeCLI-main/skills/morph-ppt/SKILL.md",
                "OfficeCLI-main/skills/officecli/SKILL.md",
                "OfficeCLI-main/skills/officecli/references/detail.md",
                "OfficeCLI-main/skills/nested/child/SKILL.md",
            ],
            "skills",
        )
        self.assertTrue(is_parent)
        self.assertEqual(paths, ["skills/morph-ppt", "skills/officecli"])


if __name__ == "__main__":
    unittest.main()
