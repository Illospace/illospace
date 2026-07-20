"""Add the chantier superseded-by retirement link.

Revision ID: 0032_chantier_superseded_by
Revises: 0030_consolidation_phase_width
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_chantier_superseded_by"
down_revision = "0030_consolidation_phase_width"
branch_labels = None
depends_on = None


_TRACKER_SLUG = "github-ticket-tracker"
_OBJECT_KEY = "chantier"
_FIELD_KEY = "superseded_by"
_FIELD_VALUES = {
    "name": "Superseded by chantier",
    "field_type": "text",
    "required": False,
    "options": [],
    "default_value": None,
    "validation": {
        "max_length": 80,
        "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    },
    "searchable": True,
    "sortable": False,
}


def _schema(bind: sa.Connection) -> str | None:
    return "public" if bind.dialect.name == "postgresql" else None


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return table_name in set(sa.inspect(bind).get_table_names(schema=_schema(bind)))


def _table(bind: sa.Connection, metadata: sa.MetaData, table_name: str) -> sa.Table:
    return sa.Table(table_name, metadata, schema=_schema(bind), autoload_with=bind)


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
        existing = bind.execute(
            sa.select(fields).where(
                fields.c.object_type_id == chantier["id"],
                fields.c.key == _FIELD_KEY,
            )
        ).mappings().first()
        values = {
            "domain_id": chantier["domain_id"],
            "object_type_id": chantier["id"],
            "key": _FIELD_KEY,
            **_FIELD_VALUES,
        }
        if "archived_at" in fields.c:
            values["archived_at"] = None
        if "updated_at" in fields.c:
            values["updated_at"] = sa.func.now()
        if existing is None:
            values.pop("updated_at", None)
            bind.execute(fields.insert().values(**values))
        else:
            values.pop("domain_id", None)
            values.pop("object_type_id", None)
            values.pop("key", None)
            bind.execute(
                fields.update().where(fields.c.id == existing["id"]).values(**values)
            )


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Keep the optional field: removing it would make retired record payloads
    # fail their dynamic schema on the next unrelated update.
    return None
