import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from outcome_contracts import (
    ImmutableContractError,
    OutcomeContractError,
    OutcomeContractInterpreter,
    OutcomeContractStore,
    evaluate_applicability,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


class OutcomeContractStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "skills.sqlite3"
        self.store = OutcomeContractStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_supersedes_exact_version_atomically(self) -> None:
        first = self.store.create_draft("gradle", SHA_A, {"requirements": []}, "author", contract_owner="build")
        first = self.store.publish(first["id"], "publisher", approver="reviewer")
        second = self.store.create_draft("gradle", SHA_A, {"requirements": []}, "author", contract_owner="build")
        second = self.store.publish(second["id"], "publisher", approver="reviewer")

        self.assertEqual(self.store.get(first["id"])["status"], "superseded")
        self.assertEqual(self.store.select("gradle", SHA_A)["id"], second["id"])
        self.assertEqual(self.store.select("gradle", SHA_A, contract_version_id=first["id"])["id"], first["id"])
        self.assertIsNone(self.store.select("gradle", SHA_B))
        self.assertEqual(second["published_by"], "publisher")
        self.assertEqual(second["approver"], "reviewer")

    def test_published_contract_body_is_immutable_even_via_sql(self) -> None:
        draft = self.store.create_draft("demo", SHA_A, {"requirements": []}, "author")
        active = self.store.publish(draft["id"], "publisher", approver="reviewer")
        with self.assertRaises(ImmutableContractError):
            self.store.update_draft(active["id"], {"requirements": [{"id": "changed"}]}, "author")
        with sqlite3.connect(self.db_path) as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE outcome_contracts SET contract_json = '{}' WHERE id = ?", (active["id"],))

    def test_only_one_active_contract_exists_across_review_modes(self) -> None:
        outcome = self.store.create_draft("demo", SHA_A, {"requirements": []}, "author")
        replay = self.store.create_draft(
            "demo", SHA_A, {"requirements": []}, "author", review_mode="historical-replay"
        )
        self.store.publish(outcome["id"], "publisher", approver="reviewer")
        self.store.publish(replay["id"], "publisher", approver="reviewer")
        self.assertEqual(self.store.get(outcome["id"])["status"], "superseded")
        self.assertIsNone(self.store.select("demo", SHA_A))
        self.assertEqual(
            self.store.select("demo", SHA_A, review_mode="historical-replay")["id"], replay["id"]
        )

    def test_failed_publish_rolls_back_superseding_previous_active(self) -> None:
        first = self.store.create_draft("demo", SHA_A, {"requirements": []}, "author")
        self.store.publish(first["id"], "publisher", approver="reviewer")
        second = self.store.create_draft("demo", SHA_A, {"requirements": []}, "author")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"""CREATE TRIGGER reject_test_publish BEFORE UPDATE OF status ON outcome_contracts
                    WHEN NEW.id = '{second['id']}' AND NEW.status = 'active'
                    BEGIN SELECT RAISE(ABORT, 'test rejection'); END"""
            )
        with self.assertRaises(Exception):
            self.store.publish(second["id"], "publisher", approver="reviewer")
        self.assertEqual(self.store.get(first["id"])["status"], "active")
        self.assertEqual(self.store.get(second["id"])["status"], "draft")

    def test_invalid_executable_contract_cannot_be_published(self) -> None:
        with self.assertRaises(OutcomeContractError):
            self.store.create_draft(
                "demo", SHA_A, {"artifacts": [{"id": "doc", "minCount": "invalid"}]}, "author"
            )
        with self.assertRaises(OutcomeContractError):
            self.store.create_draft("demo", SHA_A, {"requirements": [{"id": "empty"}]}, "author")
        with self.assertRaises(OutcomeContractError):
            self.store.create_draft(
                "demo", SHA_A,
                {"artifacts": [{"id": "always", "selector": {}, "minCount": 0}]}, "author",
            )


class OutcomeContractInterpreterTests(unittest.TestCase):
    def evaluate(self, contract, **kwargs):
        prepared = copy.deepcopy(contract)

        def approve(value):
            if isinstance(value, dict):
                if "checker" in value:
                    value.setdefault("checkerVersion", ">=1")
                    value.setdefault("parserVersion", 1)
                    value.setdefault("trustLevel", "trusted")
                    value.setdefault("approvalVersion", "local-admin-v1")
                for nested in value.values():
                    approve(nested)
            elif isinstance(value, list):
                for nested in value:
                    approve(nested)

        approve(prepared)
        for item in kwargs.get("evidence") or []:
            item.setdefault("checker_version", "1")
            item.setdefault("parser_version", 1)
            item.setdefault("approval_version", "local-admin-v1")
            item.setdefault("freshness", "current")
        return OutcomeContractInterpreter().evaluate(prepared, **kwargs)

    def test_applicability_uses_accepted_task_facts_and_preserves_unknown(self) -> None:
        rule = {"anyOf": [{"taskTag": "gradle-test"}, {"taskTag": "gradle-build"}]}
        self.assertEqual(evaluate_applicability(rule, []), "unknown")
        self.assertEqual(
            evaluate_applicability(rule, [{"taskTag": "gradle-test", "status": "candidate"}]),
            "unknown",
        )
        self.assertEqual(evaluate_applicability(rule, [{"taskTag": "document", "status": "accepted"}]), "not-applicable")
        self.assertEqual(evaluate_applicability(rule, [{"taskTag": "gradle-test", "status": "accepted"}]), "applicable")

    def test_all_of_any_of_min_count_and_artifacts(self) -> None:
        contract = {
            "applicability": {"minCount": 2, "of": [{"taskTag": "docs"}, {"commandType": "write"}, {"toolFamily": "file"}]},
            "artifacts": [{"id": "doc", "selector": {"kind": "file", "glob": "docs/*.md"}, "minCount": 1}],
            "requirements": [{"id": "document-valid", "allOf": [{"checker": "document-artifact"}]}],
        }
        result = self.evaluate(
            contract,
            task_facts={"taskTag": "docs", "commandType": "write"},
            artifacts=[{"id": "a1", "kind": "file", "path": "docs/report.md"}],
            evidence=[{"id": "e1", "checker_id": "document-artifact", "lifecycle": "finished", "outcome": "assertion-pass", "validity": "valid", "trust_level": "trusted", "assertions": {"total": 1}}],
        )
        self.assertEqual(result["verdict"], "pass")

    def test_valid_assertion_failure_differs_from_infrastructure_error(self) -> None:
        contract = {
            "applicability": {"taskTag": "gradle-test"},
            "requirements": [{"id": "tests", "allOf": [{"checker": "gradle-summary", "checkerVersion": ">=1,<2", "parserVersion": 1, "trustLevel": "trusted"}]}],
        }
        base = {"task_facts": {"taskTag": "gradle-test"}}
        failure = self.evaluate(
            contract,
            **base,
            evidence=[{"checker_id": "gradle-summary", "checker_version": "1.0.0", "parser_version": 1, "trust_level": "trusted", "outcome": "assertion-fail", "validity": "valid", "lifecycle": "finished", "assertions": {"total": 3, "failed": 1}}],
        )
        infrastructure = self.evaluate(
            contract,
            **base,
            evidence=[{"checker_id": "gradle-summary", "checker_version": "1.0.0", "parser_version": 1, "trust_level": "trusted", "outcome": "infrastructure-error", "validity": "environment-mismatch", "lifecycle": "finished"}],
        )
        self.assertEqual(failure["verdict"], "fail")
        self.assertEqual(infrastructure["verdict"], "inconclusive")

    def test_any_of_pass_is_not_overridden_by_failed_alternative(self) -> None:
        contract = {
            "applicability": {"taskTag": "test"},
            "requirements": [{"id": "choice", "anyOf": [{"checker": "first"}, {"checker": "second"}]}],
        }
        evidence = [
            {"checker_id": "first", "outcome": "assertion-fail", "validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "assertions": {"total": 1}},
            {"checker_id": "second", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "assertions": {"total": 1}},
        ]
        result = self.evaluate(contract, task_facts={"taskTag": "test"}, evidence=evidence)
        self.assertEqual(result["verdict"], "pass")

    def test_checker_pass_requires_explicit_validity_lifecycle_and_assertion_count(self) -> None:
        contract = {
            "applicability": {"taskTag": "test"},
            "requirements": [{"id": "test", "allOf": [{"checker": "demo"}]}],
        }
        result = self.evaluate(
            contract,
            task_facts={"taskTag": "test"},
            evidence=[{"checker_id": "demo", "outcome": "assertion-pass"}],
        )
        self.assertEqual(result["verdict"], "inconclusive")

    def test_checker_approval_must_match_contract_exactly(self) -> None:
        contract = {
            "applicability": {"taskTag": "test"},
            "requirements": [{"id": "test", "checker": "demo", "checkerVersion": ">=1", "parserVersion": 1, "trustLevel": "trusted", "approvalVersion": "local-admin-v1"}],
        }
        result = OutcomeContractInterpreter().evaluate(
            contract,
            task_facts={"taskTag": "test"},
            evidence=[{"checker_id": "demo", "checker_version": "1", "parser_version": 1, "approval_version": "unapproved", "trust_level": "trusted", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "assertions": {"total": 1}}],
        )
        self.assertEqual(result["verdict"], "inconclusive")

    def test_requirement_min_count_can_produce_partial_verdict(self) -> None:
        contract = {
            "applicability": {"taskTag": "test"},
            "requirements": [
                {"id": "known", "allOf": [{"checker": "known"}]},
                {"id": "quorum", "minCount": 2, "of": [{"checker": "one"}, {"checker": "two"}, {"checker": "three"}]},
            ],
        }
        evidence = [
            {"checker_id": "known", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "assertions": {"total": 1}},
            {"checker_id": "one", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "trust_level": "trusted", "assertions": {"total": 1}},
        ]
        result = self.evaluate(contract, task_facts={"taskTag": "test"}, evidence=evidence)
        self.assertEqual(result["verdict"], "partial")

    def test_malformed_assertion_count_is_inconclusive(self) -> None:
        contract = {
            "applicability": {"taskTag": "test"},
            "requirements": [{"id": "test", "allOf": [{"checker": "demo"}]}],
        }
        result = self.evaluate(
            contract,
            task_facts={"taskTag": "test"},
            evidence=[{"checker_id": "demo", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "assertions": {"total": "invalid"}}],
        )
        self.assertEqual(result["verdict"], "inconclusive")
        fractional = self.evaluate(
            contract,
            task_facts={"taskTag": "test"},
            evidence=[{"checker_id": "demo", "outcome": "assertion-pass", "validity": "valid", "lifecycle": "finished", "assertions": {"total": 1.5}}],
        )
        self.assertEqual(fractional["verdict"], "inconclusive")

    def test_untrusted_non_checker_evidence_is_inconclusive(self) -> None:
        contract = {
            "applicability": {"taskTag": "report"},
            "requirements": [{"id": "report-created", "allOf": [{"evidence": "report"}]}],
        }
        result = OutcomeContractInterpreter().evaluate(
            contract,
            task_facts={"taskTag": "report"},
            evidence=[{"type": "report", "validity": "untrusted", "lifecycle": "failed"}],
        )
        self.assertEqual(result["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
