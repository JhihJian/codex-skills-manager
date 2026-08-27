"""Skill-version quality projections and auditable trial-use judgments."""
from __future__ import annotations

import hashlib
import json
import math
import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from auth import redact_sensitive
from effect_store import EffectStore, EffectStoreError, ImmutableSnapshotError, RevisionConflict


QUALITY_VERSION = "skill-quality-v1"
_REASON_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}\Z")
_JUDGMENT_VERDICTS = frozenset({"helpful", "not-helpful", "cannot-judge", "not-applicable", "withdrawn"})
_RELATIONS = frozenset({"direct-skill-use", "task-result-only", "cannot-attribute"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def _decode(row: Any) -> dict[str, Any]:
    return EffectStore._row(row)


class SkillQualityService:
    """Owns quality-specific projections without changing outcome semantics."""

    def __init__(self, store: EffectStore, contracts: Any, feedback: Any) -> None:
        self.store = store
        self.contracts = contracts
        self.feedback = feedback

    # -------------------------------------------------------------- read model
    def directory(
        self, *, skill_id: str | None = None, observation: str | None = None,
        attribution: str | None = None, task_type: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
        limit: int = 100, cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        conditions = [
            "i.validity='valid'", "i.invocation_kind='business-use'",
            "i.skill_id NOT IN ('', '*', 'unknown')",
        ]
        params: list[Any] = []
        if skill_id:
            conditions.append("i.skill_id=?")
            params.append(skill_id)
        if attribution in {"direct", "shared"}:
            conditions.append("l.attribution_kind=?")
            params.append(attribution)
        if task_type:
            conditions.append("directory_case.task_type=?")
            params.append(task_type)
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("""EXISTS (SELECT 1 FROM task_episodes source_episode
                JOIN sessions source_session ON source_session.id=source_episode.session_id
                WHERE source_episode.id=i.task_episode_id AND source_session.source=?)""")
            params.append(source)
        if from_at:
            conditions.append("i.created_at>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("i.created_at<=?")
            params.append(self._timestamp(to_at, "to"))
        allowed = {
            "observed": {"only-loaded", "evidence-insufficient", "directional", "judgment-supported", "not-publishable", "threshold-not-met"},
            "evaluable": {"directional", "judgment-supported", "threshold-not-met"},
            "insufficient": {"only-loaded", "evidence-insufficient", "not-publishable"},
        }.get(observation)
        base_sql = """SELECT i.skill_id, i.skill_sha256,
                       COUNT(DISTINCT i.id) AS invocation_count,
                       COUNT(DISTINCT CASE WHEN i.load_status='loaded' THEN i.id END) AS loaded_count,
                       COUNT(DISTINCT directory_case.id) AS case_count,
                       COUNT(DISTINCT CASE WHEN l.attribution_kind='direct' THEN directory_case.id END) AS direct_case_count,
                       COUNT(DISTINCT CASE WHEN i.skill_sha256 IS NOT NULL THEN directory_case.id END) AS known_version_cases,
                       COUNT(DISTINCT CASE WHEN assessment.contract_version_id IS NOT NULL THEN directory_case.id END) AS contract_cases,
                       COUNT(DISTINCT CASE WHEN assessment.assessability='assessable' THEN directory_case.id END) AS assessable_cases,
                       MAX(i.created_at) AS last_observed_at
                FROM skill_invocations i
                LEFT JOIN attribution_links l ON l.skill_invocation_id=i.id AND l.status='active'
                LEFT JOIN task_cases directory_case ON directory_case.id=l.task_case_id AND directory_case.invalidated_at IS NULL
                LEFT JOIN outcome_assessments assessment ON assessment.skill_invocation_id=i.id
                  AND assessment.task_case_id=l.task_case_id AND assessment.is_current=1"""
        items: list[dict[str, Any]] = []
        raw_cursor = cursor
        exhausted = False
        while len(items) < limit and not exhausted:
            page_conditions = list(conditions)
            page_params = list(params)
            if raw_cursor:
                cursor_skill, cursor_sha = self._parse_subject_cursor(raw_cursor)
                page_conditions.append("(i.skill_id>? OR (i.skill_id=? AND COALESCE(i.skill_sha256,'')>?))")
                page_params.extend((cursor_skill, cursor_skill, cursor_sha))
            rows = self.store.execute(
                f"""{base_sql} WHERE {' AND '.join(page_conditions)}
                    GROUP BY i.skill_id, i.skill_sha256
                    ORDER BY i.skill_id, i.skill_sha256 LIMIT ?""",
                (*page_params, limit + 1),
            ).fetchall()
            if not rows:
                exhausted = True
                break
            for row in rows[:limit]:
                raw_cursor = self._subject_cursor(_decode(row))
                item = self._directory_item(
                    _decode(row), task_type=task_type, attribution=attribution,
                    source=source, from_at=from_at, to_at=to_at,
                )
                if allowed is None or item["quality_status"] in allowed:
                    items.append(item)
                    if len(items) >= limit:
                        break
            if len(rows) <= limit:
                exhausted = True
        return {
            "items": items,
            "next_cursor": None if exhausted else raw_cursor,
            "coverage": self._coverage(),
        }

    def detail(
        self, skill_id: str, skill_sha256: str | None, *, task_type: str | None = None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
    ) -> dict[str, Any]:
        subject = self._subject(skill_id, skill_sha256)
        if subject is None:
            raise KeyError(f"{skill_id}@{skill_sha256 or 'unknown'}")
        coverage = self._coverage()
        funnel = self._funnel(
            skill_id, skill_sha256, task_type=task_type, attribution=attribution,
            source=source, from_at=from_at, to_at=to_at,
        )
        result = self._formal_results(
            skill_id, skill_sha256, task_type=task_type, attribution=attribution,
            source=source, from_at=from_at, to_at=to_at, coverage=coverage,
        )
        feedback = self._feedback_summary(
            skill_id, skill_sha256, task_type=task_type, attribution=attribution,
            source=source, from_at=from_at, to_at=to_at,
        )
        judgments = self._judgment_summary(
            skill_id, skill_sha256, task_type=task_type, attribution=attribution,
            source=source, from_at=from_at, to_at=to_at,
        )
        status, reasons = self._quality_status(coverage, funnel, result)
        return {
            "subject": subject,
            "coverage": coverage,
            "quality_status": status,
            "blocking_reasons": reasons,
            "funnel": funnel,
            "formal_results": result,
            "feedback": feedback,
            "experience": judgments,
            "contracts": self._contract_summary(skill_id, skill_sha256),
            "latest_cases": self.cases(
                skill_id, skill_sha256, task_type=task_type, attribution=attribution,
                source=source, from_at=from_at, to_at=to_at, limit=10,
            )["items"],
        }

    def cases(
        self, skill_id: str, skill_sha256: str | None, *, task_type: str | None = None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
        limit: int = 50, cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 100)
        conditions = ["i.validity='valid'", "i.invocation_kind='business-use'", "l.status='active'", "c.invalidated_at IS NULL", "i.skill_id=?"]
        params: list[Any] = [skill_id]
        if skill_sha256 is None:
            conditions.append("i.skill_sha256 IS NULL")
        else:
            conditions.append("i.skill_sha256=?")
            params.append(skill_sha256)
        if task_type:
            conditions.append("c.task_type=?")
            params.append(task_type)
        if attribution in {"direct", "shared"}:
            conditions.append("l.attribution_kind=?")
            params.append(attribution)
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("session.source=?")
            params.append(source)
        if from_at:
            conditions.append("i.created_at>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("i.created_at<=?")
            params.append(self._timestamp(to_at, "to"))
        if cursor:
            created, case_id, invocation_id = cursor.split("|", 2)
            conditions.append("(i.created_at, c.id, i.id) < (?, ?, ?)")
            params.extend((created, case_id, invocation_id))
        rows = self.store.execute(
            f"""SELECT c.id AS task_case_id, c.task_type, c.current_revision,
                       i.id AS skill_invocation_id, i.load_status, i.created_at,
                       l.attribution_kind,
                       a.id AS assessment_id, a.assessability, a.automated_verdict,
                       a.contract_version_id, a.freshness
                FROM skill_invocations i JOIN attribution_links l ON l.skill_invocation_id=i.id
                JOIN task_cases c ON c.id=l.task_case_id
                JOIN task_episodes episode ON episode.id=i.task_episode_id
                JOIN sessions session ON session.id=episode.session_id
                LEFT JOIN outcome_assessments a ON a.skill_invocation_id=i.id
                  AND a.task_case_id=c.id AND a.is_current=1
                WHERE {' AND '.join(conditions)}
                ORDER BY i.created_at DESC, c.id DESC, i.id DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        items = [_decode(row) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": f"{items[-1]['created_at']}|{items[-1]['task_case_id']}|{items[-1]['skill_invocation_id']}" if has_more and items else None,
        }

    def feedback_items(
        self, skill_id: str, skill_sha256: str | None, *, task_type: str | None = None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
        limit: int = 50, cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 100)
        conditions = ["i.skill_id=?", "i.validity='valid'", "l.status='active'"]
        params: list[Any] = [skill_id]
        if skill_sha256 is None:
            conditions.append("i.skill_sha256 IS NULL")
        else:
            conditions.append("i.skill_sha256=?")
            params.append(skill_sha256)
        if task_type:
            conditions.append("subject_case.task_type=?")
            params.append(task_type)
        if attribution in {"direct", "shared"}:
            conditions.append("l.attribution_kind=?")
            params.append(attribution)
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("invocation_session.source=?")
            params.append(source)
        if from_at:
            conditions.append("COALESCE(revision.observed_at,revision.created_at)>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("COALESCE(revision.observed_at,revision.created_at)<=?")
            params.append(self._timestamp(to_at, "to"))
        if cursor:
            conditions.append("s.id > ?")
            params.append(cursor)
        rows = self.store.execute(
            f"""SELECT DISTINCT s.id, s.current_confirmed_target_id, s.current_resolution_state, s.current_process_state,
                       revision.channel, revision.category, revision.severity,
                       revision.confidence, revision.redacted_excerpt, revision.observed_at,
                       target.id AS target_id, target.skill_invocation_id, target.context_task_case_id, target.target_task_case_id,
                       l.attribution_kind,
                       CASE WHEN target.skill_invocation_id=i.id THEN 'direct-target'
                            WHEN l.attribution_kind='direct' THEN 'case-direct-context'
                            WHEN l.attribution_kind='shared' THEN 'case-shared-context'
                            ELSE 'unattributed' END AS relation_kind
                FROM skill_invocations i JOIN attribution_links l ON l.skill_invocation_id=i.id
                JOIN task_cases subject_case ON subject_case.id=l.task_case_id
                JOIN task_episodes invocation_episode ON invocation_episode.id=i.task_episode_id
                JOIN sessions invocation_session ON invocation_session.id=invocation_episode.session_id
                JOIN feedback_targets target ON target.context_task_case_id=l.task_case_id
                   OR target.target_task_case_id=l.task_case_id OR target.skill_invocation_id=i.id
                JOIN feedback_signals s ON s.id=target.feedback_signal_id
                JOIN feedback_signal_revisions revision ON revision.id=s.current_machine_revision_id
                WHERE {' AND '.join(conditions)}
                ORDER BY s.id LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        items = [_decode(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if has_more and items else None}

    def compare(self, subjects: Sequence[Mapping[str, Any]], *, task_type: str | None = None,
        attribution: str = "direct", source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
    ) -> dict[str, Any]:
        if len(subjects) != 2:
            raise ValueError("comparison requires exactly two skill versions")
        details = [self.detail(
            str(item["skillId"]), item.get("sha"), task_type=task_type,
            attribution=attribution, source=source, from_at=from_at, to_at=to_at,
        ) for item in subjects]
        reasons: list[str] = []
        if attribution != "direct":
            reasons.append("comparison-requires-direct-attribution")
        coverage_ids = {item["coverage"].get("scan_run_id") for item in details}
        if len(coverage_ids) != 1 or any(item["coverage"].get("coverage_status") != "complete" for item in details):
            reasons.append("comparison-requires-complete-shared-scope")
        snapshots = {item["formal_results"].get("snapshot_id") for item in details}
        if None in snapshots or len(snapshots) != 1:
            reasons.append("comparison-requires-shared-formal-snapshot")
        contracts = {
            tuple(item["formal_results"].get("contract_version_ids") or ()) for item in details
        }
        if len(contracts) != 1 or any(len(value) != 1 for value in contracts):
            reasons.append("comparison-requires-single-shared-contract")
        if any(not item["subject"].get("skill_sha256") for item in details):
            reasons.append("comparison-requires-known-version")
        return {"comparable": not reasons, "reasons": reasons, "subjects": details}

    # ------------------------------------------------------ trial assignments
    def assign(self, invocation_id: str, *, actor_id: str, assigned_by_actor_id: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor_id, "trial_user")
        context = self._invocation_context(invocation_id)
        if context is None:
            raise KeyError(invocation_id)
        if expires_at is not None:
            expires_at = self._timestamp(expires_at, "expires_at")
        now = _now()
        assignment_id = _digest("trial-assignment", invocation_id, actor_id)
        contract_id = self._contract_for_invocation(context["task_case_id"], invocation_id) or "no-contract"
        evidence = self._evidence_snapshot(context, contract_id)
        evidence_id = _digest("assignment-evidence", assignment_id, evidence["hash"], now)
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO use_evidence_snapshots(
                       id, skill_invocation_id, task_case_id, case_revision, skill_id,
                       skill_sha256, contract_version_id, task_type, attribution_kind,
                       scan_run_id, scope_fingerprint, evidence_json, evidence_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, invocation_id, context["task_case_id"], context["case_revision"],
                 context["skill_id"], context["skill_sha256"], contract_id, context["task_type"],
                 context["attribution_kind"], evidence["scan_run_id"], evidence["scope_fingerprint"],
                 _json(evidence["view"]), evidence["hash"], now),
            )
            self.store.execute(
                """INSERT INTO skill_use_judgment_assignments(
                       id, skill_invocation_id, actor_id, assigned_by_actor_id, status,
                       evidence_snapshot_id, expires_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                   ON CONFLICT(skill_invocation_id, actor_id) DO UPDATE SET
                     assigned_by_actor_id=excluded.assigned_by_actor_id, status='active',
                     evidence_snapshot_id=excluded.evidence_snapshot_id,
                     expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                (assignment_id, invocation_id, actor_id, assigned_by_actor_id, evidence_id, expires_at, now, now),
            )
        return self._assignment(assignment_id)

    def assignments(self, actor_id: str) -> dict[str, Any]:
        self._require_role(actor_id, "trial_user")
        now = _now()
        rows = self.store.execute(
            """SELECT assignment.*, invocation.skill_id, invocation.skill_sha256,
                      invocation.load_status, invocation.created_at AS invocation_created_at
               FROM skill_use_judgment_assignments assignment
               JOIN skill_invocations invocation ON invocation.id=assignment.skill_invocation_id
               WHERE assignment.actor_id=? AND assignment.status IN ('active', 'expired')
               ORDER BY assignment.created_at DESC, assignment.id""",
            (actor_id,),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _decode(row)
            snapshot = self.store.execute(
                "SELECT * FROM use_evidence_snapshots WHERE id=?", (item.get("evidence_snapshot_id"),)
            ).fetchone()
            if snapshot is None:
                continue
            context = self._invocation_context(item["skill_invocation_id"])
            item["evidence_preview"] = _decode(snapshot).get("evidence", {})
            item["evidence_expired"] = bool(item.get("expires_at") and item["expires_at"] <= now)
            item["evidence_stale"] = bool(item["evidence_expired"] or
                context is None or context["task_case_id"] != snapshot["task_case_id"]
                or int(context["case_revision"]) != int(snapshot["case_revision"])
            )
            items.append(item)
        return {"items": items}

    def submit_judgment(self, invocation_id: str, *, actor_id: str, expected_revision: int,
        verdict: str, attribution_relation: str, reason_code: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if verdict not in _JUDGMENT_VERDICTS:
            raise ValueError("unsupported trial judgment verdict")
        if attribution_relation not in _RELATIONS:
            raise ValueError("unsupported trial attribution relation")
        if verdict in {"not-helpful", "not-applicable"}:
            if not reason_code or _REASON_CODE.fullmatch(str(reason_code)) is None:
                raise ValueError("reason_code must be a lowercase machine-readable code")
        elif reason_code:
            raise ValueError("reason_code is only allowed for not-helpful or not-applicable")
        self._require_role(actor_id, "trial_user")
        assignment = self._active_assignment(invocation_id, actor_id)
        if assignment is None:
            raise EffectStoreError("an active trial assignment is required")
        context = self._invocation_context(invocation_id)
        if context is None:
            raise KeyError(invocation_id)
        assigned_snapshot = self.store.execute(
            "SELECT * FROM use_evidence_snapshots WHERE id=?", (assignment["evidence_snapshot_id"],)
        ).fetchone()
        if assigned_snapshot is None:
            raise EffectStoreError("trial assignment evidence is unavailable")
        if (
            context["task_case_id"] != assigned_snapshot["task_case_id"]
            or int(context["case_revision"]) != int(assigned_snapshot["case_revision"])
            or context["skill_sha256"] != assigned_snapshot["skill_sha256"]
        ):
            raise EffectStoreError("trial assignment evidence is stale; request a new assignment")
        current = self.store.execute(
            """SELECT * FROM skill_use_judgments WHERE skill_invocation_id=? AND actor_id=?
               AND is_current=1 ORDER BY revision DESC LIMIT 1""", (invocation_id, actor_id)
        ).fetchone()
        current_revision = int(current["revision"]) if current else 0
        if current_revision != int(expected_revision):
            raise RevisionConflict(f"trial judgment revision conflict: expected {expected_revision}, current {current_revision}")
        contract_id = assigned_snapshot["contract_version_id"]
        evidence_id = assigned_snapshot["id"]
        eligibility = self._judgment_eligibility(context, verdict, attribution_relation)
        now = _now()
        judgment_id = _digest("skill-use-judgment", invocation_id, actor_id, current_revision + 1)
        safe_note = redact_sensitive(note) if note else None
        with self.store.transaction():
            if current is not None:
                self.store.execute("UPDATE skill_use_judgments SET is_current=0 WHERE id=?", (current["id"],))
            self.store.execute(
                """INSERT INTO skill_use_judgments(
                       id, skill_invocation_id, task_case_id, case_revision, contract_version_id,
                       evidence_snapshot_id, actor_id, revision, verdict, reason_code, note,
                       attribution_relation, aggregation_eligibility, supersedes_id, is_current, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (judgment_id, invocation_id, context["task_case_id"], context["case_revision"], contract_id,
                 evidence_id, actor_id, current_revision + 1, verdict, reason_code, safe_note,
                 attribution_relation, eligibility, current["id"] if current else None, now),
            )
            if verdict == "not-helpful" and attribution_relation == "direct-skill-use":
                referral_id = _digest("judgment-referral", judgment_id)
                self.store.execute(
                    """INSERT INTO judgment_feedback_referrals(
                           id, skill_use_judgment_id, status, created_at, updated_at)
                       VALUES (?, ?, 'pending-review', ?, ?)""", (referral_id, judgment_id, now, now),
                )
        return self.judgment(judgment_id)

    def withdraw_judgment(self, invocation_id: str, *, actor_id: str, expected_revision: int) -> dict[str, Any]:
        current = self.store.execute(
            """SELECT * FROM skill_use_judgments WHERE skill_invocation_id=? AND actor_id=?
               AND is_current=1 ORDER BY revision DESC LIMIT 1""", (invocation_id, actor_id)
        ).fetchone()
        if current is None:
            raise KeyError(invocation_id)
        return self.submit_judgment(
            invocation_id, actor_id=actor_id, expected_revision=expected_revision,
            verdict="withdrawn", attribution_relation=current["attribution_relation"],
        )

    def judgment(self, judgment_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM skill_use_judgments WHERE id=?", (judgment_id,)).fetchone()
        if row is None:
            raise KeyError(judgment_id)
        result = _decode(row)
        snapshot = self.store.execute("SELECT * FROM use_evidence_snapshots WHERE id=?", (row["evidence_snapshot_id"],)).fetchone()
        referral = self.store.execute("SELECT * FROM judgment_feedback_referrals WHERE skill_use_judgment_id=?", (judgment_id,)).fetchone()
        result["evidence_snapshot"] = _decode(snapshot) if snapshot else None
        result["referral"] = _decode(referral) if referral else None
        return result

    def judgments(self, invocation_id: str, *, actor_id: str) -> dict[str, Any]:
        self._require_role(actor_id, "trial_user")
        rows = self.store.execute(
            """SELECT id FROM skill_use_judgments WHERE skill_invocation_id=? AND actor_id=?
               ORDER BY revision DESC""", (invocation_id, actor_id),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self.judgment(row["id"])
            referral = item.pop("referral", None)
            if referral is not None:
                item["referral"] = {"status": referral["status"]}
            snapshot = item.get("evidence_snapshot") or {}
            item["evidence_snapshot"] = {
                "id": snapshot.get("id"), "case_revision": snapshot.get("case_revision"),
                "skill_id": snapshot.get("skill_id"), "skill_sha256": snapshot.get("skill_sha256"),
                "contract_version_id": snapshot.get("contract_version_id"),
                "evidence": snapshot.get("evidence"),
            }
            items.append(item)
        return {"items": items}

    def decide_referral(self, referral_id: str, *, actor_id: str, action: str,
        reason_code: str, feedback_signal_id: str | None = None, note: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor_id, "reviewer")
        if action not in {"link", "convert", "close"}:
            raise ValueError("unsupported referral action")
        if _REASON_CODE.fullmatch(str(reason_code or "")) is None:
            raise ValueError("reason_code must be a lowercase machine-readable code")
        referral = self.store.execute("SELECT * FROM judgment_feedback_referrals WHERE id=?", (referral_id,)).fetchone()
        if referral is None:
            raise KeyError(referral_id)
        if referral["status"] != "pending-review":
            raise RevisionConflict("judgment referral is no longer pending")
        judgment = self.store.execute("SELECT * FROM skill_use_judgments WHERE id=?", (referral["skill_use_judgment_id"],)).fetchone()
        if judgment is None:
            raise EffectStoreError("judgment referral has no judgment")
        if not judgment["is_current"]:
            raise RevisionConflict("judgment referral belongs to a superseded judgment")
        target_signal = feedback_signal_id
        if action == "link":
            if not target_signal or self.store.execute("SELECT 1 FROM feedback_signals WHERE id=?", (target_signal,)).fetchone() is None:
                raise ValueError("link action requires an existing feedback signal")
        elif action == "convert":
            target_signal = self._convert_referral(referral, judgment)
        now = _now()
        status = {"link": "linked", "convert": "converted", "close": "closed"}[action]
        with self.store.transaction():
            self.store.execute(
                """UPDATE judgment_feedback_referrals SET status=?, reviewer_actor_id=?,
                       feedback_signal_id=?, reason_code=?, note=?, updated_at=? WHERE id=?""",
                (status, actor_id, target_signal, reason_code, redact_sensitive(note) if note else None, now, referral_id),
            )
        return _decode(self.store.execute("SELECT * FROM judgment_feedback_referrals WHERE id=?", (referral_id,)).fetchone())

    # ---------------------------------------------------------- quality seal
    def seal_snapshot(self, *, expected_scope_fingerprint: str, snapshot_id: str | None = None) -> dict[str, Any]:
        latest = self.store.execute(
            """SELECT * FROM scan_runs WHERE status!='running' ORDER BY finished_at DESC, id DESC LIMIT 1"""
        ).fetchone()
        if latest is None or latest["coverage_status"] != "complete":
            raise EffectStoreError("skill quality snapshots require a complete latest scan")
        metadata = json.loads(latest["metadata_json"])
        if metadata.get("scopeKind") != "configured-catalog" or metadata.get("scopeFingerprint") != expected_scope_fingerprint:
            raise EffectStoreError("skill quality snapshots require the current configured scope")
        state = self.store.execute("SELECT * FROM feedback_derivation_state").fetchone()
        cursor = int(state["change_cursor"]) if state else 0
        max_change = int(self.store.execute("SELECT COALESCE(MAX(id),0) FROM effect_derivation_changes").fetchone()[0])
        if cursor < max_change:
            raise EffectStoreError("skill quality snapshots require settled derivations")
        now = _now()
        snapshot_id = snapshot_id or _digest("skill-quality-snapshot", latest["id"], now)
        rows = self.store.execute(
            """SELECT judgment.*, evidence.skill_id, evidence.skill_sha256, evidence.task_type,
                      evidence.attribution_kind, evidence.evidence_hash,
                      evidence.scan_run_id AS evidence_scan_run_id,
                      evidence.scope_fingerprint AS evidence_scope_fingerprint,
                      evidence.case_revision AS evidence_case_revision,
                      current_case.current_revision AS current_case_revision,
                      invocation.created_at AS invocation_created_at, session.source AS invocation_source
               FROM skill_use_judgments judgment
               JOIN use_evidence_snapshots evidence ON evidence.id=judgment.evidence_snapshot_id
               JOIN task_cases current_case ON current_case.id=evidence.task_case_id
               JOIN skill_invocations invocation ON invocation.id=judgment.skill_invocation_id
               JOIN task_episodes episode ON episode.id=invocation.task_episode_id
               JOIN sessions session ON session.id=episode.session_id
               WHERE judgment.is_current=1 ORDER BY judgment.created_at DESC, judgment.id DESC"""
        ).fetchall()
        items: list[dict[str, Any]] = []
        selected_samples: set[tuple[Any, ...]] = set()
        for row in rows:
            eligible = bool(
                row["aggregation_eligibility"] == "aggregate-eligible"
                and row["verdict"] in {"helpful", "not-helpful"}
                and row["skill_sha256"] and row["contract_version_id"] != "no-contract"
            )
            exclusion = None if eligible else self._judgment_exclusion(row)
            if eligible and (
                row["evidence_scan_run_id"] != latest["id"]
                or row["evidence_scope_fingerprint"] != expected_scope_fingerprint
            ):
                eligible = False
                exclusion = "evidence-scope-stale"
            if eligible and int(row["evidence_case_revision"]) != int(row["current_case_revision"]):
                eligible = False
                exclusion = "evidence-case-revision-stale"
            sample_key = (
                row["actor_id"], row["task_case_id"], row["skill_id"], row["skill_sha256"],
                row["contract_version_id"], row["task_type"],
            )
            if eligible and sample_key in selected_samples:
                eligible = False
                exclusion = "superseded-case-sample"
            elif eligible:
                selected_samples.add(sample_key)
            items.append({"row": row, "eligible": int(eligible), "exclusion": exclusion})
        summary = {
            "judgments": len(items), "eligible": sum(item["eligible"] for item in items),
            "helpful": sum(1 for item in items if item["eligible"] and item["row"]["verdict"] == "helpful"),
            "notHelpful": sum(1 for item in items if item["eligible"] and item["row"]["verdict"] == "not-helpful"),
        }
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO skill_quality_snapshots(
                       id, scan_run_id, cutoff_at, coverage_status, scope_fingerprint,
                       derivation_cursor, versions_json, summary_json, sealed, created_at)
                   VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, 0, ?)""",
                (snapshot_id, latest["id"], now, expected_scope_fingerprint, cursor,
                 _json({"qualityVersion": QUALITY_VERSION}), _json(summary), now),
            )
            for item in items:
                row = item["row"]
                frozen = {
                    "judgmentId": row["id"], "revision": row["revision"],
                    "verdict": row["verdict"], "evidenceHash": row["evidence_hash"],
                    "attributionRelation": row["attribution_relation"],
                    "source": row["invocation_source"], "invocationCreatedAt": row["invocation_created_at"],
                }
                self.store.execute(
                    """INSERT INTO skill_quality_snapshot_items(
                           snapshot_id, skill_use_judgment_id, skill_id, skill_sha256,
                           contract_version_id, task_type, attribution_kind, metric_eligible,
                           exclusion_reason, frozen_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, row["id"], row["skill_id"], row["skill_sha256"],
                     row["contract_version_id"], row["task_type"], row["attribution_kind"],
                     item["eligible"], item["exclusion"], _json(frozen)),
                )
            self.store.execute("UPDATE skill_quality_snapshots SET sealed=1 WHERE id=?", (snapshot_id,))
        return self.quality_snapshot(snapshot_id)

    def quality_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM skill_quality_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = _decode(row)
        result["items"] = [_decode(item) for item in self.store.execute(
            "SELECT * FROM skill_quality_snapshot_items WHERE snapshot_id=? ORDER BY skill_id, skill_sha256",
            (snapshot_id,),
        ).fetchall()]
        return result

    # -------------------------------------------------------------- internals
    def _directory_item(self, row: Mapping[str, Any], *, task_type: str | None = None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
    ) -> dict[str, Any]:
        coverage = self._coverage()
        funnel = {
            "valid_invocations": row.get("invocation_count", 0), "loaded_invocations": row.get("loaded_count", 0),
            "cases": row.get("case_count", 0), "known_version_cases": row.get("known_version_cases", 0),
            "direct_cases": row.get("direct_case_count", 0), "contract_cases": row.get("contract_cases", 0),
            "assessable_cases": row.get("assessable_cases", 0),
        }
        status, reasons = self._quality_status(
            coverage, funnel, self._formal_results(
                row["skill_id"], row["skill_sha256"], task_type=task_type,
                attribution=attribution, source=source, from_at=from_at, to_at=to_at,
                coverage=coverage,
            ),
        )
        return {**dict(row), "quality_status": status, "blocking_reasons": reasons}

    def _subject(self, skill_id: str, skill_sha256: str | None) -> dict[str, Any] | None:
        if skill_id in {"", "*", "unknown"}:
            return None
        condition = "i.skill_sha256 IS NULL" if skill_sha256 is None else "i.skill_sha256=?"
        params: tuple[Any, ...] = (skill_id,) if skill_sha256 is None else (skill_id, skill_sha256)
        row = self.store.execute(
            f"""SELECT i.skill_id, i.skill_sha256, MAX(i.created_at) AS last_observed_at,
                       COUNT(DISTINCT i.id) AS invocation_count
                FROM skill_invocations i WHERE i.skill_id=? AND {condition}
                  AND i.validity='valid' AND i.invocation_kind='business-use'
                GROUP BY i.skill_id, i.skill_sha256""", params,
        ).fetchone()
        return _decode(row) if row else None

    def _subject_cursor(self, item: Mapping[str, Any]) -> str:
        payload = _json([item["skill_id"], item.get("skill_sha256") or ""])
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _parse_subject_cursor(value: str) -> tuple[str, str]:
        try:
            padded = value + "=" * (-len(value) % 4)
            skill_id, skill_sha = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("invalid quality cursor") from exc
        if not isinstance(skill_id, str) or not isinstance(skill_sha, str):
            raise ValueError("invalid quality cursor")
        return skill_id, skill_sha

    def _coverage(self) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM scan_runs WHERE status!='running' ORDER BY finished_at DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return {"coverage_status": "unknown", "scan_run_id": None, "scope_fingerprint": None}
        decoded = _decode(row)
        metadata = decoded.get("metadata") or {}
        return {
            "scan_run_id": decoded["id"], "coverage_status": decoded["coverage_status"],
            "scope_kind": metadata.get("scopeKind"), "scope_fingerprint": metadata.get("scopeFingerprint"),
            "discovered_files": decoded.get("discovered_files", 0), "indexed_files": decoded.get("indexed_files", 0),
            "pending_files": decoded.get("pending_files", 0), "failed_files": decoded.get("failed_files", 0),
        }

    def _funnel(self, skill_id: str, skill_sha256: str | None, *, task_type: str | None,
        attribution: str | None, source: str | None = None, from_at: str | None = None,
        to_at: str | None = None,
    ) -> dict[str, int]:
        conditions = ["i.skill_id=?", "i.validity='valid'", "i.invocation_kind='business-use'"]
        params: list[Any] = [skill_id]
        if skill_sha256 is None:
            conditions.append("i.skill_sha256 IS NULL")
        else:
            conditions.append("i.skill_sha256=?")
            params.append(skill_sha256)
        if task_type:
            conditions.append("c.task_type=?")
            params.append(task_type)
        if attribution in {"direct", "shared"}:
            conditions.append("l.attribution_kind=?")
            params.append(attribution)
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("session.source=?")
            params.append(source)
        if from_at:
            conditions.append("i.created_at>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("i.created_at<=?")
            params.append(self._timestamp(to_at, "to"))
        row = self.store.execute(
            f"""SELECT COUNT(DISTINCT i.id) AS valid_invocations,
                       COUNT(DISTINCT CASE WHEN i.load_status='loaded' THEN i.id END) AS loaded_invocations,
                       COUNT(DISTINCT c.id) AS cases,
                       COUNT(DISTINCT CASE WHEN i.skill_sha256 IS NOT NULL THEN c.id END) AS known_version_cases,
                       COUNT(DISTINCT CASE WHEN l.attribution_kind='direct' THEN c.id END) AS direct_cases,
                       COUNT(DISTINCT CASE WHEN a.contract_version_id IS NOT NULL THEN c.id END) AS contract_cases,
                       COUNT(DISTINCT CASE WHEN a.assessability='assessable' THEN c.id END) AS assessable_cases
                FROM skill_invocations i
                LEFT JOIN attribution_links l ON l.skill_invocation_id=i.id AND l.status='active'
                LEFT JOIN task_cases c ON c.id=l.task_case_id AND c.invalidated_at IS NULL
                JOIN task_episodes episode ON episode.id=i.task_episode_id
                JOIN sessions session ON session.id=episode.session_id
                LEFT JOIN outcome_assessments a ON a.skill_invocation_id=i.id
                  AND a.task_case_id=l.task_case_id AND a.is_current=1
                WHERE {' AND '.join(conditions)}""", params,
        ).fetchone()
        return {key: int(value or 0) for key, value in dict(row).items()}

    def _formal_results(self, skill_id: str, skill_sha256: str | None, *, task_type: str | None = None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
        coverage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        coverage = coverage or self._coverage()
        if coverage.get("coverage_status") != "complete" or not coverage.get("scan_run_id"):
            return self._empty_formal_results()
        snapshot = self.store.execute(
            """SELECT * FROM metric_snapshots WHERE sealed=1 AND coverage_status='complete'
               AND COALESCE(json_extract(dimensions_json,'$.metricKind'),'')<>'session-negative-feedback'
               AND scan_run_id=? AND json_extract(dimensions_json,'$.scanScopeFingerprint')=?
               AND (json_extract(dimensions_json,'$.skillId') IS NULL
                    OR json_extract(dimensions_json,'$.skillId')=?)
               ORDER BY CASE WHEN json_extract(dimensions_json,'$.skillId') IS NULL THEN 0 ELSE 1 END,
                        cutoff_at DESC, id DESC LIMIT 1""",
            (coverage["scan_run_id"], coverage.get("scope_fingerprint"), skill_id),
        ).fetchone()
        if snapshot is None:
            return self._empty_formal_results()
        conditions = ["metric.snapshot_id=?", "metric.skill_id=?", "metric.metric_eligible=1", "metric.attribution_kind='direct'"]
        params: list[Any] = [snapshot["id"], skill_id]
        if skill_sha256 is None:
            conditions.append("metric.skill_sha256 IS NULL")
        else:
            conditions.append("metric.skill_sha256=?")
            params.append(skill_sha256)
        if task_type:
            conditions.append("metric.task_type=?")
            params.append(task_type)
        if attribution and attribution != "direct":
            return self._empty_formal_results()
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("anchor_session.source=?")
            params.append(source)
        if from_at:
            conditions.append("anchor.created_at>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("anchor.created_at<=?")
            params.append(self._timestamp(to_at, "to"))
        rows = self.store.execute(
            f"""SELECT metric.contract_version_id, metric.task_type, COUNT(*) AS eligible_cases,
                       SUM(metric.effective_verdict='pass') AS pass,
                       SUM(metric.effective_verdict='partial') AS partial,
                       SUM(metric.effective_verdict='fail') AS fail
                FROM metric_snapshot_cases metric
                JOIN skill_invocations anchor ON anchor.id=metric.case_anchor_invocation_id
                JOIN task_episodes anchor_episode ON anchor_episode.id=anchor.task_episode_id
                JOIN sessions anchor_session ON anchor_session.id=anchor_episode.session_id
                WHERE {' AND '.join(conditions)}
                GROUP BY metric.contract_version_id, metric.task_type
                ORDER BY metric.contract_version_id, metric.task_type""", params,
        ).fetchall()
        groups = [self._formal_group(_decode(row)) for row in rows]
        result = {"snapshot_id": snapshot["id"], "groups": groups, "mixed_contracts": len(groups) > 1}
        if len(groups) == 1:
            result.update({key: groups[0][key] for key in ("eligible_cases", "pass", "partial", "fail", "threshold_status")})
            result["contract_version_ids"] = (groups[0]["contract_version_id"],)
        else:
            result.update({"eligible_cases": 0, "pass": 0, "partial": 0, "fail": 0,
                           "threshold_status": "unconfigured",
                           "contract_version_ids": tuple(group["contract_version_id"] for group in groups)})
        return result

    def _empty_formal_results(self) -> dict[str, Any]:
        return {"snapshot_id": None, "eligible_cases": 0, "pass": 0, "partial": 0, "fail": 0,
                "groups": [], "mixed_contracts": False, "contract_version_ids": (), "threshold_status": "unconfigured"}

    def _formal_group(self, row: Mapping[str, Any]) -> dict[str, Any]:
        result = {key: int(row[key] or 0) for key in ("eligible_cases", "pass", "partial", "fail")}
        contract_version_id = str(row["contract_version_id"] or "")
        result["contract_version_id"] = contract_version_id
        result["task_type"] = row["task_type"]
        result["threshold_status"] = "unconfigured"
        if contract_version_id and self.contracts is not None:
            try:
                contract = self.contracts.get(contract_version_id)
            except Exception:
                contract = None
            contract_body = (contract or {}).get("contract", contract or {})
            thresholds = contract_body.get("qualityThresholds") or contract_body.get("quality_thresholds")
            if isinstance(thresholds, Mapping):
                minimum_sample = int(thresholds.get("minimumSample") or thresholds.get("minimum_sample") or 50)
                pass_floor = thresholds.get("passRateLowerBound", thresholds.get("pass_rate_lower_bound"))
                fail_ceiling = thresholds.get("failRateUpperBound", thresholds.get("fail_rate_upper_bound"))
                if isinstance(pass_floor, (int, float)) and isinstance(fail_ceiling, (int, float)):
                    denominator = result["eligible_cases"]
                    pass_lower = self._wilson_lower(result["pass"], denominator)
                    fail_upper = self._wilson_upper(result["fail"], denominator)
                    result.update({
                        "minimum_sample": minimum_sample, "pass_lower_bound": pass_lower,
                        "fail_upper_bound": fail_upper,
                    })
                    result["threshold_status"] = (
                        "met" if denominator >= minimum_sample and pass_lower >= float(pass_floor)
                        and fail_upper <= float(fail_ceiling) else "not-met"
                    )
        return result

    def _feedback_summary(self, skill_id: str, skill_sha256: str | None, *, task_type: str | None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
    ) -> dict[str, int]:
        items = self.feedback_items(
            skill_id, skill_sha256, task_type=task_type, attribution=attribution, source=source,
            from_at=from_at, to_at=to_at, limit=100,
        )
        counts = {"direct_target": 0, "case_direct_context": 0, "case_shared_context": 0, "unattributed": 0, "confirmed_direct": 0}
        for item in items["items"]:
            key = item["relation_kind"].replace("-", "_")
            if key in counts:
                counts[key] += 1
            if item["relation_kind"] == "direct-target" and item["current_confirmed_target_id"] == item["target_id"]:
                counts["confirmed_direct"] += 1
        return counts

    def _judgment_summary(self, skill_id: str, skill_sha256: str | None, *, task_type: str | None,
        attribution: str | None = None, source: str | None = None,
        from_at: str | None = None, to_at: str | None = None,
    ) -> dict[str, Any]:
        coverage = self._coverage()
        quality_snapshot = self.store.execute(
            """SELECT * FROM skill_quality_snapshots WHERE sealed=1 AND coverage_status='complete'
               AND scan_run_id=? AND scope_fingerprint=? ORDER BY cutoff_at DESC, id DESC LIMIT 1""",
            (coverage.get("scan_run_id"), coverage.get("scope_fingerprint")),
        ).fetchone() if coverage.get("coverage_status") == "complete" else None
        conditions = ["snapshot.skill_id=?", "judgment.is_current=1"]
        params: list[Any] = [skill_id]
        if skill_sha256 is None:
            conditions.append("snapshot.skill_sha256 IS NULL")
        else:
            conditions.append("snapshot.skill_sha256=?")
            params.append(skill_sha256)
        if task_type:
            conditions.append("snapshot.task_type=?")
            params.append(task_type)
        if attribution in {"direct", "shared"}:
            conditions.append("snapshot.attribution_kind=?")
            params.append(attribution)
        if source:
            if source not in {"codex", "pi"}:
                raise ValueError("source must be codex or pi")
            conditions.append("session.source=?")
            params.append(source)
        if from_at:
            conditions.append("invocation.created_at>=?")
            params.append(self._timestamp(from_at, "from"))
        if to_at:
            conditions.append("invocation.created_at<=?")
            params.append(self._timestamp(to_at, "to"))
        current = self.store.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(judgment.verdict='helpful') AS helpful,
                       SUM(judgment.verdict='not-helpful') AS not_helpful,
                       SUM(judgment.verdict='cannot-judge') AS cannot_judge,
                       SUM(judgment.aggregation_eligibility='aggregate-eligible') AS eligible_current
                FROM skill_use_judgments judgment
                JOIN use_evidence_snapshots snapshot ON snapshot.id=judgment.evidence_snapshot_id
                JOIN skill_invocations invocation ON invocation.id=judgment.skill_invocation_id
                JOIN task_episodes episode ON episode.id=invocation.task_episode_id
                JOIN sessions session ON session.id=episode.session_id
                WHERE {' AND '.join(conditions)}""", params,
        ).fetchone()
        pending = {key: int(value or 0) for key, value in dict(current).items()}
        if quality_snapshot is None:
            return {
                "snapshot_id": None, "official": False, "pending_judgments": pending["total"],
                "total": 0, "helpful": 0, "not_helpful": 0, "cannot_judge": 0, "eligible_current": 0,
            }
        snapshot_conditions = ["item.snapshot_id=?", "item.skill_id=?", "item.metric_eligible=1"]
        snapshot_params: list[Any] = [quality_snapshot["id"], skill_id]
        if skill_sha256 is None:
            snapshot_conditions.append("item.skill_sha256 IS NULL")
        else:
            snapshot_conditions.append("item.skill_sha256=?")
            snapshot_params.append(skill_sha256)
        if task_type:
            snapshot_conditions.append("item.task_type=?")
            snapshot_params.append(task_type)
        if attribution in {"direct", "shared"}:
            snapshot_conditions.append("item.attribution_kind=?")
            snapshot_params.append(attribution)
        if source:
            snapshot_conditions.append("json_extract(item.frozen_json,'$.source')=?")
            snapshot_params.append(source)
        if from_at:
            snapshot_conditions.append("json_extract(item.frozen_json,'$.invocationCreatedAt')>=?")
            snapshot_params.append(self._timestamp(from_at, "from"))
        if to_at:
            snapshot_conditions.append("json_extract(item.frozen_json,'$.invocationCreatedAt')<=?")
            snapshot_params.append(self._timestamp(to_at, "to"))
        sealed = self.store.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(json_extract(item.frozen_json,'$.verdict')='helpful') AS helpful,
                       SUM(json_extract(item.frozen_json,'$.verdict')='not-helpful') AS not_helpful
                FROM skill_quality_snapshot_items item WHERE {' AND '.join(snapshot_conditions)}""",
            snapshot_params,
        ).fetchone()
        return {
            "snapshot_id": quality_snapshot["id"], "official": True, "pending_judgments": pending["total"],
            "total": int(sealed["total"] or 0), "helpful": int(sealed["helpful"] or 0),
            "not_helpful": int(sealed["not_helpful"] or 0), "cannot_judge": 0,
            "eligible_current": int(sealed["total"] or 0),
        }

    def _quality_status(self, coverage: Mapping[str, Any], funnel: Mapping[str, Any], results: Mapping[str, Any]) -> tuple[str, list[str]]:
        reasons: list[str] = []
        if not int(funnel.get("valid_invocations") or funnel.get("invocation_count") or 0):
            return "unobserved", ["no-valid-business-use"]
        if not int(funnel.get("cases") or funnel.get("case_count") or 0):
            return "only-loaded", ["no-task-case"]
        if coverage.get("coverage_status") != "complete":
            reasons.append("coverage-partial")
        if not int(funnel.get("known_version_cases") or 0):
            reasons.append("version-unknown")
        if not int(funnel.get("contract_cases") or 0):
            reasons.append("contract-missing")
        if not int(funnel.get("assessable_cases") or 0):
            reasons.append("assessment-evidence-missing")
        if not results.get("snapshot_id"):
            reasons.append("formal-snapshot-missing")
        if results.get("mixed_contracts"):
            reasons.append("multiple-statistical-keys")
        count = int(results.get("eligible_cases") or 0)
        if reasons:
            return ("not-publishable" if coverage.get("coverage_status") != "complete" else "evidence-insufficient"), reasons
        if count < 20:
            return "evidence-insufficient", ["sample-under-20"]
        if count < 50:
            return "directional", []
        if results.get("threshold_status") == "met":
            return "judgment-supported", []
        if results.get("threshold_status") == "not-met":
            return "threshold-not-met", []
        return "directional", ["contract-quality-threshold-unconfigured"]

    def _contract_summary(self, skill_id: str, skill_sha256: str | None) -> dict[str, Any]:
        rows = self.store.execute(
            """SELECT DISTINCT contract_version_id FROM outcome_assessments
               WHERE skill_id=? AND ((? IS NULL AND skill_sha256 IS NULL) OR skill_sha256=?)
                 AND contract_version_id IS NOT NULL ORDER BY contract_version_id""",
            (skill_id, skill_sha256, skill_sha256),
        ).fetchall()
        return {"contract_version_ids": [row["contract_version_id"] for row in rows]}

    def _invocation_context(self, invocation_id: str) -> dict[str, Any] | None:
        rows = self.store.execute(
            """SELECT i.*, link.task_case_id, link.attribution_kind, task_case.current_revision AS case_revision,
                       task_case.task_type
                FROM skill_invocations i JOIN attribution_links link ON link.skill_invocation_id=i.id
                JOIN task_cases task_case ON task_case.id=link.task_case_id
                WHERE i.id=? AND i.validity='valid' AND i.invocation_kind='business-use'
                  AND link.status='active' AND task_case.invalidated_at IS NULL
                ORDER BY CASE link.attribution_kind WHEN 'direct' THEN 0 ELSE 1 END, link.task_case_id LIMIT 1""",
            (invocation_id,),
        ).fetchone()
        return _decode(rows) if rows else None

    def _active_assignment(self, invocation_id: str, actor_id: str) -> Any:
        return self.store.execute(
            """SELECT * FROM skill_use_judgment_assignments WHERE skill_invocation_id=? AND actor_id=?
               AND status='active' AND (expires_at IS NULL OR expires_at>?)""",
            (invocation_id, actor_id, _now()),
        ).fetchone()

    def _assignment(self, assignment_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM skill_use_judgment_assignments WHERE id=?", (assignment_id,)).fetchone()
        if row is None:
            raise KeyError(assignment_id)
        return _decode(row)

    def _contract_for_invocation(self, case_id: str, invocation_id: str) -> str | None:
        row = self.store.execute(
            """SELECT contract_version_id FROM outcome_assessments
               WHERE task_case_id=? AND skill_invocation_id=? AND is_current=1
                 AND contract_version_id IS NOT NULL ORDER BY revision DESC LIMIT 1""",
            (case_id, invocation_id),
        ).fetchone()
        return row["contract_version_id"] if row else None

    def _evidence_snapshot(self, context: Mapping[str, Any], contract_id: str) -> dict[str, Any]:
        case_id = context["task_case_id"]
        goal = self.store.execute(
            """SELECT goal_text FROM task_episodes episode JOIN task_case_episodes link
               ON link.task_episode_id=episode.id WHERE link.task_case_id=?
               ORDER BY episode.created_at LIMIT 1""", (case_id,)
        ).fetchone()
        checks = self.store.execute(
            """SELECT status, assertion_outcome, freshness FROM check_runs
               WHERE task_case_id=? AND case_revision=? ORDER BY started_at DESC LIMIT 5""",
            (case_id, context["case_revision"]),
        ).fetchall()
        assessments = self.store.execute(
            """SELECT assessability, automated_verdict, freshness FROM outcome_assessments
               WHERE task_case_id=? AND skill_invocation_id=? AND case_revision=? AND is_current=1""",
            (case_id, context["id"], context["case_revision"]),
        ).fetchall()
        feedback_count = self.store.execute(
            """SELECT COUNT(*) FROM feedback_targets WHERE context_task_case_id=? OR target_task_case_id=?""",
            (case_id, case_id),
        ).fetchone()[0]
        coverage = self._coverage()
        view = {
            "goal": redact_sensitive(goal["goal_text"]) if goal and goal["goal_text"] else None,
            "skill": {"id": context["skill_id"], "sha": context["skill_sha256"], "loadStatus": context["load_status"]},
            "case": {"id": case_id, "revision": context["case_revision"], "taskType": context["task_type"]},
            "contractVersionId": contract_id,
            "checks": [{"status": row["status"], "assertionOutcome": row["assertion_outcome"], "freshness": row["freshness"]} for row in checks],
            "assessments": [{"assessability": row["assessability"], "verdict": row["automated_verdict"], "freshness": row["freshness"]} for row in assessments],
            "relatedFeedback": {"count": int(feedback_count), "contentAvailable": False},
            "coverage": coverage,
        }
        return {"view": view, "hash": _digest(view), "scan_run_id": coverage.get("scan_run_id"), "scope_fingerprint": coverage.get("scope_fingerprint")}

    def _judgment_eligibility(self, context: Mapping[str, Any], verdict: str, relation: str) -> str:
        if relation != "direct-skill-use" or verdict not in {"helpful", "not-helpful"}:
            return "individual-only"
        if context["attribution_kind"] == "shared":
            return "shared-only"
        if context["attribution_kind"] != "direct" or not context.get("skill_sha256"):
            return "excluded"
        return "aggregate-eligible"

    def _require_role(self, actor_id: str, role: str) -> None:
        row = self.store.execute("SELECT active, roles_json FROM actors WHERE id=?", (actor_id,)).fetchone()
        roles = set(json.loads(row["roles_json"])) if row else set()
        if row is None or not row["active"] or (role not in roles and not (role == "trial_user" and "admin" in roles)):
            raise EffectStoreError(f"{role} role is required")

    def _timestamp(self, value: str, field: str) -> str:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{field} must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _convert_referral(self, referral: Mapping[str, Any], judgment: Mapping[str, Any]) -> str:
        snapshot = self.store.execute("SELECT * FROM use_evidence_snapshots WHERE id=?", (judgment["evidence_snapshot_id"],)).fetchone()
        if snapshot is None:
            raise EffectStoreError("judgment evidence snapshot is missing")
        event_fingerprint = _digest("trial-judgment-event", judgment["id"], referral["id"])
        payload = {"text": judgment["reason_code"] or "trial-not-helpful", "metadata": {"judgmentId": judgment["id"], "referralId": referral["id"], "trial": True}}
        event = self.store.upsert_event(
            event_fingerprint, source="trial-judgment", session_family=f"trial:{judgment['actor_id']}",
            source_event_id=referral["id"], event_type="trial_judgment",
            payload_hash=_digest(payload), payload=payload, protocol_time=judgment["created_at"],
        )
        span = {
            "event_id": event["id"], "block_index": 0, "start": 0, "end": len(payload["text"]),
            "origin": "trial-judgment", "excerpt_hash": _digest(payload["text"]),
            "redacted_excerpt": payload["text"], "protocol_locator": f"judgment:{judgment['id']}",
            "redaction_status": "clean", "truncated": False,
        }
        candidate = {
            "channel": "trial-experience", "category": "trial-not-helpful", "severity": "medium",
            "confidence": 1.0, "authority": "user", "detector_id": "trial-judgment-referral",
            "detector_version": QUALITY_VERSION, "span": span, "metadata": {"judgmentId": judgment["id"], "referralId": referral["id"]},
        }
        target = {
            "target_kind": "skill-invocation", "skill_invocation_id": judgment["skill_invocation_id"],
            "context_task_case_id": judgment["task_case_id"], "target_task_case_id": None,
            "target_event_id": None, "tool_call_id": None, "tool_result_id": None,
            "relation": "trial-judgment-referral", "confidence": 1.0,
            "evidence": {"judgmentId": judgment["id"], "snapshotId": judgment["evidence_snapshot_id"]},
        }
        result = self.feedback._persist_candidate(
            event, judgment["task_case_id"], candidate, [target], source="trial-judgment", queue=False,
        )
        return result["id"]

    def _judgment_exclusion(self, row: Mapping[str, Any]) -> str:
        if row["contract_version_id"] == "no-contract":
            return "contract-missing"
        if not row["skill_sha256"]:
            return "version-unknown"
        if row["attribution_relation"] != "direct-skill-use":
            return "attribution-not-direct"
        if row["attribution_kind"] != "direct":
            return "shared-or-non-direct"
        return "verdict-ineligible"

    @staticmethod
    def _wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
        if total <= 0:
            return 0.0
        proportion = successes / total
        denominator = 1 + z * z / total
        centre = proportion + z * z / (2 * total)
        spread = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
        return max(0.0, (centre - spread) / denominator)

    @staticmethod
    def _wilson_upper(successes: int, total: int, z: float = 1.959963984540054) -> float:
        if total <= 0:
            return 1.0
        proportion = successes / total
        denominator = 1 + z * z / total
        centre = proportion + z * z / (2 * total)
        spread = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
        return min(1.0, (centre + spread) / denominator)