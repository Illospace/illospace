"""Align pull-request tracking fields with Cycle missions.

Revision ID: 0025_pr_tracker_owner_fields
Revises: 0024_cycle_degradation_state
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Mapping

from alembic import op
import sqlalchemy as sa


revision = "0025_pr_tracker_owner_fields"
down_revision = "0024_cycle_degradation_state"
branch_labels = None
depends_on = None


_TRACKER_SLUG = "github-ticket-tracker"
_MIRRORED_FIELD_KEYS = ("assignee", "progress_note")
_FIELD_DEFINITION_COLUMNS = (
    "name",
    "field_type",
    "required",
    "options",
    "default_value",
    "validation",
    "searchable",
    "sortable",
)
_TARGET_CYCLES = {
    2: "Uwear Ticket Coordinator Check-ins",
    8: "GitHub Reflex",
}
_MISSION_CONTRACT_MARKER = "Pull request write contract:"
_MISSION_CONTRACT = (
    'Pull request write contract: On `pull_request` records, store draft-ness only as '
    '`state: "draft"`; use `review_status: "pending"` for newly opened or draft PR '
    "records, and never write `draft` to `review_status`. Store owner by next action "
    "in `assignee` and the one-line next action in `progress_note`."
)
_REVISION_RATIONALE = (
    "Document the pull-request draft, review-status, owner, and next-action field contract."
)


def _schema(bind: sa.Connection) -> str | None:
    return "public" if bind.dialect.name == "postgresql" else None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in set(sa.inspect(bind).get_table_names(schema=_schema(bind)))


def _table(
    bind: sa.Connection,
    metadata: sa.MetaData,
    table_name: str,
) -> sa.Table:
    return sa.Table(
        table_name,
        metadata,
        schema=_schema(bind),
        autoload_with=bind,
    )


def _active_condition(table: sa.Table) -> sa.ColumnElement[bool]:
    if "archived_at" not in table.c:
        return sa.true()
    return table.c.archived_at.is_(None)


def _mirror_pull_request_fields(bind: sa.Connection) -> None:
    required_tables = {
        "domains",
        "domain_object_types",
        "domain_field_definitions",
    }
    if not all(_table_exists(bind, table_name) for table_name in required_tables):
        return

    metadata = sa.MetaData()
    domains = _table(bind, metadata, "domains")
    object_types = _table(bind, metadata, "domain_object_types")
    fields = _table(bind, metadata, "domain_field_definitions")
    ticket_objects = object_types.alias("ticket_objects")
    pull_request_objects = object_types.alias("pull_request_objects")

    object_pairs = bind.execute(
        sa.select(
            pull_request_objects.c.domain_id,
            pull_request_objects.c.id.label("pull_request_object_type_id"),
            ticket_objects.c.id.label("ticket_object_type_id"),
        )
        .select_from(
            pull_request_objects.join(
                domains,
                domains.c.id == pull_request_objects.c.domain_id,
            ).join(
                ticket_objects,
                sa.and_(
                    ticket_objects.c.domain_id == pull_request_objects.c.domain_id,
                    ticket_objects.c.key == "ticket",
                ),
            )
        )
        .where(
            domains.c.slug == _TRACKER_SLUG,
            pull_request_objects.c.key == "pull_request",
            _active_condition(domains),
            _active_condition(ticket_objects),
            _active_condition(pull_request_objects),
        )
    ).mappings()

    for pair in object_pairs:
        source_fields = bind.execute(
            sa.select(fields)
            .where(
                fields.c.object_type_id == pair["ticket_object_type_id"],
                fields.c.key.in_(_MIRRORED_FIELD_KEYS),
                _active_condition(fields),
            )
            .order_by(fields.c.id)
        ).mappings()
        for source in source_fields:
            mirrored_values = {
                column: source[column]
                for column in _FIELD_DEFINITION_COLUMNS
            }
            if "archived_at" in fields.c:
                mirrored_values["archived_at"] = None

            existing = bind.execute(
                sa.select(fields).where(
                    fields.c.object_type_id
                    == pair["pull_request_object_type_id"],
                    fields.c.key == source["key"],
                )
            ).mappings().first()
            if existing is not None:
                if all(
                    existing[column] == value
                    for column, value in mirrored_values.items()
                ):
                    continue
                update_values = dict(mirrored_values)
                if "updated_at" in fields.c:
                    update_values["updated_at"] = sa.func.now()
                bind.execute(
                    fields.update()
                    .where(fields.c.id == existing["id"])
                    .values(**update_values)
                )
                continue

            bind.execute(
                fields.insert().values(
                    domain_id=pair["domain_id"],
                    object_type_id=pair["pull_request_object_type_id"],
                    key=source["key"],
                    **mirrored_values,
                )
            )


def _mission_prompt(prompt: str) -> str:
    clean_prompt = str(prompt or "").rstrip()
    if _MISSION_CONTRACT_MARKER in clean_prompt:
        return clean_prompt
    return f"{clean_prompt}\n\n{_MISSION_CONTRACT}"


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


def _reconcile_cycle_missions(bind: sa.Connection) -> None:
    if not _table_exists(bind, "cycles"):
        return

    metadata = sa.MetaData()
    cycles = _table(bind, metadata, "cycles")
    revisions = (
        _table(bind, metadata, "cycle_revisions")
        if _table_exists(bind, "cycle_revisions")
        else None
    )
    target_condition = sa.or_(
        *(
            sa.and_(cycles.c.id == cycle_id, cycles.c.name == cycle_name)
            for cycle_id, cycle_name in _TARGET_CYCLES.items()
        )
    )
    cycle_rows = bind.execute(
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
        ).where(target_condition)
    ).mappings().all()

    for cycle in cycle_rows:
        prompt = _mission_prompt(cycle["prompt"])
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


def _upgrade(bind: sa.Connection) -> None:
    _mirror_pull_request_fields(bind)
    _reconcile_cycle_missions(bind)


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Removing these fields could strand owner/next-action values already stored
    # in domain records. Cycle revisions are an append-only audit trail, so the
    # reconciled mission contract is likewise preserved on downgrade.
    return None
