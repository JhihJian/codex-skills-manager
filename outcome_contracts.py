"""Versioned outcome contracts and their deterministic interpreter.

The store deliberately receives the skills SQLite path from its caller.  It
does not import application globals, which keeps the skills repository as the
single owner of contract definitions.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_STATES = frozenset({"draft", "active", "superseded", "retired"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MISSING = object()


class OutcomeContractError(ValueError):
    """Base error for invalid contract operations."""


class ContractNotFoundError(OutcomeContractError):
    pass


class ImmutableContractError(OutcomeContractError):
    pass


class ContractConflictError(OutcomeContractError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OutcomeContractError(f"{field} is required")
    return text


def _validate_sha256(value: str) -> str:
    sha = _require_text(value, "skill_sha256").lower()
    if not _SHA256_RE.fullmatch(sha):
        raise OutcomeContractError("skill_sha256 must be a 64-character hexadecimal SHA-256")
    return sha


def _validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise OutcomeContractError("contract must be an object")
    result = dict(contract)
    requirement_ids: list[str] = []
    artifact_ids: list[str] = []
    for field, target in (("requirements", requirement_ids), ("artifacts", artifact_ids)):
        entries = result.get(field, [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise OutcomeContractError(f"{field} must be a list")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise OutcomeContractError(f"every {field} entry must be an object")
            target.append(_require_text(entry.get("id"), f"{field}.id"))
        if len(target) != len(set(target)):
            raise OutcomeContractError(f"{field} IDs must be unique")
    _validate_logic(result.get("applicability"), "applicability", allow_empty=True)
    for requirement in result.get("requirements") or []:
        logical_keys = [key for key in ("allOf", "anyOf", "minCount") if key in requirement]
        if len(logical_keys) > 1:
            raise OutcomeContractError(f"requirements.{requirement['id']} must use one combination operator")
        if logical_keys:
            _validate_logic(
                {key: requirement[key] for key in ("allOf", "anyOf", "minCount", "of", "conditions") if key in requirement},
                f"requirements.{requirement['id']}",
                require_evidence_atom=True,
            )
        elif not ({"checker", "evidence"} & set(requirement)):
            raise OutcomeContractError(f"requirements.{requirement['id']} must define checker or evidence")
    for artifact in result.get("artifacts") or []:
        if not isinstance(artifact.get("selector", {}), Mapping):
            raise OutcomeContractError(f"artifacts.{artifact['id']}.selector must be an object")
        minimum = _contract_count(artifact.get("minCount", 1), f"artifacts.{artifact['id']}.minCount")
        maximum = artifact.get("maxCount")
        if maximum is not None and _contract_count(maximum, f"artifacts.{artifact['id']}.maxCount") < minimum:
            raise OutcomeContractError(f"artifacts.{artifact['id']}.maxCount must be at least minCount")
    return result


def _contract_count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OutcomeContractError(f"{field} must be a non-negative integer")
    return value


def _validate_logic(
    expression: Any,
    field: str,
    *,
    allow_empty: bool = False,
    require_evidence_atom: bool = False,
) -> None:
    if expression is None and allow_empty:
        return
    if not isinstance(expression, Mapping):
        raise OutcomeContractError(f"{field} must be an object")
    operators = [key for key in ("allOf", "anyOf", "minCount") if key in expression]
    if not operators:
        if not expression and not allow_empty:
            raise OutcomeContractError(f"{field} cannot be empty")
        if require_evidence_atom and not ({"checker", "evidence"} & set(expression)):
            raise OutcomeContractError(f"{field} must define checker or evidence")
        return
    if len(operators) != 1:
        raise OutcomeContractError(f"{field} must use one combination operator")
    operator = operators[0]
    if operator in {"allOf", "anyOf"}:
        choices = expression[operator]
        if not isinstance(choices, list) or not choices:
            raise OutcomeContractError(f"{field}.{operator} must be a non-empty list")
    else:
        raw = expression["minCount"]
        if isinstance(raw, Mapping):
            minimum = _contract_count(raw.get("count", raw.get("min")), f"{field}.minCount")
            choices = raw.get("of", raw.get("conditions"))
        else:
            minimum = _contract_count(raw, f"{field}.minCount")
            choices = expression.get("of", expression.get("conditions"))
        if minimum < 1:
            raise OutcomeContractError(f"{field}.minCount must be at least 1")
        if not isinstance(choices, list) or not choices or minimum > len(choices):
            raise OutcomeContractError(f"{field}.minCount choices must satisfy the requested count")
    for index, choice in enumerate(choices):
        _validate_logic(
            choice,
            f"{field}.{operator}[{index}]",
            require_evidence_atom=require_evidence_atom,
        )


def _version_tuple(value: Any) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(value or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _version_satisfies(value: Any, constraint: Any) -> bool:
    actual = _version_tuple(value)
    if actual is None:
        return False
    for condition in str(constraint).split(","):
        match = re.fullmatch(r"\s*(>=|<=|>|<|==|=)?\s*(v?\d+(?:\.\d+)*)\s*", condition)
        if match is None:
            return False
        expected = _version_tuple(match.group(2))
        if expected is None:
            return False
        length = max(len(actual), len(expected))
        left = actual + (0,) * (length - len(actual))
        right = expected + (0,) * (length - len(expected))
        operator = match.group(1) or "=="
        if not {
            "==": left == right,
            "=": left == right,
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
        }[operator]:
            return False
    return True


class OutcomeContractStore:
    """Persist outcome contracts in a caller-selected skills SQLite database."""

    def __init__(self, skills_db_path: str | Path):
        self.db_path = Path(skills_db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS outcome_contracts (
                  id TEXT PRIMARY KEY,
                  skill_id TEXT NOT NULL,
                  skill_sha256 TEXT NOT NULL,
                  review_mode TEXT NOT NULL,
                  version INTEGER NOT NULL CHECK(version > 0),
                  status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'superseded', 'retired')),
                  contract_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  published_at TEXT,
                  retired_at TEXT,
                  created_by TEXT NOT NULL,
                  updated_by TEXT NOT NULL,
                  published_by TEXT,
                  retired_by TEXT,
                  contract_owner TEXT NOT NULL,
                  approver TEXT,
                  governance_status TEXT NOT NULL DEFAULT 'draft',
                  governance_note TEXT,
                  UNIQUE(skill_id, skill_sha256, review_mode, version)
                );

                DROP INDEX IF EXISTS uq_outcome_contract_active;

                CREATE UNIQUE INDEX uq_outcome_contract_active
                ON outcome_contracts(skill_id, skill_sha256)
                WHERE status = 'active';

                CREATE INDEX IF NOT EXISTS ix_outcome_contract_lookup
                ON outcome_contracts(skill_id, skill_sha256, review_mode, status, version);

                CREATE TRIGGER IF NOT EXISTS outcome_contract_published_body_immutable
                BEFORE UPDATE ON outcome_contracts
                WHEN OLD.status != 'draft' AND (
                  NEW.skill_id != OLD.skill_id OR
                  NEW.skill_sha256 != OLD.skill_sha256 OR
                  NEW.review_mode != OLD.review_mode OR
                  NEW.version != OLD.version OR
                  NEW.contract_json != OLD.contract_json OR
                  NEW.created_at != OLD.created_at OR
                  NEW.created_by != OLD.created_by OR
                  NEW.contract_owner != OLD.contract_owner OR
                  COALESCE(NEW.approver, '') != COALESCE(OLD.approver, '') OR
                  NEW.governance_status != OLD.governance_status OR
                  COALESCE(NEW.governance_note, '') != COALESCE(OLD.governance_note, '') OR
                  COALESCE(NEW.published_at, '') != COALESCE(OLD.published_at, '') OR
                  COALESCE(NEW.published_by, '') != COALESCE(OLD.published_by, '')
                )
                BEGIN
                  SELECT RAISE(ABORT, 'published outcome contract is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS outcome_contract_status_transition
                BEFORE UPDATE OF status ON outcome_contracts
                WHEN NOT (
                  (OLD.status = 'draft' AND NEW.status IN ('draft', 'active', 'retired')) OR
                  (OLD.status = 'active' AND NEW.status IN ('active', 'superseded', 'retired')) OR
                  (OLD.status = 'superseded' AND NEW.status IN ('superseded', 'retired')) OR
                  (OLD.status = 'retired' AND NEW.status = 'retired')
                )
                BEGIN
                  SELECT RAISE(ABORT, 'invalid outcome contract status transition');
                END;

                CREATE TRIGGER IF NOT EXISTS outcome_contract_published_delete_guard
                BEFORE DELETE ON outcome_contracts
                WHEN OLD.status != 'draft'
                BEGIN
                  SELECT RAISE(ABORT, 'published outcome contract cannot be deleted');
                END;
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["contract"] = json.loads(item.pop("contract_json"))
        item["contract_version_id"] = item["id"]
        return item

    def create_draft(
        self,
        skill_id: str,
        skill_sha256: str,
        contract: Mapping[str, Any],
        actor: str,
        *,
        review_mode: str = "outcome",
        contract_owner: str | None = None,
        approver: str | None = None,
        governance_note: str | None = None,
    ) -> dict[str, Any]:
        skill = _require_text(skill_id, "skill_id")
        sha = _validate_sha256(skill_sha256)
        mode = _require_text(review_mode, "review_mode")
        acting_user = _require_text(actor, "actor")
        owner = _require_text(contract_owner or acting_user, "contract_owner")
        body = _validate_contract(contract)
        now = _utc_now()
        contract_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = int(
                conn.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 FROM outcome_contracts
                       WHERE skill_id = ? AND skill_sha256 = ? AND review_mode = ?""",
                    (skill, sha, mode),
                ).fetchone()[0]
            )
            conn.execute(
                """INSERT INTO outcome_contracts(
                     id, skill_id, skill_sha256, review_mode, version, status,
                     contract_json, created_at, updated_at, created_by, updated_by,
                     contract_owner, approver, governance_status, governance_note
                   ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
                (
                    contract_id,
                    skill,
                    sha,
                    mode,
                    version,
                    _canonical_json(body),
                    now,
                    now,
                    acting_user,
                    acting_user,
                    owner,
                    str(approver).strip() if approver else None,
                    governance_note,
                ),
            )
        return self.get(contract_id)

    def update_draft(
        self,
        contract_id: str,
        contract: Mapping[str, Any],
        actor: str,
        *,
        contract_owner: str | None = None,
        approver: str | None | object = _MISSING,
        governance_note: str | None | object = _MISSING,
    ) -> dict[str, Any]:
        acting_user = _require_text(actor, "actor")
        current = self.get(contract_id)
        if current["status"] != "draft":
            raise ImmutableContractError("published outcome contracts cannot be edited")
        body = _validate_contract(contract)
        owner = _require_text(contract_owner or current["contract_owner"], "contract_owner")
        next_approver = current["approver"] if approver is _MISSING else (str(approver).strip() if approver else None)
        next_note = current["governance_note"] if governance_note is _MISSING else governance_note
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE outcome_contracts
                   SET contract_json = ?, updated_at = ?, updated_by = ?, contract_owner = ?,
                       approver = ?, governance_note = ?
                   WHERE id = ? AND status = 'draft'""",
                (
                    _canonical_json(body),
                    _utc_now(),
                    acting_user,
                    owner,
                    next_approver,
                    next_note,
                    contract_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ImmutableContractError("draft changed before it could be updated")
        return self.get(contract_id)

    def publish(
        self,
        contract_id: str,
        actor: str,
        *,
        approver: str | None = None,
        governance_status: str = "approved",
        governance_note: str | None | object = _MISSING,
    ) -> dict[str, Any]:
        """Atomically supersede the previous active contract and publish a draft."""
        acting_user = _require_text(actor, "actor")
        governance = _require_text(governance_status, "governance_status")
        if governance != "approved":
            raise OutcomeContractError("a published contract must have approved governance status")
        now = _utc_now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM outcome_contracts WHERE id = ?", (contract_id,)).fetchone()
                if row is None:
                    raise ContractNotFoundError(f"outcome contract not found: {contract_id}")
                if row["status"] != "draft":
                    raise ImmutableContractError("only a draft can be published")
                final_approver = str(approver or row["approver"] or "").strip() or None
                if final_approver is None:
                    raise OutcomeContractError("approver is required to publish an outcome contract")
                final_note = row["governance_note"] if governance_note is _MISSING else governance_note
                conn.execute(
                    """UPDATE outcome_contracts
                       SET status = 'superseded', updated_at = ?, updated_by = ?
                       WHERE skill_id = ? AND skill_sha256 = ? AND status = 'active'""",
                    (now, acting_user, row["skill_id"], row["skill_sha256"]),
                )
                cursor = conn.execute(
                    """UPDATE outcome_contracts
                       SET status = 'active', updated_at = ?, updated_by = ?, published_at = ?,
                           published_by = ?, approver = ?, governance_status = ?, governance_note = ?
                       WHERE id = ? AND status = 'draft'""",
                    (now, acting_user, now, acting_user, final_approver, governance, final_note, contract_id),
                )
                if cursor.rowcount != 1:
                    raise ContractConflictError("draft changed before it could be published")
        except sqlite3.IntegrityError as exc:
            raise ContractConflictError(str(exc)) from exc
        return self.get(contract_id)

    def retire(self, contract_id: str, actor: str) -> dict[str, Any]:
        acting_user = _require_text(actor, "actor")
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE outcome_contracts
                   SET status = 'retired', updated_at = ?, updated_by = ?, retired_at = ?, retired_by = ?
                   WHERE id = ? AND status IN ('draft', 'active', 'superseded')""",
                (now, acting_user, now, acting_user, contract_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute("SELECT status FROM outcome_contracts WHERE id = ?", (contract_id,)).fetchone()
                if row is None:
                    raise ContractNotFoundError(f"outcome contract not found: {contract_id}")
                raise ImmutableContractError("retired outcome contract cannot transition")
        return self.get(contract_id)

    def delete_draft(self, contract_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM outcome_contracts WHERE id = ? AND status = 'draft'", (contract_id,))
            if cursor.rowcount != 1:
                row = conn.execute("SELECT status FROM outcome_contracts WHERE id = ?", (contract_id,)).fetchone()
                if row is None:
                    raise ContractNotFoundError(f"outcome contract not found: {contract_id}")
                raise ImmutableContractError("published outcome contracts cannot be deleted")

    def get(self, contract_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM outcome_contracts WHERE id = ?", (contract_id,)).fetchone()
        result = self._decode(row)
        if result is None:
            raise ContractNotFoundError(f"outcome contract not found: {contract_id}")
        return result

    def list_contracts(
        self,
        skill_id: str,
        skill_sha256: str | None = None,
        *,
        review_mode: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["skill_id = ?"]
        params: list[Any] = [_require_text(skill_id, "skill_id")]
        if skill_sha256 is not None:
            conditions.append("skill_sha256 = ?")
            params.append(_validate_sha256(skill_sha256))
        if review_mode is not None:
            conditions.append("review_mode = ?")
            params.append(_require_text(review_mode, "review_mode"))
        if status is not None:
            if status not in CONTRACT_STATES:
                raise OutcomeContractError(f"invalid status: {status}")
            conditions.append("status = ?")
            params.append(status)
        query = "SELECT * FROM outcome_contracts WHERE " + " AND ".join(conditions) + " ORDER BY version DESC"
        with self._connect() as conn:
            return [self._decode(row) for row in conn.execute(query, params).fetchall()]  # type: ignore[misc]

    def select(
        self,
        skill_id: str,
        skill_sha256: str,
        *,
        review_mode: str = "outcome",
        contract_version_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        """Select only an exact skill SHA and review mode; never fall back by name."""
        skill = _require_text(skill_id, "skill_id")
        sha = _validate_sha256(skill_sha256)
        mode = _require_text(review_mode, "review_mode")
        conditions = ["skill_id = ?", "skill_sha256 = ?", "review_mode = ?"]
        params: list[Any] = [skill, sha, mode]
        if contract_version_id is not None:
            conditions.append("id = ?")
            params.append(contract_version_id)
        elif version is not None:
            conditions.append("version = ?")
            params.append(int(version))
        else:
            conditions.append("status = 'active'")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outcome_contracts WHERE " + " AND ".join(conditions), params
            ).fetchone()
        return self._decode(row)

    select_contract = select
    publish_draft = publish


def _fact_parts(fact: Mapping[str, Any]) -> tuple[str | None, Any, str]:
    status = str(fact.get("status", "accepted"))
    key = (
        fact.get("key")
        or fact.get("fact_type")
        or fact.get("factType")
        or fact.get("predicate")
        or fact.get("predicate_name")
    )
    value = fact.get("value", fact.get("fact_value", fact.get("factValue", _MISSING)))
    ignored = {
        "id", "evidenceId", "producerVersion", "confidence", "status", "source_kind", "sourceKind",
        "fact_type", "factType", "fact_value", "factValue", "predicate", "predicate_name",
    }
    if key is None:
        candidates = [(str(k), v) for k, v in fact.items() if k not in ignored]
        if len(candidates) == 1:
            key, value = candidates[0]
    return (str(key) if key is not None else None, value, status)


def _normalize_facts(task_facts: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> dict[str, list[Any]]:
    normalized: dict[str, list[Any]] = {}
    if task_facts is None:
        return normalized
    if isinstance(task_facts, Mapping):
        if "facts" in task_facts and isinstance(task_facts["facts"], Sequence):
            task_facts = task_facts["facts"]
        else:
            for key, value in task_facts.items():
                if key.startswith("_") or key in {"complete", "revision"}:
                    continue
                values = value if isinstance(value, (list, tuple, set)) else [value]
                normalized.setdefault(str(key), []).extend(values)
            return normalized
    for fact in task_facts:
        if not isinstance(fact, Mapping):
            continue
        key, value, status = _fact_parts(fact)
        if status != "accepted" or key is None or value is _MISSING:
            continue
        normalized.setdefault(key, []).append(value)
    return normalized


def _atomic_applicability(predicate: Mapping[str, Any], facts: Mapping[str, list[Any]]) -> str:
    if "fact" in predicate:
        key = str(predicate["fact"])
        expected = predicate.get("equals", predicate.get("value", True))
    else:
        controls = {"allOf", "anyOf", "minCount", "of", "conditions", "count"}
        entries = [(str(key), value) for key, value in predicate.items() if key not in controls]
        if len(entries) != 1:
            return "unknown"
        key, expected = entries[0]
    if key not in facts:
        return "unknown"
    expected_values = expected if isinstance(expected, (list, tuple, set)) else [expected]
    actual_values = facts[key]
    return "true" if any(actual in expected_values for actual in actual_values) else "false"


def _evaluate_logic(expression: Any, atom_evaluator: Any) -> str:
    if expression is None or expression == {}:
        return "true"
    if not isinstance(expression, Mapping):
        return "unknown"
    if "allOf" in expression:
        results = [_evaluate_logic(item, atom_evaluator) for item in expression.get("allOf") or []]
        if not results:
            return "true"
        if "false" in results:
            return "false"
        return "unknown" if "unknown" in results else "true"
    if "anyOf" in expression:
        results = [_evaluate_logic(item, atom_evaluator) for item in expression.get("anyOf") or []]
        if not results:
            return "false"
        if "true" in results:
            return "true"
        return "unknown" if "unknown" in results else "false"
    if "minCount" in expression:
        raw = expression["minCount"]
        try:
            if isinstance(raw, Mapping):
                minimum = int(raw.get("count", raw.get("min", 1)))
                choices = raw.get("of", raw.get("conditions", []))
            else:
                minimum = int(raw)
                choices = expression.get("of", expression.get("conditions", []))
        except (TypeError, ValueError):
            return "unknown"
        if not isinstance(choices, list) or minimum < 1:
            return "unknown"
        results = [_evaluate_logic(item, atom_evaluator) for item in choices]
        passed = results.count("true")
        possible = passed + results.count("unknown")
        if passed >= minimum:
            return "true"
        if possible < minimum:
            return "false"
        return "unknown"
    return atom_evaluator(expression)


def evaluate_applicability(
    expression: Mapping[str, Any] | None,
    task_facts: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> str:
    facts = _normalize_facts(task_facts)
    result = _evaluate_logic(expression, lambda atom: _atomic_applicability(atom, facts))
    return {"true": "applicable", "false": "not-applicable", "unknown": "unknown"}[result]


def _artifact_matches(selector: Mapping[str, Any], artifact: Mapping[str, Any]) -> bool:
    for key, expected in selector.items():
        actual = artifact.get(key)
        if key == "glob":
            path = str(artifact.get("path") or artifact.get("name") or "")
            if not fnmatch.fnmatch(path, str(expected)):
                return False
        elif isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _clause_result(clause: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> tuple[str, list[str], str | None]:
    is_checker_clause = "checker" in clause
    if is_checker_clause:
        matches = [
            item for item in evidence
            if item.get("checker_id", item.get("checker")) == clause["checker"]
        ]
    else:
        matches = list(evidence)
        for key, expected in clause.items():
            if key in {"id", "assertions", "checkerVersion", "parserVersion", "trustLevel"}:
                continue
            if key == "evidence":
                matches = [
                    item for item in matches
                    if item.get("evidence", item.get("type", item.get("kind"))) == expected
                ]
            else:
                matches = [item for item in matches if item.get(key) == expected]
    if not matches:
        return "inconclusive", [], "evidence-missing"
    ids = [str(item.get("id")) for item in matches if item.get("id") is not None]
    if not is_checker_clause:
        for item in matches:
            lifecycle = item.get("lifecycle")
            validity = item.get("validity")
            status = item.get("status")
            if lifecycle not in {None, "finished"}:
                continue
            if validity not in {None, "valid"}:
                continue
            if status in {"candidate", "rejected", "invalid", "revoked"}:
                continue
            return "pass", ids, None
        return "inconclusive", ids, "evidence-invalid"
    saw_valid_pass = False
    reason = "evidence-invalid"
    for item in matches:
        outcome = item.get("outcome")
        validity = item.get("validity")
        lifecycle = item.get("lifecycle")
        checker_version = item.get("checker_version", item.get("checkerVersion"))
        parser_version = item.get("parser_version", item.get("parserVersion"))
        trust_level = item.get("trust_level", item.get("trustLevel"))
        if "checkerVersion" in clause and not _version_satisfies(checker_version, clause["checkerVersion"]):
            reason = "checker-version-mismatch"
            continue
        if "parserVersion" in clause and str(parser_version) != str(clause["parserVersion"]):
            reason = "parser-version-mismatch"
            continue
        if "trustLevel" in clause:
            trust_order = {"untrusted": 0, "sandboxed": 1, "trusted": 2}
            if trust_order.get(str(trust_level), -1) < trust_order.get(str(clause["trustLevel"]), 99):
                reason = "checker-trust-insufficient"
                continue
        if lifecycle != "finished" or validity != "valid":
            reason = str(outcome or validity)
            continue
        assertions = item.get("assertions") or {}
        total = assertions.get("total", item.get("assertion_count"))
        if outcome in {"assertion-pass", "assertion-fail"}:
            has_assertions = isinstance(total, int) and not isinstance(total, bool) and total > 0
            if not has_assertions:
                reason = "empty-assertions"
                continue
        if outcome == "assertion-fail":
            return "fail", ids, "assertion-fail"
        if outcome == "assertion-pass":
            expected_assertions = clause.get("assertions") or {}
            if any(assertions.get(key) != value for key, value in expected_assertions.items()):
                return "fail", ids, "assertion-mismatch"
            saw_valid_pass = True
        else:
            reason = str(outcome or "inconclusive")
    return ("pass", ids, None) if saw_valid_pass else ("inconclusive", ids, reason)


def _requirement_result(requirement: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    requirement_id = str(requirement.get("id") or "")
    expression = {key: requirement[key] for key in ("allOf", "anyOf", "minCount", "of", "conditions") if key in requirement}
    if not expression:
        clauses = requirement.get("evidence")
        expression = clauses if isinstance(clauses, Mapping) else requirement

    clause_details: list[dict[str, Any]] = []

    def evaluate_atom(atom: Mapping[str, Any]) -> str:
        status, evidence_ids, reason = _clause_result(atom, evidence)
        clause_details.append({"clause": dict(atom), "status": status, "evidence_ids": evidence_ids, "reason": reason})
        return {"pass": "true", "fail": "false", "inconclusive": "unknown"}[status]

    logical = _evaluate_logic(expression, evaluate_atom)
    status = {"true": "pass", "false": "fail", "unknown": "inconclusive"}[logical]
    return {"id": requirement_id, "status": status, "clauses": clause_details}


class OutcomeContractInterpreter:
    """Interpret applicability, artifacts, and deterministic evidence."""

    def evaluate(
        self,
        contract: Mapping[str, Any],
        *,
        task_facts: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        artifacts: Sequence[Mapping[str, Any]] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        applicability = evaluate_applicability(contract.get("applicability"), task_facts)
        if applicability != "applicable":
            return {
                "applicability": applicability,
                "verdict": "inconclusive",
                "artifact_results": [],
                "requirement_results": [],
                "reasons": ["applicability-unknown" if applicability == "unknown" else "not-applicable"],
            }

        artifact_results: list[dict[str, Any]] = []
        for requirement in contract.get("artifacts") or []:
            selector = requirement.get("selector") or {}
            matches = [artifact for artifact in artifacts if _artifact_matches(selector, artifact)]
            try:
                minimum = int(requirement.get("minCount", 1))
                maximum = int(requirement["maxCount"]) if requirement.get("maxCount") is not None else None
            except (TypeError, ValueError):
                artifact_results.append(
                    {
                        "id": str(requirement.get("id") or ""),
                        "status": "inconclusive",
                        "count": len(matches),
                        "artifact_ids": [],
                        "reason": "artifact-contract-invalid",
                    }
                )
                continue
            complete = bool(requirement.get("observationComplete", True))
            valid_count = len(matches) >= minimum and (maximum is None or len(matches) <= maximum)
            status = "pass" if valid_count else ("fail" if complete else "inconclusive")
            artifact_results.append(
                {
                    "id": str(requirement.get("id") or ""),
                    "status": status,
                    "count": len(matches),
                    "artifact_ids": [str(item.get("id")) for item in matches if item.get("id") is not None],
                    "reason": None if status == "pass" else ("artifact-count" if complete else "artifact-observation-incomplete"),
                }
            )

        requirement_results = [_requirement_result(item, evidence) for item in contract.get("requirements") or []]
        statuses = [item["status"] for item in artifact_results + requirement_results]
        reasons = [str(item["reason"]) for item in artifact_results if item.get("reason")]
        reasons.extend(
            str(clause["reason"])
            for item in requirement_results
            for clause in item["clauses"]
            if clause.get("reason")
        )
        # Combination semantics decide whether a valid failed assertion is
        # decisive. Infrastructure and missing evidence remain inconclusive.
        if "fail" in statuses:
            verdict = "fail"
        elif statuses and all(status == "pass" for status in statuses):
            verdict = "pass"
        elif "pass" in statuses:
            verdict = "partial"
        else:
            verdict = "inconclusive"
        return {
            "applicability": applicability,
            "verdict": verdict,
            "artifact_results": artifact_results,
            "requirement_results": requirement_results,
            "reasons": sorted(set(reasons)),
        }


def interpret_contract(
    contract: Mapping[str, Any],
    *,
    task_facts: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
    evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return OutcomeContractInterpreter().evaluate(
        contract, task_facts=task_facts, artifacts=artifacts, evidence=evidence
    )


evaluate_contract = interpret_contract
