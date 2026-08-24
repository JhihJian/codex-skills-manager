import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from outcome_checkers import (
    BubblewrapCheckerRunner,
    CheckerCommandRejected,
    DocumentArtifactChecker,
    GradleSummaryChecker,
)


class GradleSummaryCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = GradleSummaryChecker()

    def test_exit_zero_without_assertions_is_not_pass(self) -> None:
        result = self.checker.parse("BUILD SUCCESSFUL", exit_code=0)
        self.assertEqual(result["outcome"], "parse-error")

    def test_reports_failures_and_skips(self) -> None:
        failed = self.checker.parse("10 tests completed, 2 failed, 1 skipped\nBUILD FAILED", exit_code=1)
        skipped = self.checker.parse("3 tests completed, 3 skipped\nBUILD SUCCESSFUL", exit_code=0)
        self.assertEqual(failed["outcome"], "inconclusive")
        self.assertEqual(failed["assertions"]["failed"], 2)
        self.assertEqual(skipped["outcome"], "parse-error")
        self.assertEqual(skipped["reason"], "all-tests-skipped")

    def test_environment_failure_is_not_assertion_failure(self) -> None:
        result = self.checker.parse("", "ERROR: JAVA_HOME is not set", exit_code=1)
        self.assertEqual(result["outcome"], "infrastructure-error")
        self.assertEqual(result["validity"], "environment-mismatch")

    @unittest.skipUnless(shutil.which("gradle"), "trusted Gradle is not installed")
    def test_runner_mode_requires_attested_test_task_summary(self) -> None:
        command = self.checker.build_command("/workspace")
        self.assertIn("--init-script", command)
        nonce = self.checker._attestation_nonce
        forged = self.checker.parse("1 test completed", exit_code=0)
        self.assertEqual(forged["outcome"], "parse-error")
        self.checker._attestation_nonce = nonce
        attested = self.checker.parse(f"CODEX_TEST_SUMMARY_{nonce}:1:0:0", exit_code=0)
        self.assertEqual(attested["outcome"], "inconclusive")


class DocumentArtifactCheckerTests(unittest.TestCase):
    def test_document_content_and_workspace_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "report.md").write_text("# Report\n\nAccepted result.\n", encoding="utf-8")
            checker = DocumentArtifactChecker()
            passed = checker.check(root, "report.md", min_bytes=10, required_text=["Accepted"], allowed_extensions=["md"])
            missing_text = checker.check(root, "report.md", required_text=["Not present"])
            outside = checker.check(root, "../secret.md")
        self.assertEqual(passed["outcome"], "assertion-pass")
        self.assertEqual(missing_text["outcome"], "assertion-fail")
        self.assertEqual(outside["outcome"], "blocked")

    def test_document_symbolic_link_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real.md").write_text("content", encoding="utf-8")
            (root / "link.md").symlink_to("real.md")
            result = DocumentArtifactChecker().check(root, "link.md")
        self.assertEqual(result["outcome"], "blocked")


class BubblewrapRunnerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("gradle"), "bubblewrap or trusted Gradle is not installed")
    def test_workspace_gradlew_cannot_forge_trusted_gradle_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "gradlew"
            script.write_text("#!/bin/sh\necho '1 test completed'\n", encoding="utf-8")
            script.chmod(0o755)
            (root / "build.gradle").write_text(
                "tasks.register('test') { doLast { println '1 test completed' } }\n",
                encoding="utf-8",
            )
            runner = BubblewrapCheckerRunner(
                [GradleSummaryChecker()], allowed_workspace_roots=[root], timeout_seconds=10
            )
            result = runner.run("gradle-summary", root)
        self.assertNotEqual(result["outcome"], "assertion-pass")
        if result["outcome"] == "blocked":
            self.assertEqual(result["validity"], "environment-mismatch")
            self.assertEqual(result["reason"], "sandbox-start-failed")

    def test_rejects_unregistered_and_arbitrary_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = BubblewrapCheckerRunner([GradleSummaryChecker()], bwrap_path="/missing/bwrap")
            with self.assertRaises(CheckerCommandRejected):
                runner.run("unknown", temporary)
            with self.assertRaises(CheckerCommandRejected):
                runner.run("gradle-summary", temporary, command=["sh", "-c", "cat log"])

    def test_unavailable_bwrap_does_not_execute_or_build_command(self) -> None:
        class SentinelChecker:
            checker_id = "sentinel"
            version = "1"

            def build_command(self, sandbox_workspace: str, **options: object) -> list[str]:
                raise AssertionError("must not build or execute a command without bwrap")

        with tempfile.TemporaryDirectory() as temporary:
            runner = BubblewrapCheckerRunner([SentinelChecker()], bwrap_path="/missing/bwrap")
            with patch("outcome_checkers.subprocess.Popen") as popen:
                result = runner.run("sentinel", temporary)
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "bwrap-unavailable")
        popen.assert_not_called()

    def test_workspace_symbolic_links_are_rejected_before_execution(self) -> None:
        class SentinelChecker:
            checker_id = "sentinel"
            version = "1"

            def build_command(self, sandbox_workspace: str, **options: object) -> list[str]:
                return ["/bin/true"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link").symlink_to("/etc/passwd")
            runner = BubblewrapCheckerRunner([SentinelChecker()], bwrap_path="/bin/true")
            with self.assertRaises(CheckerCommandRejected):
                runner.run("sentinel", root)

    def test_workspace_copy_limit_is_enforced_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.txt").write_text("too large", encoding="utf-8")
            runner = BubblewrapCheckerRunner(
                [GradleSummaryChecker()], bwrap_path="/bin/true", max_workspace_bytes=2
            )
            with self.assertRaises(CheckerCommandRejected):
                runner.run("gradle-summary", root)

    def test_empty_directories_count_toward_workspace_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").mkdir()
            (root / "two").mkdir()
            runner = BubblewrapCheckerRunner(
                [GradleSummaryChecker()], bwrap_path="/bin/true", max_workspace_files=1
            )
            with self.assertRaises(CheckerCommandRejected):
                runner.run("gradle-summary", root)


if __name__ == "__main__":
    unittest.main()