"""SkillRepository - domain queries for the skills table."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import load_only, selectinload

from brain.platform.db.models.skill import Skill, SkillExecution, SkillVersion
from brain.platform.db.models.skill_bundle import SkillInstallation
from brain.platform.db.repositories.base import BaseRepository


def _not_archived():
    """Return a filter clause for non-archived skills.

    Uses coalesce-style OR so it works on both PostgreSQL (where server_default
    sets archived=false) and SQLite tests (where the column may be NULL, 0, or
    the boolean false).
    """
    return or_(Skill.archived == False, Skill.archived.is_(None))  # noqa: E712


_SKILL_READ_COLUMNS = (
    Skill.id,
    Skill.name,
    Skill.description,
    Skill.procedure,
    Skill.version,
    Skill.level,
    Skill.skill_type,
    Skill.maturity,
    Skill.confidence,
    Skill.use_count,
    Skill.success_count,
    Skill.failure_count,
    Skill.partial_count,
    Skill.avg_duration_sec,
    Skill.last_used,
    Skill.pitfalls,
    Skill.refinements,
    Skill.triggers,
    Skill.guardrails,
    Skill.graduated_steps,
    Skill.auto_emerged,
    Skill.builtin,
    Skill.archived,
    Skill.thinking_tier,
    Skill.skill_installation_id,
    Skill.bundle_version_id,
    Skill.bundle_digest,
    Skill.overlay_revision,
    Skill.effective_digest,
    Skill.source_kind,
    Skill.trust_level,
)

_SKILL_EXECUTION_READ_COLUMNS = (
    SkillExecution.id,
    SkillExecution.task_description,
    SkillExecution.outcome,
    SkillExecution.duration_sec,
    SkillExecution.started_at,
    SkillExecution.rework_rounds,
    SkillExecution.flagged,
)

_SKILL_COMMAND_COLUMNS = (
    Skill.name,
    Skill.description,
    Skill.maturity,
    Skill.use_count,
    Skill.success_count,
)

_SKILL_LIST_COLUMNS = (
    Skill.id,
    Skill.name,
    Skill.version,
    Skill.archived,
)


def _active_skill_stmt(*, with_executions: bool = False):
    options = [load_only(*_SKILL_READ_COLUMNS)]
    if with_executions:
        options.append(
            selectinload(Skill.executions).load_only(*_SKILL_EXECUTION_READ_COLUMNS)
        )
    return (
        select(Skill)
        .where(_not_archived())
        .order_by(Skill.use_count.desc())
        .options(*options)
    )


def _visible_installation(*, org_id: str, user_id: str):
    return exists(
        select(SkillInstallation.id).where(
            SkillInstallation.skill_id == Skill.id,
            SkillInstallation.enabled.is_(True),
            SkillInstallation.review_status == "approved",
            or_(
                SkillInstallation.archived == False,  # noqa: E712
                SkillInstallation.archived.is_(None),
            ),
            or_(
                and_(
                    SkillInstallation.enabled_scope == "system",
                    SkillInstallation.org_id.is_(None),
                    SkillInstallation.user_id.is_(None),
                ),
                and_(
                    SkillInstallation.org_id == org_id,
                    or_(
                        SkillInstallation.user_id.is_(None),
                        SkillInstallation.user_id == user_id,
                    ),
                ),
            ),
        )
    )


class SkillRepository(BaseRepository[Skill]):
    """CRUD + domain queries for Skill."""

    model = Skill

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def a_list_active(self) -> Sequence[Skill]:
        """Return non-archived skills ordered by use_count descending."""
        return (await self._session.scalars(_active_skill_stmt())).all()

    async def a_list_command_summaries(self) -> Sequence:
        """Return the skinny skill fields used by slash-command suggestions."""
        stmt = (
            select(*_SKILL_COMMAND_COLUMNS)
            .where(_not_archived())
            .order_by(Skill.use_count.desc())
        )
        return (await self._session.execute(stmt)).all()

    async def a_list_active_with_executions(self) -> Sequence[Skill]:
        """Return non-archived skills with executions eagerly loaded."""
        return (await self._session.scalars(_active_skill_stmt(with_executions=True))).all()

    async def a_list_visible(self, *, org_id: str, user_id: str) -> Sequence[Skill]:
        """Return active skills installed for the user or system."""
        stmt = (
            select(Skill)
            .where(
                _not_archived(),
                _visible_installation(org_id=org_id, user_id=user_id),
            )
            .order_by(Skill.name, Skill.id)
            .options(load_only(*_SKILL_LIST_COLUMNS))
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_active_for_dashboard(self) -> Sequence[Skill]:
        """Return dashboard skill rows without historical execution payloads."""
        return (await self._session.scalars(_active_skill_stmt())).all()

    async def a_overview_summary(self, *, limit: int = 10) -> tuple[list[dict[str, Any]], int, int]:
        """Return the lightweight skill totals used by the system overview."""
        active_filter = _not_archived()
        rows = (
            await self._session.execute(
                select(Skill.name, Skill.maturity, Skill.use_count)
                .where(active_filter)
                .order_by(Skill.use_count.desc())
                .limit(limit)
            )
        ).all()
        totals = (
            await self._session.execute(
                select(
                    func.count(Skill.id).label("skills"),
                    func.coalesce(func.sum(Skill.use_count), 0).label("executions"),
                ).where(active_filter)
            )
        ).first()
        summary = [
            {"name": row.name, "maturity": row.maturity, "use_count": int(row.use_count or 0)}
            for row in rows
        ]
        skill_count = int(totals.skills or 0) if totals else 0
        executions = int(totals.executions or 0) if totals else 0
        return summary, skill_count, executions

    # ------------------------------------------------------------------
    # Lookup by name
    # ------------------------------------------------------------------

    async def a_get_by_name(self, name: str) -> Skill | None:
        """Return active skill by name, or None."""
        stmt = select(Skill).where(Skill.name == name, _not_archived())
        return (await self._session.scalars(stmt)).first()

    async def a_get_by_name_or_raise(self, name: str) -> Skill:
        """Return active skill by name, or raise LookupError."""
        skill = await self.a_get_by_name(name)
        if skill is None:
            raise LookupError(f"Skill '{name}' not found")
        return skill

    async def a_get_visible(
        self,
        *,
        org_id: str,
        user_id: str,
        skill_id: int | None = None,
        name: str | None = None,
    ) -> Skill | None:
        """Return one active skill installed for the user or system."""
        stmt = select(Skill).where(
            _not_archived(),
            _visible_installation(org_id=org_id, user_id=user_id),
        )
        if skill_id is not None:
            stmt = stmt.where(Skill.id == skill_id)
        if name is not None:
            stmt = stmt.where(Skill.name == name)
        return (await self._session.scalars(stmt.options(load_only(*_SKILL_READ_COLUMNS)))).first()

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    async def a_update_thinking(
        self,
        skill_id: int,
        thinking_tier: str | None = None,
    ) -> Skill:
        """Update skill reasoning effort, flush, return skill."""
        skill = await self.a_get_or_raise(skill_id)
        if thinking_tier is not None:
            skill.thinking_tier = thinking_tier
        await self._session.flush()
        return skill

    _UPDATABLE_FIELDS = {
        "name",
        "description",
        "procedure",
        "thinking_tier",
        "pitfalls",
        "refinements",
        "triggers",
        "guardrails",
    }

    async def a_update_full(self, skill_id: int, **fields) -> Skill:
        """Update allowed fields. Bumps version if procedure changes."""
        unexpected = set(fields) - self._UPDATABLE_FIELDS
        if unexpected:
            raise ValueError(f"Cannot update fields: {unexpected}")
        skill = await self.a_get_or_raise(skill_id)
        new_procedure = fields.get("procedure")
        if new_procedure is not None and new_procedure != skill.procedure:
            skill.version = (skill.version or 1) + 1
        for key, value in fields.items():
            setattr(skill, key, value)
        if fields and skill.builtin:
            skill.builtin = False
        await self._session.flush()
        return skill

    # ------------------------------------------------------------------
    # JSONB mutations
    # ------------------------------------------------------------------

    async def a_add_guardrail(self, name: str, text: str, severity: str) -> Skill:
        """Append a guardrail dict to skill.guardrails, flush, return skill."""
        skill = await self.a_get_by_name_or_raise(name)
        guardrails = list(skill.guardrails or [])
        guardrails.append({"text": text, "severity": severity})
        skill.guardrails = guardrails
        await self._session.flush()
        return skill

    async def a_add_trigger(self, name: str, direction: str, pattern: str) -> Skill:
        """Append a trigger dict to skill.triggers, flush, return skill."""
        skill = await self.a_get_by_name_or_raise(name)
        triggers = list(skill.triggers or [])
        triggers.append({"direction": direction, "pattern": pattern})
        skill.triggers = triggers
        await self._session.flush()
        return skill

    async def a_remove_trigger(self, name: str, index: int) -> Skill:
        """Remove trigger at index from skill.triggers, flush, return skill."""
        skill = await self.a_get_by_name_or_raise(name)
        triggers = list(skill.triggers or [])
        if index < 0 or index >= len(triggers):
            raise ValueError(
                f"Invalid trigger index {index} for skill '{name}' "
                f"(has {len(triggers)} triggers)"
            )
        triggers.pop(index)
        skill.triggers = triggers
        await self._session.flush()
        return skill

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def a_archive(self, skill_id: int) -> Skill:
        """Soft-delete a skill by setting archived=True, flush."""
        skill = await self.a_get_or_raise(skill_id)
        skill.archived = True
        await self._session.flush()
        return skill

    # ------------------------------------------------------------------
    # Attention / health
    # ------------------------------------------------------------------

    async def a_needing_attention(self) -> Sequence[Skill]:
        """Return active skills with low confidence OR high failure count."""
        stmt = select(Skill).where(
            _not_archived(),
            or_(Skill.confidence < 0.6, Skill.failure_count > 2),
        )
        return (await self._session.scalars(stmt)).all()

    # ------------------------------------------------------------------
    # Related tables
    # ------------------------------------------------------------------

    async def a_get_sparkline(self, name: str, limit: int = 30) -> Sequence[SkillExecution]:
        """Return the most recent executions for a skill (for sparkline charts)."""
        skill = await self.a_get_by_name_or_raise(name)
        stmt = (
            select(SkillExecution)
            .where(SkillExecution.skill_id == skill.id)
            .order_by(SkillExecution.id.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_get_versions(self, name: str) -> Sequence[SkillVersion]:
        """Return version history for a skill, newest first."""
        skill = await self.a_get_by_name_or_raise(name)
        stmt = (
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.version.desc())
        )
        return (await self._session.scalars(stmt)).all()
