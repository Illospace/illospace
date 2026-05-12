"""Unit of Work — transaction boundary for database-backed code."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cached_property
from collections.abc import Callable, Iterator
from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db import SessionFactory
from brain.platform.db.repositories.agent_run import (
    AgentRunArtifactRepository,
    AgentRunEventRepository,
    AgentRunRepository,
)
from brain.platform.db.repositories.cycles import CycleRepository, CycleRunRepository
from brain.systems.user_domains.service import DomainService
from brain.platform.db.repositories.run import RunRepository
from brain.platform.db.repositories.ideas import (
    IdeaConnectionRepository,
    IdeaRepository,
    IdeaThreadRepository,
    UserMentionRepository,
)
from brain.platform.db.repositories.chat import (
    ChatConversationReadRepository,
    ChatConversationRepository,
    ChatMessageMentionRepository,
    ChatMessageRepository,
    ChatNotificationRepository,
)
from brain.platform.db.repositories.notifications import NotificationEventRepository
from brain.platform.db.repositories.memories import EdgeRepository, MemoryRepository
from brain.platform.db.repositories.memory_dag import MemorySummaryRepository
from brain.platform.db.repositories.memory_health import (
    MemoryHealthRepository,
    RetrievalPoolStatsRepository,
)
from brain.platform.db.repositories.narratives import NarrativeRepository
from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.repositories.skill_quality import SkillRunEvidenceRepository
from brain.platform.db.repositories.system import (
    ConsolidationRunRepository,
    DailyMetricsRepository,
    RetrievalLogRepository,
)
from brain.platform.db.repositories.team import (
    OrgApiKeyRepository,
    OrgRepository,
    TeamRepository,
    UserApiKeyRepository,
)
from brain.platform.db.repositories.scratchpad import ScratchpadRepository
from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
from brain.platform.db.repositories.vault import (
    VaultAccessLogRepository,
    VaultRepository,
    VaultShareRepository,
)

RepoT = TypeVar("RepoT")
ReturnT = TypeVar("ReturnT")

_sync_session_override: ContextVar[Session | None] = ContextVar(
    "unit_of_work_sync_session_override",
    default=None,
)


def _running_async_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@contextmanager
def use_sync_session(session: Session) -> Iterator[None]:
    """Bind nested sync UnitOfWork blocks to an AsyncSession.run_sync session."""

    token = _sync_session_override.set(session)
    try:
        yield
    finally:
        _sync_session_override.reset(token)


async def run_sync_with_unit_of_work(fn: Callable[..., ReturnT], /, *args: Any, **kwargs: Any) -> ReturnT:
    """Run a legacy-shaped service function inside an async UnitOfWork."""

    async with UnitOfWork() as uow:
        def _invoke(sync_session: Session) -> ReturnT:
            with use_sync_session(sync_session):
                return fn(*args, **kwargs)

        return await uow.session.run_sync(_invoke)


class AsyncRepositoryProxy(Generic[RepoT]):
    """Expose sync repository methods as awaitable async operations.

    Repository classes remain focused on domain queries. The proxy runs each
    call inside ``AsyncSession.run_sync`` so the SQLAlchemy sync ORM code is
    executed in SQLAlchemy's greenlet bridge against the asyncpg connection.
    """

    def __init__(self, session: AsyncSession, repo_cls: type[RepoT]) -> None:
        self._session = session
        self._repo_cls = repo_cls

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._repo_cls, name, None)
        if not callable(attr):
            raise AttributeError(name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            def _invoke(sync_session: Session) -> Any:
                repo = self._repo_cls(sync_session)
                return getattr(repo, name)(*args, **kwargs)

            return await self._session.run_sync(_invoke)

        return _call


class UnitOfWork:
    """All repos share one session.

    New runtime code should use ``async with UnitOfWork()``. Synchronous context
    manager support remains for older CLI/test paths while they are migrated.
    """

    def __init__(self) -> None:
        self._session: Session | None = None
        self._async_session: AsyncSession | None = None
        self._external_session = False

    async def __aenter__(self) -> UnitOfWork:
        self._async_session = SessionFactory()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._async_session is not None
        if exc_type:
            await self._async_session.rollback()
        else:
            await self._async_session.commit()
        await self._async_session.close()
        self._async_session = None
        self._clear_cached_repositories()

    def __enter__(self) -> UnitOfWork:
        override = _sync_session_override.get()
        if override is not None:
            self._session = override
            self._external_session = True
            return self

        if _running_async_loop():
            raise RuntimeError(
                "Synchronous UnitOfWork cannot open the legacy DB engine inside async runtime. "
                "Use `async with UnitOfWork()` or wrap sync code with `run_sync_with_unit_of_work()`."
            )

        from brain.platform.db.legacy import legacy_session_factory

        self._session = legacy_session_factory()
        self._external_session = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._session is not None
        if self._external_session:
            if exc_type is None:
                self._session.flush()
        elif exc_type:
            self._session.rollback()
        else:
            self._session.commit()
        if not self._external_session:
            self._session.close()
        self._session = None
        self._external_session = False
        self._clear_cached_repositories()

    def _clear_cached_repositories(self) -> None:
        for attr in list(self.__dict__):
            if isinstance(getattr(type(self), attr, None), cached_property):
                del self.__dict__[attr]

    @property
    def session(self) -> Session | AsyncSession:
        session = self._session or self._async_session
        assert session is not None, "UnitOfWork not entered"
        return session

    def _repo(self, repo_cls: type[RepoT]) -> RepoT | AsyncRepositoryProxy[RepoT]:
        if self._async_session is not None:
            return AsyncRepositoryProxy(self._async_session, repo_cls)
        assert self._session is not None, "UnitOfWork not entered"
        return repo_cls(self._session)

    def commit(self):
        if self._async_session is not None:
            return self._async_session.commit()
        assert self._session is not None, "UnitOfWork not entered"
        return self._session.commit()

    def rollback(self):
        if self._async_session is not None:
            return self._async_session.rollback()
        assert self._session is not None, "UnitOfWork not entered"
        return self._session.rollback()

    @cached_property
    def skills(self):
        return self._repo(SkillRepository)

    @cached_property
    def skill_bundles(self):
        return self._repo(SkillBundleRepository)

    @cached_property
    def skill_run_evidence(self):
        return self._repo(SkillRunEvidenceRepository)

    @cached_property
    def ideas(self):
        return self._repo(IdeaRepository)

    @cached_property
    def idea_threads(self):
        return self._repo(IdeaThreadRepository)

    @cached_property
    def idea_connections(self):
        return self._repo(IdeaConnectionRepository)

    @cached_property
    def user_mentions(self):
        return self._repo(UserMentionRepository)

    @cached_property
    def memories(self):
        return self._repo(MemoryRepository)

    @cached_property
    def edges(self):
        return self._repo(EdgeRepository)

    @cached_property
    def vault(self):
        return self._repo(VaultRepository)

    @cached_property
    def vault_shares(self):
        return self._repo(VaultShareRepository)

    @cached_property
    def vault_access_log(self):
        return self._repo(VaultAccessLogRepository)

    def daily_metrics(self):
        return self._repo(DailyMetricsRepository)

    @cached_property
    def consolidation_runs(self):
        return self._repo(ConsolidationRunRepository)

    @cached_property
    def retrieval_logs(self):
        return self._repo(RetrievalLogRepository)

    @cached_property
    def orgs(self):
        return self._repo(OrgRepository)

    @cached_property
    def team(self):
        return self._repo(TeamRepository)

    @cached_property
    def user_api_keys(self):
        return self._repo(UserApiKeyRepository)

    @cached_property
    def org_api_keys(self):
        return self._repo(OrgApiKeyRepository)

    @cached_property
    def run(self):
        return self._repo(RunRepository)

    @cached_property
    def agent_runs(self):
        return self._repo(AgentRunRepository)

    @cached_property
    def agent_run_events(self):
        return self._repo(AgentRunEventRepository)

    @cached_property
    def agent_run_artifacts(self):
        return self._repo(AgentRunArtifactRepository)

    @cached_property
    def cycles(self):
        return self._repo(CycleRepository)

    @cached_property
    def domains(self) -> DomainService:
        if self._async_session is not None:
            return AsyncRepositoryProxy(self._async_session, DomainService)
        assert self._session is not None, "UnitOfWork not entered"
        return DomainService(self.session)

    @cached_property
    def cycle_runs(self):
        return self._repo(CycleRunRepository)

    @cached_property
    def memory_summaries(self):
        return self._repo(MemorySummaryRepository)

    @cached_property
    def narratives(self):
        return self._repo(NarrativeRepository)

    @cached_property
    def memory_health(self):
        return self._repo(MemoryHealthRepository)

    @cached_property
    def pool_stats(self):
        return self._repo(RetrievalPoolStatsRepository)

    @cached_property
    def scratchpad(self):
        return self._repo(ScratchpadRepository)

    def chat_conversations(self):
        return self._repo(ChatConversationRepository)

    @cached_property
    def chat_messages(self):
        return self._repo(ChatMessageRepository)

    @cached_property
    def chat_mentions(self):
        return self._repo(ChatMessageMentionRepository)

    @cached_property
    def chat_notifications(self):
        return self._repo(ChatNotificationRepository)

    @cached_property
    def chat_reads(self):
        return self._repo(ChatConversationReadRepository)

    def notifications(self):
        return self._repo(NotificationEventRepository)
