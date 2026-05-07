"""Repositories for durable learning evidence and policy candidates."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select

from brain.platform.db.models.learning import (
    LearningSignal,
    PolicyUpdateCandidate,
    TrajectoryEvalCase,
)
from brain.platform.db.repositories.base import BaseRepository


class LearningSignalRepository(BaseRepository[LearningSignal]):
    """Append and replay learning signals with digest idempotency."""

    model = LearningSignal

    def get_by_digest(self, signal_digest: str) -> LearningSignal | None:
        stmt = select(LearningSignal).where(LearningSignal.signal_digest == signal_digest)
        return self._session.scalars(stmt).first()

    def record_signal(self, *, signal_digest: str, signal_type: str, **values: Any) -> LearningSignal:
        existing = self.get_by_digest(signal_digest)
        if existing is not None:
            self._update(existing, signal_type=signal_type, **values)
            return existing
        signal = LearningSignal(
            signal_digest=signal_digest,
            signal_type=signal_type,
            **values,
        )
        self._session.add(signal)
        self._session.flush()
        return signal

    def list_by_scope(
        self,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        signal_type: str | None = None,
        limit: int = 100,
    ) -> Sequence[LearningSignal]:
        stmt = select(LearningSignal).order_by(LearningSignal.created_at.desc(), LearningSignal.id.desc())
        if org_id is not None:
            stmt = stmt.where(LearningSignal.org_id == org_id)
        if user_id is not None:
            stmt = stmt.where(LearningSignal.user_id == user_id)
        if signal_type is not None:
            stmt = stmt.where(LearningSignal.signal_type == signal_type)
        return self._session.scalars(stmt.limit(limit)).all()

    def _update(self, row: LearningSignal, **values: Any) -> None:
        for key, value in values.items():
            if value is not None and hasattr(row, key):
                setattr(row, key, value)
        self._session.flush()


class TrajectoryEvalCaseRepository(BaseRepository[TrajectoryEvalCase]):
    """Store compact eval cases derived from run trajectories."""

    model = TrajectoryEvalCase

    def get_by_digest(self, eval_digest: str) -> TrajectoryEvalCase | None:
        stmt = select(TrajectoryEvalCase).where(TrajectoryEvalCase.eval_digest == eval_digest)
        return self._session.scalars(stmt).first()

    def upsert_eval_case(self, *, eval_digest: str, payload: dict[str, Any], **values: Any) -> TrajectoryEvalCase:
        existing = self.get_by_digest(eval_digest)
        if existing is not None:
            existing.payload = payload
            for key, value in values.items():
                if value is not None and hasattr(existing, key):
                    setattr(existing, key, value)
            self._session.flush()
            return existing
        row = TrajectoryEvalCase(eval_digest=eval_digest, payload=payload, **values)
        self._session.add(row)
        self._session.flush()
        return row


class PolicyUpdateCandidateRepository(BaseRepository[PolicyUpdateCandidate]):
    """Store proposed active-learning policy changes before rollout."""

    model = PolicyUpdateCandidate

    def get_by_digest(self, candidate_digest: str) -> PolicyUpdateCandidate | None:
        stmt = select(PolicyUpdateCandidate).where(PolicyUpdateCandidate.candidate_digest == candidate_digest)
        return self._session.scalars(stmt).first()

    def upsert_candidate(
        self,
        *,
        candidate_digest: str,
        candidate_type: str,
        policy_payload: dict[str, Any],
        **values: Any,
    ) -> PolicyUpdateCandidate:
        existing = self.get_by_digest(candidate_digest)
        if existing is not None:
            existing.candidate_type = candidate_type
            existing.policy_payload = policy_payload
            for key, value in values.items():
                if value is not None and hasattr(existing, key):
                    setattr(existing, key, value)
            self._session.flush()
            return existing
        row = PolicyUpdateCandidate(
            candidate_digest=candidate_digest,
            candidate_type=candidate_type,
            policy_payload=policy_payload,
            **values,
        )
        self._session.add(row)
        self._session.flush()
        return row
