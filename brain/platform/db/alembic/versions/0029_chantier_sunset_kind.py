"""Add sunset to the chantier kind contract.

Revision ID: 0029_chantier_sunset_kind
Revises: 0028_deactivate_pinned_chantier_digest
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Mapping

from alembic import op
import sqlalchemy as sa


revision = "0029_chantier_sunset_kind"
down_revision = "0028_deactivate_pinned_chantier_digest"
branch_labels = None
depends_on = None


_TRACKER_SLUG = "github-ticket-tracker"
_OBJECT_KEY = "chantier"
_FIELD_KEY = "kind"
_SUNSET_KIND = "sunset"
_SUNSET_RECORD_SLUG = "shopify-app-sunset"


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


def _active_condition(table: sa.Table) -> sa.ColumnElement[bool]:
    if "archived_at" not in table.c:
        return sa.true()
    return table.c.archived_at.is_(None)


def _upgrade(bind: sa.Connection) -> None:
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
    records = (
        _table(bind, metadata, "domain_records")
        if _table_exists(bind, "domain_records")
        else None
    )

    chantier_rows = bind.execute(
        sa.select(object_types.c.id, object_types.c.domain_id)
        .join(domains, domains.c.id == object_types.c.domain_id)
        .where(
            domains.c.slug == _TRACKER_SLUG,
            object_types.c.key == _OBJECT_KEY,
            _active_condition(domains),
            _active_condition(object_types),
        )
    ).mappings().all()

    for chantier in chantier_rows:
        kind_field = bind.execute(
            sa.select(fields).where(
                fields.c.object_type_id == chantier["id"],
                fields.c.key == _FIELD_KEY,
                _active_condition(fields),
            )
        ).mappings().first()
        if kind_field is not None:
            options = list(kind_field.get("options") or [])
            if _SUNSET_KIND not in options:
                values: dict[str, object] = {"options": [*options, _SUNSET_KIND]}
                if "updated_at" in fields.c:
                    values["updated_at"] = sa.func.now()
                bind.execute(
                    fields.update()
                    .where(fields.c.id == kind_field["id"])
                    .values(**values)
                )

        if records is not None:
            _migrate_sunset_record(bind, records, int(chantier["id"]))


def _migrate_sunset_record(
    bind: sa.Connection,
    records: sa.Table,
    object_type_id: int,
) -> None:
    required_columns = {"id", "object_type_id", "data"}
    if not required_columns.issubset(records.c.keys()):
        return

    rows = bind.execute(
        sa.select(records).where(
            records.c.object_type_id == object_type_id,
            _active_condition(records),
        )
    ).mappings().all()
    for row in rows:
        data = row.get("data")
        if not isinstance(data, Mapping):
            continue
        if data.get("slug") != _SUNSET_RECORD_SLUG or data.get("kind") != "quality":
            continue
        updated_data = dict(data)
        updated_data["kind"] = _SUNSET_KIND
        values: dict[str, object] = {"data": updated_data}
        if "version" in records.c:
            values["version"] = int(row.get("version") or 0) + 1
        if "updated_at" in records.c:
            values["updated_at"] = sa.func.now()
        bind.execute(
            records.update().where(records.c.id == row["id"]).values(**values)
        )


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Deliberate no-op: removing an accepted value could invalidate records.
    return None
