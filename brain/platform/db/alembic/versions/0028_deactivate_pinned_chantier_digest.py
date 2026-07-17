"""Deactivate the coordinator contract that pinned a chantier playbook record id.

Revision ID: 0028_deactivate_pinned_chantier_digest
Revises: 0027_chantier_digest_v2
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Mapping
import logging

from alembic import op
import sqlalchemy as sa


revision = "0028_deactivate_pinned_chantier_digest"
down_revision = "0027_chantier_digest_v2"
branch_labels = None
depends_on = None


_LOGGER = logging.getLogger(__name__)
_COORDINATOR_CYCLE_ID = 2
_COORDINATOR_CYCLE_NAME = "Uwear Ticket Coordinator Check-ins"
_MISSION_CONTRACT_MARKER = "Chantier-primary digest contract v2:"
_INVALID_PLAYBOOK_REFERENCE = "record 1274"
_SAFE_FALLBACK_MISSION = "Run the existing owner-primary coordinator mission."
_REVISION_RATIONALE = (
    "Deactivate the pinned-record chantier digest contract until slug-based activation."
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


def _without_pinned_contract(prompt: str) -> str | None:
    clean_prompt = str(prompt or "").strip()
    first_block, separator, remainder = clean_prompt.partition("\n\n")
    if not (
        first_block.startswith(_MISSION_CONTRACT_MARKER)
        and _INVALID_PLAYBOOK_REFERENCE in first_block
    ):
        return None
    if separator and remainder.strip():
        return remainder.strip()
    return _SAFE_FALLBACK_MISSION


def _core_doc_is_active(bind: sa.Connection) -> bool:
    if not _table_exists(bind, "domain_records"):
        return False
    metadata = sa.MetaData()
    records = _table(bind, metadata, "domain_records")
    required_columns = {"id", "domain_id", "version", "data"}
    if not required_columns.issubset(records.c.keys()):
        return False
    core = bind.execute(
        sa.select(records.c.domain_id, records.c.version, records.c.data).where(
            records.c.id == 1155
        )
    ).mappings().first()
    data = core["data"] if core is not None else None
    return bool(
        core is not None
        and int(core["domain_id"]) == 37
        and int(core["version"]) >= 8
        and isinstance(data, Mapping)
        and data.get("slug") == "uwear-engineering-triage"
    )


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
    required_tables = {"cycles", "cycle_revisions"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not _table_exists(bind, table_name)
    )
    if missing_tables:
        _LOGGER.warning(
            "Cannot deactivate the pinned chantier digest contract because required "
            "tables are missing: %s. Leaving coordinator state unchanged.",
            ", ".join(missing_tables),
        )
        return

    metadata = sa.MetaData()
    cycles = _table(bind, metadata, "cycles")
    revisions = _table(bind, metadata, "cycle_revisions")
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
        _LOGGER.warning(
            "Coordinator cycle 2 was not found; leaving coordinator state unchanged. "
            "Slug-based activation remains pending."
        )
        return

    safe_prompt = _without_pinned_contract(str(cycle["prompt"] or ""))
    if safe_prompt is None:
        if not _core_doc_is_active(bind):
            _LOGGER.warning(
                "Domain 37 doc 1155 is not activated at v8, but coordinator cycle 2 "
                "does not contain the known pinned record-1274 contract. Leaving it "
                "unchanged; slug-based activation remains pending."
            )
        return

    update_values: dict[str, object] = {"prompt": safe_prompt}
    if "updated_at" in cycles.c:
        update_values["updated_at"] = sa.func.now()
    bind.execute(
        cycles.update().where(cycles.c.id == cycle["id"]).values(**update_values)
    )
    _record_cycle_revision(bind, revisions, cycle, safe_prompt)
    _LOGGER.warning(
        "Deactivated coordinator cycle 2's pinned record-1274 chantier contract. "
        "The deploy remains usable; run the slug activation CLI to atomically activate "
        "Domain 37 doc 1155 and the corrected mission."
    )


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Never restore a mission known to point at a record from another domain.
    return None
