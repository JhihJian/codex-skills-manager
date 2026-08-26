import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from feedback_detector import detect_user_feedback


class FeedbackPerformanceTests(unittest.TestCase):
    def test_five_thousand_new_messages_are_detected_within_budget(self):
        messages = ["按钮还是没反应" if index % 2 else "没问题，现在可以了" for index in range(5000)]
        started = time.perf_counter()
        candidates = sum(len(detect_user_feedback(message, event_id=str(index))) for index, message in enumerate(messages))
        elapsed = time.perf_counter() - started
        self.assertEqual(candidates, 2500)
        self.assertLess(elapsed, 2.0)

    def test_scaled_query_benchmark_meets_storage_and_latency_budgets(self):
        root = Path(__file__).resolve().parent
        completed = subprocess.run(
            [
                sys.executable, str(root / "scripts" / "benchmark-feedback.py"),
                "--events", "10000", "--signals", "5000", "--targets", "10000",
                "--actions", "10000", "--repetitions", "2", "--clients", "2",
            ],
            cwd=root, capture_output=True, text=True, timeout=60, check=True,
        )
        report = json.loads(completed.stdout)
        self.assertLessEqual(report["bytesPerSignal"], 4096)
        for name, metrics in report["queries"].items():
            self.assertLess(metrics["p95Ms"], 200, name)
        required_indexes = {
            "idx_feedback_revision_time", "idx_feedback_revision_channel",
            "idx_feedback_revision_category", "idx_feedback_revision_severity",
            "idx_feedback_revision_source", "idx_feedback_revision_confidence",
            "idx_feedback_signal_process", "idx_feedback_target_kind",
        }
        self.assertTrue(required_indexes <= set(report["feedbackIndexes"]))


if __name__ == "__main__":
    unittest.main()