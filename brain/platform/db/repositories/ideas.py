"""IdeaRepository — domain queries for ideas, threads, connections."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from typing import Sequence

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased, load_only

from brain.platform.db.models.idea import (
    Idea,
    IdeaConnection,
    IdeaStateLog,
    IdeaThread,
    UserMention,
)
from brain.platform.db.models.org import User
from brain.platform.db.repositories.base import BaseRepository


IDEA_LIST_LOAD_COLUMNS = (
    Idea.id,
    Idea.title,
    Idea.display_title,
    Idea.description,
    Idea.status,
    Idea.origin,
    Idea.origin_ref,
    Idea.salience_score,
    Idea.position_x,
    Idea.position_y,
    Idea.position_sticky,
    Idea.orbit_anchor_type,
    Idea.orbit_anchor_id,
    Idea.parent_id,
    Idea.created_at,
    Idea.updated_at,
    Idea.archived_at,
    Idea.user_id,
    Idea.org_id,
    Idea.active_agents,
    Idea.agent_details,
    Idea.attachments,
)


class IdeaRepository(BaseRepository[Idea]):
    model = Idea
    pk_column = "id"

    @staticmethod
    def _list_active_stmt(*, limit: int | None = None):
        stmt = (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(Idea.archived_at.is_(None))
            .order_by(Idea.updated_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return stmt

    @staticmethod
    def _get_for_org_stmt(idea_id: str, org_id: str):
        return select(Idea).where(Idea.id == idea_id, Idea.org_id == org_id)

    @staticmethod
    def _list_active_for_org_stmt(org_id: str, *, limit: int | None = None):
        stmt = (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(Idea.org_id == org_id, Idea.archived_at.is_(None))
            .order_by(Idea.updated_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return stmt

    @staticmethod
    def _list_archived_stmt(*, limit: int | None = None):
        stmt = (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(Idea.archived_at.is_not(None))
            .order_by(Idea.archived_at.desc(), Idea.updated_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return stmt

    @staticmethod
    def _list_archived_for_org_stmt(org_id: str, *, limit: int | None = None):
        org_user_ids = select(User.id).where(User.org_id == str(org_id))
        stmt = (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(
                Idea.archived_at.is_not(None),
                or_(
                    Idea.org_id == org_id,
                    and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
                ),
            )
            .order_by(Idea.archived_at.desc(), Idea.updated_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return stmt

    async def a_hard_delete_archived(self) -> int:
        idea_ids = list(
            (
                await self._session.scalars(
                    select(Idea.id).where(Idea.archived_at.is_not(None))
                )
            ).all()
        )
        return await self._a_hard_delete_by_ids(idea_ids)

    async def a_hard_delete_archived_for_org(self, org_id: str) -> int:
        org_user_ids = select(User.id).where(User.org_id == str(org_id))
        idea_ids = list(
            (
                await self._session.scalars(
                    select(Idea.id).where(
                        Idea.archived_at.is_not(None),
                        or_(
                            Idea.org_id == org_id,
                            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
                        ),
                    )
                )
            ).all()
        )
        return await self._a_hard_delete_by_ids(idea_ids)

    async def _a_hard_delete_by_ids(self, idea_ids: Sequence[str]) -> int:
        ids = [str(idea_id) for idea_id in idea_ids if idea_id]
        if not ids:
            return 0
        await self._session.execute(
            update(Idea)
            .where(Idea.parent_id.in_(ids))
            .values(parent_id=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            delete(Idea)
            .where(Idea.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()
        return len(ids)

    @staticmethod
    def _list_by_status_stmt(status: str):
        return (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(Idea.status == status, Idea.archived_at.is_(None))
            .order_by(Idea.updated_at.desc())
        )

    @staticmethod
    def _list_by_status_for_org_stmt(status: str, org_id: str):
        return (
            select(Idea)
            .options(load_only(*IDEA_LIST_LOAD_COLUMNS))
            .where(
                Idea.org_id == org_id,
                Idea.status == status,
                Idea.archived_at.is_(None),
            )
            .order_by(Idea.updated_at.desc())
        )

    async def a_list_active(self, *, limit: int | None = None) -> Sequence[Idea]:
        stmt = self._list_active_stmt(limit=limit)
        return (await self._session.scalars(stmt)).all()

    async def a_list_by_org(
        self, org_id: str, *, limit: int | None = None
    ) -> Sequence[Idea]:
        return await self.a_list_active_for_org(org_id, limit=limit)

    async def a_get_for_org(self, idea_id: str, org_id: str) -> Idea | None:
        stmt = self._get_for_org_stmt(idea_id, org_id)
        return (await self._session.scalars(stmt)).first()

    async def a_get_for_org_or_raise(self, idea_id: str, org_id: str) -> Idea:
        idea = await self.a_get_for_org(idea_id, org_id)
        if idea is None:
            raise LookupError(f"Idea {idea_id} not found")
        return idea

    async def a_list_active_for_org(
        self, org_id: str, *, limit: int | None = None
    ) -> Sequence[Idea]:
        stmt = self._list_active_for_org_stmt(org_id, limit=limit)
        return (await self._session.scalars(stmt)).all()

    async def a_list_archived(self, *, limit: int | None = None) -> Sequence[Idea]:
        stmt = self._list_archived_stmt(limit=limit)
        return (await self._session.scalars(stmt)).all()

    async def a_list_archived_for_org(
        self, org_id: str, *, limit: int | None = None
    ) -> Sequence[Idea]:
        stmt = self._list_archived_for_org_stmt(org_id, limit=limit)
        return (await self._session.scalars(stmt)).all()

    async def a_list_by_status(self, status: str) -> Sequence[Idea]:
        stmt = self._list_by_status_stmt(status)
        return (await self._session.scalars(stmt)).all()

    async def a_list_by_status_for_org(self, status: str, org_id: str) -> Sequence[Idea]:
        stmt = self._list_by_status_for_org_stmt(status, org_id)
        return (await self._session.scalars(stmt)).all()

    async def a_update_status(
        self, idea_id: str, new_status: str, trigger: str | None = None
    ) -> Idea:
        idea = await self.a_get_or_raise(idea_id)
        old_status = idea.status
        idea.status = new_status
        log = IdeaStateLog(
            idea_id=idea_id,
            from_state=old_status,
            to_state=new_status,
            trigger=trigger,
        )
        self._session.add(log)
        return idea

    async def a_archive(self, idea_id: str) -> Idea:
        from datetime import datetime, timezone

        idea = await self.a_get_or_raise(idea_id)
        idea.archived_at = datetime.now(timezone.utc)
        return idea


class IdeaThreadRepository(BaseRepository[IdeaThread]):
    model = IdeaThread

    async def a_list_by_idea(self, idea_id: str) -> Sequence[IdeaThread]:
        stmt = (
            select(IdeaThread)
            .where(IdeaThread.idea_id == idea_id)
            .order_by(IdeaThread.created_at.asc())
        )
        return (await self._session.scalars(stmt)).all()

    async def a_add_message(
        self,
        idea_id: str,
        role: str,
        content: str,
        user_id: str | None = None,
    ) -> IdeaThread:
        message = IdeaThread(
            idea_id=idea_id,
            role=role,
            content=content,
            user_id=user_id,
        )
        self._session.add(message)
        return message


class IdeaConnectionRepository(BaseRepository[IdeaConnection]):
    model = IdeaConnection
    pk_column = "id"

    async def a_list_by_idea(self, idea_id: str) -> Sequence[IdeaConnection]:
        stmt = select(IdeaConnection).where(
            or_(
                IdeaConnection.source_id == idea_id,
                IdeaConnection.target_id == idea_id,
            )
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_all_active(self) -> Sequence[IdeaConnection]:
        return (await self._session.scalars(select(IdeaConnection))).all()

    async def a_list_by_idea_for_org(
        self, idea_id: str, org_id: str
    ) -> Sequence[IdeaConnection]:
        source = aliased(Idea)
        target = aliased(Idea)
        stmt = (
            select(IdeaConnection)
            .join(source, IdeaConnection.source_id == source.id)
            .join(target, IdeaConnection.target_id == target.id)
            .where(
                and_(
                    or_(
                        IdeaConnection.source_id == idea_id,
                        IdeaConnection.target_id == idea_id,
                    ),
                    source.org_id == org_id,
                    target.org_id == org_id,
                )
            )
            .order_by(IdeaConnection.created_at)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_all_active_for_org(self, org_id: str) -> Sequence[IdeaConnection]:
        source = aliased(Idea)
        target = aliased(Idea)
        stmt = (
            select(IdeaConnection)
            .join(source, IdeaConnection.source_id == source.id)
            .join(target, IdeaConnection.target_id == target.id)
            .where(source.org_id == org_id, target.org_id == org_id)
            .order_by(IdeaConnection.created_at)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_get_for_org(self, connection_id: str, org_id: str) -> IdeaConnection | None:
        source = aliased(Idea)
        target = aliased(Idea)
        stmt = (
            select(IdeaConnection)
            .join(source, IdeaConnection.source_id == source.id)
            .join(target, IdeaConnection.target_id == target.id)
            .where(
                IdeaConnection.id == connection_id,
                source.org_id == org_id,
                target.org_id == org_id,
            )
        )
        return (await self._session.scalars(stmt)).first()


class UserMentionRepository(BaseRepository[UserMention]):
    model = UserMention

    @staticmethod
    def _mark_seen_for_idea_stmt(*, user_id: str, idea_id: str):
        return (
            update(UserMention)
            .where(
                UserMention.user_id == user_id,
                UserMention.idea_id == idea_id,
                UserMention.seen_at.is_(None),
            )
            .values(seen_at=datetime.now(timezone.utc))
        )

    @staticmethod
    def _mark_seen_for_thread_message_stmt(
        *,
        user_id: str,
        idea_id: str,
        thread_message_id: int,
    ):
        return (
            update(UserMention)
            .where(
                UserMention.user_id == user_id,
                UserMention.idea_id == idea_id,
                UserMention.thread_message_id == thread_message_id,
                UserMention.seen_at.is_(None),
            )
            .values(seen_at=datetime.now(timezone.utc))
        )

    async def a_create_if_missing(
        self,
        *,
        user_id: str,
        idea_id: str,
        mentioned_by: str,
        thread_message_id: int | None = None,
    ) -> tuple[UserMention, bool]:
        mention_id = await self._session.scalar(
            insert(UserMention)
            .values(
                user_id=user_id,
                idea_id=idea_id,
                mentioned_by=mentioned_by,
                thread_message_id=thread_message_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    UserMention.user_id,
                    UserMention.idea_id,
                    UserMention.thread_message_id,
                ]
            )
            .returning(UserMention.id)
        )
        if mention_id is not None:
            mention = await self.a_get(int(mention_id))
            if mention is None:
                raise LookupError(f"UserMention {mention_id} was inserted but not found")
            return mention, True

        mention = (
            await self._session.scalars(
                select(UserMention).where(
                    UserMention.user_id == user_id,
                    UserMention.idea_id == idea_id,
                    UserMention.thread_message_id == thread_message_id,
                )
            )
        ).first()
        if mention is None:
            raise LookupError("Existing user mention could not be loaded after conflict")
        return mention, False

    async def a_mark_seen_for_idea(self, *, user_id: str, idea_id: str) -> int:
        result = await self._session.execute(
            self._mark_seen_for_idea_stmt(user_id=user_id, idea_id=idea_id)
        )
        return int(result.rowcount or 0)

    async def a_mark_seen_for_thread_message(
        self,
        *,
        user_id: str,
        idea_id: str,
        thread_message_id: int,
    ) -> int:
        result = await self._session.execute(
            self._mark_seen_for_thread_message_stmt(
                user_id=user_id,
                idea_id=idea_id,
                thread_message_id=thread_message_id,
            )
        )
        return int(result.rowcount or 0)

    async def a_list_unread_for_user(self, *, user_id: str) -> list[dict[str, Any]]:
        rows = (
            await self._session.execute(
                select(
                    UserMention.idea_id,
                    UserMention.created_at,
                    UserMention.mentioned_by,
                    User.name.label("mentioner_name"),
                    User.color.label("mentioner_color"),
                )
                .join(User, UserMention.mentioned_by == User.id)
                .where(
                    UserMention.user_id == user_id,
                    UserMention.seen_at.is_(None),
                )
                .order_by(UserMention.created_at.desc())
            )
        ).mappings().all()
        return [dict(row) for row in rows]
