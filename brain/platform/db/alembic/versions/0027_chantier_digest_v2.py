"""Seed the chantier-primary coordinator digest contract v2.

Revision ID: 0027_chantier_digest_v2
Revises: 0026_chantier_object_type, 0026_packet_brief_deliveries
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Mapping

from alembic import op
import sqlalchemy as sa


revision = "0027_chantier_digest_v2"
down_revision = (
    "0026_chantier_object_type",
    "0026_packet_brief_deliveries",
)
branch_labels = None
depends_on = None


_COORDINATOR_CYCLE_ID = 2
_COORDINATOR_CYCLE_NAME = "Uwear Ticket Coordinator Check-ins"
_MISSION_CONTRACT_MARKER = "Chantier-primary digest contract v2:"
_MISSION_CONTRACT = (
    "Chantier-primary digest contract v2: Load Enterprise Documentation Domain 37 "
    "record 1155 and the chantier-operations playbook record 1274 before each digest "
    "or new-work filing. This v2 block supersedes any earlier owner-primary digest "
    "shape in this mission. Keep exact issue, PR, repo, and active tracker counts and "
    "add the exact active-chantier count. Give each materially moving chantier its own "
    "section with state, one goal-progress line, movement since the last digest, next "
    "step, blockers, and owners; roll quiet chantiers into one line; keep ungrouped "
    "tickets in Loose items. End every digest with a Per-person recap footer naming "
    "Reda, Axel, and JB: top next action, or the existing exact-assignee/GitHub-issue/"
    "authored-PR/builder-candidate empty checks plus a rebalancing recommendation. "
    "In Phase B, persist chantier state, member refs, blockers, and next step alongside "
    "the existing per-person snapshot items; a chantier may not depart silently. "
    "Material chantier movement means state change, member gain/loss, or blocker hit/"
    "clear. Before filing, match active chantiers by refs/external ids and title/root "
    "cause; attach exact matches and only propose, never auto-create, a chantier for "
    "an ungrouped family. Must-surface every active chantier untouched 3+ days, missing "
    "next_step, or blocked. When deploy-verified member states meet the goal, propose "
    "close-out with an outcome summary in goal language, not PR counts."
)
_REVISION_RATIONALE = (
    "Seed chantier-primary digest v2, attach-at-triage, freshness, and close-out rules."
)


def _schema(bind: sa.Connection) -> str | None:
    return "public" if bind.dialect.name == "postgresql" else None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in set(sa.inspect(bind).get_table_names(schema=_schema(bind)))


def _table(bind: sa.Connection, metadata: sa.MetaData, table_name: str) -> sa.Table:
    return sa.Table(
        table_name,
        metadata,
        schema=_schema(bind),
        autoload_with=bind,
    )


def _mission_prompt(prompt: str) -> str:
    clean_prompt = str(prompt or "").strip()
    if _MISSION_CONTRACT_MARKER in clean_prompt:
        return clean_prompt
    if not clean_prompt:
        return _MISSION_CONTRACT
    # Put v2 first so it stays visible even when the launch envelope must trim
    # the tail of a near-12K legacy mission.
    return f"{_MISSION_CONTRACT}\n\n{clean_prompt}"


def _record_cycle_revision(
    bind: sa.Connection,
    revisions: sa.Table,
    cycle: Mapping[str, object],
    prompt: str,
) -> None:
    latest = bind.execute(
        sa.select(revisions)
        .where(revisions.c.cycle_id == cycle["id"])
        .order_by(
            revisions.c.revision_number.desc(),
            revisions.c.id.desc(),
        )
        .limit(1)
    ).mappings().first()
    if latest is not None and latest["prompt"] == prompt:
        return

    bind.execute(
        revisions.insert().values(
            cycle_id=cycle["id"],
            revision_number=(int(latest["revision_number"]) + 1 if latest else 1),
            source_type="system",
            source_id=None,
            rationale=_REVISION_RATIONALE,
            name=cycle["name"],
            prompt=prompt,
            schedule_expr=cycle["schedule_expr"],
            timezone=cycle["timezone"],
            enabled=cycle["enabled"],
            model_override=cycle["model_override"],
            thinking_override=cycle["thinking_override"],
            target_idea_id=cycle["target_idea_id"],
            context_policy=(
                latest["context_policy"]
                if latest is not None and latest["context_policy"]
                else {}
            ),
        )
    )


def _upgrade(bind: sa.Connection) -> None:
    if not _table_exists(bind, "cycles"):
        return

    metadata = sa.MetaData()
    cycles = _table(bind, metadata, "cycles")
    revisions = (
        _table(bind, metadata, "cycle_revisions")
        if _table_exists(bind, "cycle_revisions")
        else None
    )
    cycle = bind.execute(
        sa.select(
            cycles.c.id,
            cycles.c.name,
            cycles.c.prompt,
            cycles.c.schedule_expr,
            cycles.c.timezone,
            cycles.c.enabled,
            cycles.c.model_override,
            cycles.c.thinking_override,
            cycles.c.target_idea_id,
        ).where(
            cycles.c.id == _COORDINATOR_CYCLE_ID,
            cycles.c.name == _COORDINATOR_CYCLE_NAME,
        )
    ).mappings().first()
    if cycle is None:
        return

    prompt = _mission_prompt(str(cycle["prompt"] or ""))
    if prompt != cycle["prompt"]:
        update_values: dict[str, object] = {"prompt": prompt}
        if "updated_at" in cycles.c:
            update_values["updated_at"] = sa.func.now()
        bind.execute(
            cycles.update()
            .where(cycles.c.id == cycle["id"])
            .values(**update_values)
        )
    if revisions is not None:
        _record_cycle_revision(bind, revisions, cycle, prompt)


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Cycle revisions are an append-only audit trail. Removing the seeded
    # contract would also make runs depend on whichever older mission happened
    # to be live, so preserve the reconciliation on downgrade.
    return None
