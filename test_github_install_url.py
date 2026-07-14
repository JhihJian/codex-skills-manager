import unittest

from app import archive_skill_paths_from_names, github_install_target, parse_github_install_url


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
