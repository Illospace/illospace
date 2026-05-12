"""Unit of Work — transaction boundary for database-backed code."""
from __future__ import annotations

import asyncio
from functools import cached_property
import threading
from collections.abc import Callable
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

async def run_unit_of_work_task(fn: Callable[..., ReturnT], /, *args: Any, **kwargs: Any) -> ReturnT:
    """Run a synchronous entrypoint on the async DB compatibility boundary."""

    return await asyncio.to_thread(fn, *args, **kwargs)


class _AsyncLoopWorker:
    """Owns one event loop for a blocking compatibility transaction."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name="blocking-async-uow", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    def run(self, awaitable):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(awaitable, self._loop).result()

    def call(self, fn: Callable[[], ReturnT]) -> ReturnT:
        async def _invoke() -> ReturnT:
            return fn()

        return self.run(_invoke())

    def close(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class BlockingAsyncQuery:
    """Small compatibility wrapper for legacy ``session.query`` call sites."""

    def __init__(self, session: BlockingAsyncSession, *entities: Any) -> None:
        from sqlalchemy import select

        self._session = session
        self._entities = entities
        self._stmt = select(*entities)

    def filter(self, *criteria: Any):
        self._stmt = self._stmt.where(*criteria)
        return self

    def where(self, *criteria: Any):
        return self.filter(*criteria)

    def join(self, *args: Any, **kwargs: Any):
        self._stmt = self._stmt.join(*args, **kwargs)
        return self

    def order_by(self, *clauses: Any):
        self._stmt = self._stmt.order_by(*clauses)
        return self

    def limit(self, value: int):
        self._stmt = self._stmt.limit(value)
        return self

    def with_for_update(self, **kwargs: Any):
        self._stmt = self._stmt.with_for_update(**kwargs)
        return self

    def _single_model_entity(self) -> bool:
        return len(self._entities) == 1 and hasattr(self._entities[0], "__mapper__")

    def all(self):
        result = self._session.execute(self._stmt)
        return result.scalars().all() if self._single_model_entity() else result.all()

    def first(self):
        result = self._session.execute(self._stmt.limit(1))
        return result.scalars().first() if self._single_model_entity() else result.first()

    def scalar(self):
        return self._session.scalar(self._stmt.limit(1))

    def one_or_none(self):
        result = self._session.execute(self._stmt.limit(2))
        return result.scalars().one_or_none() if self._single_model_entity() else result.one_or_none()


class BlockingAsyncSession:
    """Synchronous-looking adapter backed by an ``AsyncSession``.

    This is only for sync entrypoints such as CLIs and worker bootstrap code.
    Async request/runtime code should continue to use ``async with UnitOfWork()``.
    """

    def __init__(self, worker: _AsyncLoopWorker, session: AsyncSession) -> None:
        self._worker = worker
        self._session = session

    def scalars(self, *args: Any, **kwargs: Any):
        return self._worker.run(self._session.scalars(*args, **kwargs))

    def execute(self, *args: Any, **kwargs: Any):
        return self._worker.run(self._session.execute(*args, **kwargs))

    def scalar(self, *args: Any, **kwargs: Any):
        return self._worker.run(self._session.scalar(*args, **kwargs))

    def get(self, *args: Any, **kwargs: Any):
        return self._worker.run(self._session.get(*args, **kwargs))

    def add(self, obj: Any) -> None:
        self._worker.call(lambda: self._session.add(obj))

    def add_all(self, objects: list[Any]) -> None:
        self._worker.call(lambda: self._session.add_all(objects))

    def begin_nested(self):
        return _BlockingAsyncTransaction(self._worker, self._session.begin_nested())

    def commit(self) -> None:
        self._worker.run(self._session.commit())

    def delete(self, obj: Any) -> None:
        self._worker.run(self._session.delete(obj))

    def flush(self) -> None:
        self._worker.run(self._session.flush())

    def refresh(self, obj: Any) -> None:
        self._worker.run(self._session.refresh(obj))

    def expunge(self, obj: Any) -> None:
        self._worker.call(lambda: self._session.expunge(obj))

    def get_bind(self, *args: Any, **kwargs: Any):
        return self._worker.call(lambda: self._session.get_bind(*args, **kwargs))

    def rollback(self) -> None:
        self._worker.run(self._session.rollback())

    def close(self) -> None:
        self._worker.run(self._session.close())

    def query(self, *entities: Any) -> BlockingAsyncQuery:
        return BlockingAsyncQuery(self, *entities)

    def table_exists(self, table_name: str) -> bool:
        from sqlalchemy import inspect

        async def _exists() -> bool:
            connection = await self._session.connection()
            return await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(table_name)
            )

        return bool(self._worker.run(_exists()))


class _BlockingAsyncTransaction:
    def __init__(self, worker: _AsyncLoopWorker, transaction) -> None:
        self._worker = worker
        self._transaction = transaction

    def __enter__(self):
        return self._worker.run(self._transaction.__aenter__())

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._worker.run(self._transaction.__aexit__(exc_type, exc_val, exc_tb))


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

            return await getattr(self._session, "run_sync")(_invoke)

        return _call


class UnitOfWork:
    """All repos share one session.

    New runtime code should use ``async with UnitOfWork()``. Synchronous context
    manager support remains for older CLI/test paths while they are migrated.
    """

    def __init__(self, *, _blocking: bool = False) -> None:
        self._session: Session | None = None
        self._async_session: AsyncSession | None = None
        self._blocking_session: BlockingAsyncSession | None = None
        self._external_session = False
        self._blocking = _blocking
        self._worker: _AsyncLoopWorker | None = None

    @classmethod
    def blocking(cls) -> UnitOfWork:
        return cls(_blocking=True)

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
        if not self._blocking:
            raise RuntimeError("Use `async with UnitOfWork()` or `open_unit_of_work()`.")
        self._worker = _AsyncLoopWorker()

        async def _open() -> AsyncSession:
            return SessionFactory()

        self._async_session = self._worker.run(_open())
        self._blocking_session = BlockingAsyncSession(self._worker, self._async_session)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._worker is not None
        assert self._async_session is not None
        try:
            if exc_type:
                self._worker.run(self._async_session.rollback())
            else:
                self._worker.run(self._async_session.commit())
            self._worker.run(self._async_session.close())
        finally:
            self._async_session = None
            self._blocking_session = None
            self._external_session = False
            self._clear_cached_repositories()
            self._worker.close()
            self._worker = None

    def _clear_cached_repositories(self) -> None:
        for attr in list(self.__dict__):
            if isinstance(getattr(type(self), attr, None), cached_property):
                del self.__dict__[attr]

    @property
    def session(self) -> Session | AsyncSession | BlockingAsyncSession:
        session = self._blocking_session or self._session or self._async_session
        assert session is not None, "UnitOfWork not entered"
        return session

    def _repo(self, repo_cls: type[RepoT]) -> RepoT | AsyncRepositoryProxy[RepoT]:
        if self._blocking_session is not None:
            return repo_cls(self._blocking_session)  # type: ignore[arg-type]
        if self._async_session is not None:
            return AsyncRepositoryProxy(self._async_session, repo_cls)
        assert self._session is not None, "UnitOfWork not entered"
        return repo_cls(self._session)

    def commit(self):
        if self._blocking_session is not None:
            return self._blocking_session._worker.run(self._blocking_session._session.commit())
        if self._async_session is not None:
            return self._async_session.commit()
        assert self._session is not None, "UnitOfWork not entered"
        return self._session.commit()

    def rollback(self):
        if self._blocking_session is not None:
            return self._blocking_session.rollback()
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
        if self._blocking_session is not None:
            return DomainService(self.session)
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


def open_unit_of_work(factory: Callable[[], UnitOfWork] = UnitOfWork) -> UnitOfWork:
    """Open the centralized compatibility UOW for sync entrypoints.

    Production code should prefer ``async with UnitOfWork()``. This helper keeps
    legacy CLI/job boundaries explicit while still routing I/O through the async
    SQLAlchemy engine instead of the retired sync engine.
    """

    try:
        return factory(_blocking=True)  # type: ignore[call-arg]
    except TypeError:
        return factory()
