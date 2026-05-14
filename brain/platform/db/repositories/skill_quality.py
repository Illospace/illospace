"""Repository helpers for skill quality evidence."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

from sqlalchemy import func, select

from brain.platform.db.models.skill_quality import SkillRunEvidence
from brain.platform.db.repositories.base import BaseRepository


class SkillRunEvidenceRepository(BaseRepository[SkillRunEvidence]):
    """Persistence access for per-run skill quality evidence."""

    model = SkillRunEvidence

    async def a_record_evidence_idempotent(
        self,
        *,
        skill_name: str,
        skill_effective_digest: str,
        run_id: int | None = None,
        skill_id: int | None = None,
        bundle_namespace: str | None = None,
        bundle_name: str | None = None,
        bundle_version: str | None = None,
        bundle_digest: str | None = None,
        trace_id: str | None = None,
        task_class: str | None = None,
        outcome_label: str | None = None,
        verifier_status: str | None = None,
        user_feedback: str | None = None,
        token_bucket: str | None = None,
        total_tokens: int | None = None,
        cost_bucket: str | None = None,
        cost_usd: float | None = None,
        runtime_bucket: str | None = None,
        runtime_ms: int | None = None,
        tool_risk_class: str | None = None,
        action_risk_class: str | None = None,
        evidence_source: str | None = None,
        notes: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> SkillRunEvidence:
        if run_id is not None:
            existing = await self.a_get_by_run_digest(run_id, skill_effective_digest)
            if existing is not None:
                return existing

        evidence = SkillRunEvidence(
            skill_id=skill_id,
            skill_name=skill_name,
            skill_effective_digest=skill_effective_digest,
            bundle_namespace=bundle_namespace,
            bundle_name=bundle_name,
            bundle_version=bundle_version,
            bundle_digest=bundle_digest,
            run_id=run_id,
            trace_id=trace_id,
            task_class=task_class,
            outcome_label=outcome_label,
            verifier_status=verifier_status,
            user_feedback=user_feedback,
            token_bucket=token_bucket,
            total_tokens=total_tokens,
            cost_bucket=cost_bucket,
            cost_usd=cost_usd,
            runtime_bucket=runtime_bucket,
            runtime_ms=runtime_ms,
            tool_risk_class=tool_risk_class,
            action_risk_class=action_risk_class,
            evidence_source=evidence_source,
            notes=notes,
            org_id=org_id,
            user_id=user_id,
        )
        self._session.add(evidence)
        await self._session.flush()
        return evidence

    async def a_get_by_run_digest(
        self,
        run_id: int,
        skill_effective_digest: str,
    ) -> SkillRunEvidence | None:
        stmt = select(SkillRunEvidence).where(
            SkillRunEvidence.run_id == run_id,
            SkillRunEvidence.skill_effective_digest == skill_effective_digest,
        )
        return (await self._session.scalars(stmt)).first()

    async def a_list_by_skill(
        self,
        *,
        skill_effective_digest: str | None = None,
        skill_name: str | None = None,
        limit: int = 100,
    ) -> Sequence[SkillRunEvidence]:
        if not skill_effective_digest and not skill_name:
            raise ValueError("skill_effective_digest or skill_name is required")

        stmt = select(SkillRunEvidence)
        if skill_effective_digest:
            stmt = stmt.where(
                SkillRunEvidence.skill_effective_digest == skill_effective_digest
            )
        if skill_name:
            stmt = stmt.where(SkillRunEvidence.skill_name == skill_name)
        stmt = stmt.order_by(SkillRunEvidence.created_at.desc(), SkillRunEvidence.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        return (await self._session.scalars(stmt)).all()

    async def a_aggregate_counts(
        self,
        *,
        skill_effective_digest: str | None = None,
        skill_name: str | None = None,
    ) -> dict[str, Any]:
        stmt = select(
            SkillRunEvidence.outcome_label,
            SkillRunEvidence.verifier_status,
            SkillRunEvidence.user_feedback,
            func.count(SkillRunEvidence.id),
        )
        if skill_effective_digest:
            stmt = stmt.where(
                SkillRunEvidence.skill_effective_digest == skill_effective_digest
            )
        if skill_name:
            stmt = stmt.where(SkillRunEvidence.skill_name == skill_name)
        stmt = stmt.group_by(
            SkillRunEvidence.outcome_label,
            SkillRunEvidence.verifier_status,
            SkillRunEvidence.user_feedback,
        )

        total = 0
        counts: dict[str, dict[str, int]] = {
            "by_outcome_label": defaultdict(int),
            "by_verifier_status": defaultdict(int),
            "by_user_feedback": defaultdict(int),
        }
        result = await self._session.execute(stmt)
        for outcome_label, verifier_status, user_feedback, count in result:
            total += count
            counts["by_outcome_label"][outcome_label or "unknown"] += count
            counts["by_verifier_status"][verifier_status or "unknown"] += count
            counts["by_user_feedback"][user_feedback or "unknown"] += count

        return {
            "total": total,
            "by_outcome_label": dict(counts["by_outcome_label"]),
            "by_verifier_status": dict(counts["by_verifier_status"]),
            "by_user_feedback": dict(counts["by_user_feedback"]),
        }
