"""Repositories for external personal-agent connections and tasks."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
    ExternalAgentTaskArtifactRow,
    ExternalAgentTaskEventRow,
    ExternalAgentTaskRow,
)
from brain.platform.db.repositories.base import BaseRepository


class ExternalAgentConnectionRepository(BaseRepository[ExternalAgentConnectionRow]):
    model = ExternalAgentConnectionRow
    pk_column = "id"

    def list_for_org(self, org_id: str, *, owner_user_id: str | None = None) -> Sequence[ExternalAgentConnectionRow]:
        stmt = (
            select(ExternalAgentConnectionRow)
            .where(ExternalAgentConnectionRow.org_id == str(org_id))
            .order_by(ExternalAgentConnectionRow.created_at.desc(), ExternalAgentConnectionRow.id.desc())
        )
        if owner_user_id:
            stmt = stmt.where(ExternalAgentConnectionRow.owner_user_id == str(owner_user_id))
        return self._session.scalars(stmt).all()


class ExternalAgentConnectionTokenRepository(BaseRepository[ExternalAgentConnectionTokenRow]):
    model = ExternalAgentConnectionTokenRow
    pk_column = "id"

    def get_by_hash(self, token_hash: str) -> ExternalAgentConnectionTokenRow | None:
        return self._session.scalars(
            select(ExternalAgentConnectionTokenRow)
            .where(ExternalAgentConnectionTokenRow.token_hash == token_hash)
            .limit(1)
        ).first()


class ExternalAgentTaskRepository(BaseRepository[ExternalAgentTaskRow]):
    model = ExternalAgentTaskRow
    pk_column = "id"

    def list_for_connection(
        self,
        connection_id: str,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> Sequence[ExternalAgentTaskRow]:
        stmt = (
            select(ExternalAgentTaskRow)
            .where(ExternalAgentTaskRow.connection_id == str(connection_id))
            .order_by(ExternalAgentTaskRow.created_at.desc(), ExternalAgentTaskRow.id.desc())
            .limit(limit)
        )
        if statuses:
            stmt = stmt.where(ExternalAgentTaskRow.status.in_([str(status) for status in statuses]))
        return self._session.scalars(stmt).all()

    def list_for_idea(self, idea_id: str, *, limit: int = 50) -> Sequence[ExternalAgentTaskRow]:
        stmt = (
            select(ExternalAgentTaskRow)
            .where(ExternalAgentTaskRow.source_idea_id == str(idea_id))
            .order_by(ExternalAgentTaskRow.created_at.desc(), ExternalAgentTaskRow.id.desc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()


class ExternalAgentTaskEventRepository(BaseRepository[ExternalAgentTaskEventRow]):
    model = ExternalAgentTaskEventRow

    def list_for_task(self, task_id: str, *, limit: int = 200) -> Sequence[ExternalAgentTaskEventRow]:
        stmt = (
            select(ExternalAgentTaskEventRow)
            .where(ExternalAgentTaskEventRow.task_id == str(task_id))
            .order_by(ExternalAgentTaskEventRow.sequence_no.asc(), ExternalAgentTaskEventRow.id.asc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()


class ExternalAgentTaskArtifactRepository(BaseRepository[ExternalAgentTaskArtifactRow]):
    model = ExternalAgentTaskArtifactRow
    pk_column = "id"

    def list_for_task(self, task_id: str) -> Sequence[ExternalAgentTaskArtifactRow]:
        stmt = (
            select(ExternalAgentTaskArtifactRow)
            .where(ExternalAgentTaskArtifactRow.task_id == str(task_id))
            .order_by(ExternalAgentTaskArtifactRow.created_at.asc(), ExternalAgentTaskArtifactRow.id.asc())
        )
        return self._session.scalars(stmt).all()
