"""Unit of Work — transaction boundary for database-backed code."""
from __future__ import annotations

from contextlib import suppress
from functools import cached_property
import inspect
import logging
from typing import Any, Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db import SessionFactory
from brain.platform.db.repositories.agent_run import (
    AgentRunArtifactRepository,
    AgentRunEventRepository,
    AgentRunRepository,
)
from brain.platform.db.repositories.cycles import CycleRepository, CycleRunRepository
from brain.systems.user_domains.service import AsyncDomainService
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
from brain.platform.db.repositories.external_agents import (
    ExternalAgentConnectionRepository,
    ExternalAgentConnectionTokenRepository,
    ExternalAgentTaskArtifactRepository,
    ExternalAgentTaskEventRepository,
    ExternalAgentTaskRepository,
)
from brain.platform.db.repositories.notifications import NotificationEventRepository
from brain.platform.db.repositories.reconstructive_memory import (
    MemoryAssertionRepository,
    MemoryEdgeRepository,
    MemoryNodeRepository,
    MemorySourceRepository,
    ReconstructiveEdgeCompatibilityRepository,
    ReconstructiveMemoryCompatibilityRepository,
    ReconstructionRepository,
)
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
)
from brain.platform.db.repositories.scratchpad import ScratchpadRepository
from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
from brain.platform.db.repositories.vault import (
    VaultAccessLogRepository,
    VaultRepository,
)

RepoT = TypeVar("RepoT")
logger = logging.getLogger(__name__)


def _method_owner(repo_cls: type[Any], name: str) -> type[Any] | None:
    for owner in repo_cls.__mro__:
        value = owner.__dict__.get(name)
        if callable(value):
            return owner
    return None


class AsyncRepositoryProxy(Generic[RepoT]):
    """Expose repository methods as awaitable async operations.

    Repositories expose native async methods. While callers migrate, ``foo``
    resolves to explicit ``a_foo`` methods without keeping sync DB bodies alive.
    """

    def __init__(self, session: AsyncSession, repo_cls: type[RepoT]) -> None:
        self._session = session
        self._repo_cls = repo_cls

    def __getattr__(self, name: str) -> Any:
        repo = self._repo_cls(self._session)
        method = getattr(repo, name, None)
        if method is not None and inspect.iscoroutinefunction(method):
            return method

        owner = _method_owner(self._repo_cls, name)
        async_owner = _method_owner(self._repo_cls, f"a_{name}")
        if owner is not None and async_owner is not owner:
            raise AttributeError(
                f"{self._repo_cls.__name__}.{name} has no native async implementation; "
                f"add async {self._repo_cls.__name__}.{name} or "
                f"{self._repo_cls.__name__}.a_{name}."
            )

        async_method = getattr(repo, f"a_{name}", None)
        if async_method is not None and inspect.iscoroutinefunction(async_method):
            return async_method

        raise AttributeError(
            f"{self._repo_cls.__name__}.{name} has no native async implementation; "
            f"add async {self._repo_cls.__name__}.{name} or "
            f"{self._repo_cls.__name__}.a_{name}."
        )


class UnitOfWork:
    """All repos share one session.

    Runtime code should use ``async with UnitOfWork()``.
    """

    def __init__(self) -> None:
        self._async_session: AsyncSession | None = None

    @classmethod
    def blocking(cls) -> UnitOfWork:
        raise RuntimeError("UnitOfWork is async-only; use `async with UnitOfWork()`.")

    async def __aenter__(self) -> UnitOfWork:
        self._async_session = SessionFactory()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._async_session is not None
        exit_failed = exc_type is not None
        try:
            if exc_type:
                try:
                    await self._async_session.rollback()
                except BaseException:
                    logger.exception("unit_of_work_rollback_failed")
            else:
                try:
                    await self._async_session.commit()
                except BaseException:
                    exit_failed = True
                    with suppress(BaseException):
                        await self._async_session.rollback()
                    raise
        finally:
            try:
                await self._async_session.close()
            except BaseException:
                if not exit_failed:
                    raise
                logger.exception("unit_of_work_close_failed_after_primary_error")
            finally:
                self._async_session = None
                self._clear_cached_repositories()

    def __enter__(self) -> UnitOfWork:
        raise RuntimeError("UnitOfWork is async-only; use `async with UnitOfWork()`.")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def _clear_cached_repositories(self) -> None:
        for attr in list(self.__dict__):
            if isinstance(getattr(type(self), attr, None), cached_property):
                del self.__dict__[attr]

    @property
    def session(self) -> AsyncSession:
        assert self._async_session is not None, "UnitOfWork not entered"
        return self._async_session

    def _repo(self, repo_cls: type[RepoT]) -> RepoT | AsyncRepositoryProxy[RepoT]:
        if self._async_session is not None:
            return AsyncRepositoryProxy(self._async_session, repo_cls)
        raise AssertionError("UnitOfWork not entered")

    async def commit(self):
        assert self._async_session is not None, "UnitOfWork not entered"
        await self._async_session.commit()

    async def rollback(self):
        assert self._async_session is not None, "UnitOfWork not entered"
        await self._async_session.rollback()

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
        return self._repo(ReconstructiveMemoryCompatibilityRepository)

    @cached_property
    def edges(self):
        return self._repo(ReconstructiveEdgeCompatibilityRepository)

    @cached_property
    def vault(self):
        return self._repo(VaultRepository)

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
    def domains(self) -> AsyncDomainService:
        if self._async_session is not None:
            return AsyncDomainService(self._async_session)
        raise AssertionError("UnitOfWork not entered")

    @cached_property
    def cycle_runs(self):
        return self._repo(CycleRunRepository)

    @cached_property
    def memory_sources(self):
        return self._repo(MemorySourceRepository)

    @cached_property
    def memory_nodes(self):
        return self._repo(MemoryNodeRepository)

    @cached_property
    def memory_edges_new(self):
        return self._repo(MemoryEdgeRepository)

    @cached_property
    def memory_assertions(self):
        return self._repo(MemoryAssertionRepository)

    @cached_property
    def reconstruction_runs(self):
        return self._repo(ReconstructionRepository)

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

    @cached_property
    def notifications(self):
        return self._repo(NotificationEventRepository)

    @cached_property
    def external_agent_connections(self):
        return self._repo(ExternalAgentConnectionRepository)

    @cached_property
    def external_agent_connection_tokens(self):
        return self._repo(ExternalAgentConnectionTokenRepository)

    @cached_property
    def external_agent_tasks(self):
        return self._repo(ExternalAgentTaskRepository)

    @cached_property
    def external_agent_task_events(self):
        return self._repo(ExternalAgentTaskEventRepository)

    @cached_property
    def external_agent_task_artifacts(self):
        return self._repo(ExternalAgentTaskArtifactRepository)
