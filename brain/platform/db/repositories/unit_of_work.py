"""Unit of Work — transaction boundary for non-Flask code."""
from __future__ import annotations

from functools import cached_property

from sqlalchemy.orm import Session

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


class UnitOfWork:
    """All repos share one session. Auto-commits on success, rollback on error."""

    def __init__(self) -> None:
        self._session: Session | None = None

    def __enter__(self) -> UnitOfWork:
        self._session = SessionFactory()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._session is not None
        if exc_type:
            self._session.rollback()
        else:
            self._session.commit()
        self._session.close()
        self._session = None
        for attr in list(self.__dict__):
            if isinstance(getattr(type(self), attr, None), cached_property):
                del self.__dict__[attr]

    @property
    def session(self) -> Session:
        assert self._session is not None, "UnitOfWork not entered"
        return self._session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    @cached_property
    def skills(self) -> SkillRepository:
        return SkillRepository(self.session)

    @cached_property
    def skill_bundles(self) -> SkillBundleRepository:
        return SkillBundleRepository(self.session)

    @cached_property
    def skill_run_evidence(self) -> SkillRunEvidenceRepository:
        return SkillRunEvidenceRepository(self.session)

    @cached_property
    def ideas(self) -> IdeaRepository:
        return IdeaRepository(self.session)

    @cached_property
    def idea_threads(self) -> IdeaThreadRepository:
        return IdeaThreadRepository(self.session)

    @cached_property
    def idea_connections(self) -> IdeaConnectionRepository:
        return IdeaConnectionRepository(self.session)

    @cached_property
    def user_mentions(self) -> UserMentionRepository:
        return UserMentionRepository(self.session)

    @cached_property
    def memories(self) -> MemoryRepository:
        return MemoryRepository(self.session)

    @cached_property
    def edges(self) -> EdgeRepository:
        return EdgeRepository(self.session)

    @cached_property
    def vault(self) -> VaultRepository:
        return VaultRepository(self.session)

    @cached_property
    def vault_shares(self) -> VaultShareRepository:
        return VaultShareRepository(self.session)

    @cached_property
    def vault_access_log(self) -> VaultAccessLogRepository:
        return VaultAccessLogRepository(self.session)

    @cached_property
    def daily_metrics(self) -> DailyMetricsRepository:
        return DailyMetricsRepository(self.session)

    @cached_property
    def consolidation_runs(self) -> ConsolidationRunRepository:
        return ConsolidationRunRepository(self.session)

    @cached_property
    def retrieval_logs(self) -> RetrievalLogRepository:
        return RetrievalLogRepository(self.session)

    @cached_property
    def orgs(self) -> OrgRepository:
        return OrgRepository(self.session)

    @cached_property
    def team(self) -> TeamRepository:
        return TeamRepository(self.session)

    @cached_property
    def user_api_keys(self) -> UserApiKeyRepository:
        return UserApiKeyRepository(self.session)

    @cached_property
    def org_api_keys(self) -> OrgApiKeyRepository:
        return OrgApiKeyRepository(self.session)

    @cached_property
    def run(self) -> RunRepository:
        return RunRepository(self.session)

    @cached_property
    def agent_runs(self) -> AgentRunRepository:
        return AgentRunRepository(self.session)

    @cached_property
    def agent_run_events(self) -> AgentRunEventRepository:
        return AgentRunEventRepository(self.session)

    @cached_property
    def agent_run_artifacts(self) -> AgentRunArtifactRepository:
        return AgentRunArtifactRepository(self.session)

    @cached_property
    @cached_property
    def cycles(self) -> CycleRepository:
        return CycleRepository(self.session)

    @cached_property
    def domains(self) -> DomainService:
        return DomainService(self.session)

    @cached_property
    def cycle_runs(self) -> CycleRunRepository:
        return CycleRunRepository(self.session)

    @cached_property
    def memory_summaries(self) -> MemorySummaryRepository:
        return MemorySummaryRepository(self.session)

    @cached_property
    def narratives(self) -> NarrativeRepository:
        return NarrativeRepository(self.session)

    @cached_property
    def memory_health(self) -> MemoryHealthRepository:
        return MemoryHealthRepository(self.session)

    @cached_property
    def pool_stats(self) -> RetrievalPoolStatsRepository:
        return RetrievalPoolStatsRepository(self.session)

    @cached_property
    def scratchpad(self) -> ScratchpadRepository:
        return ScratchpadRepository(self.session)

    def chat_conversations(self) -> ChatConversationRepository:
        return ChatConversationRepository(self.session)

    @cached_property
    def chat_messages(self) -> ChatMessageRepository:
        return ChatMessageRepository(self.session)

    @cached_property
    def chat_mentions(self) -> ChatMessageMentionRepository:
        return ChatMessageMentionRepository(self.session)

    @cached_property
    def chat_notifications(self) -> ChatNotificationRepository:
        return ChatNotificationRepository(self.session)

    @cached_property
    def chat_reads(self) -> ChatConversationReadRepository:
        return ChatConversationReadRepository(self.session)

    @cached_property
    def notifications(self) -> NotificationEventRepository:
        return NotificationEventRepository(self.session)
