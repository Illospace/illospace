"""Add queryable chantier members and blockers.

Revision ID: 0036_chantier_members_blockers
Revises: 0035_open_ask_ledger
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0036_chantier_members_blockers"
down_revision = "0035_open_ask_ledger"
branch_labels = None
depends_on = None


_TRACKER_SLUG = "github-ticket-tracker"
_OBJECT_KEY = "chantier"
_FIELD_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "member_refs",
        "name": "Member references",
        "field_type": "json",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {
            "type": "array",
            "items": {"type": "string", "min_length": 1},
        },
        "searchable": True,
        "sortable": False,
    },
    {
        "key": "blockers",
        "name": "Blockers",
        "field_type": "json",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {
            "type": "array",
            "items": {"type": "string", "min_length": 1},
        },
        "searchable": True,
        "sortable": False,
    },
)


def _schema(bind: sa.Connection) -> str | None:
    return "public" if bind.dialect.name == "postgresql" else None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in set(
        sa.inspect(bind).get_table_names(schema=_schema(bind))
    )


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


def _upgrade(bind: sa.Connection) -> None:
    required = {"domains", "domain_object_types", "domain_field_definitions"}
    if not all(_table_exists(bind, table_name) for table_name in required):
        return

    metadata = sa.MetaData()
    domains = _table(bind, metadata, "domains")
    object_types = _table(bind, metadata, "domain_object_types")
    fields = _table(bind, metadata, "domain_field_definitions")
    chantiers = bind.execute(
        sa.select(object_types.c.id, object_types.c.domain_id)
        .join(domains, domains.c.id == object_types.c.domain_id)
        .where(
            domains.c.slug == _TRACKER_SLUG,
            object_types.c.key == _OBJECT_KEY,
            _active_condition(domains),
            _active_condition(object_types),
        )
    ).mappings().all()

    for chantier in chantiers:
        for spec in _FIELD_SPECS:
            existing = bind.execute(
                sa.select(fields).where(
                    fields.c.object_type_id == chantier["id"],
                    fields.c.key == spec["key"],
                )
            ).mappings().first()
            values = {
                "domain_id": chantier["domain_id"],
                "object_type_id": chantier["id"],
                **spec,
            }
            if "archived_at" in fields.c:
                values["archived_at"] = None
            if "updated_at" in fields.c:
                values["updated_at"] = sa.func.now()
            if existing is None:
                values.pop("updated_at", None)
                bind.execute(fields.insert().values(**values))
                continue

            values.pop("domain_id", None)
            values.pop("object_type_id", None)
            values.pop("key", None)
            bind.execute(
                fields.update()
                .where(fields.c.id == existing["id"])
                .values(**values)
            )


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Keep the fields so existing digest payloads remain valid after rollback.
    return None
