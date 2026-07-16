"""Provision the Domain-1 chantier record contract.

Revision ID: 0026_chantier_object_type
Revises: 0025_pr_tracker_owner_fields
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0026_chantier_object_type"
down_revision = "0025_pr_tracker_owner_fields"
branch_labels = None
depends_on = None


_TRACKER_SLUG = "github-ticket-tracker"
_OBJECT_KEY = "chantier"
_CONTRACT_MARKER = "chantier-record-contract-v1"
_OBJECT_VALUES = {
    "name": "Chantier",
    "description": (
        "Cross-repository outcome container for coordinated work. "
        f"Schema marker: {_CONTRACT_MARKER}."
    ),
    "title_field": "title",
}
_GITHUB_ISSUE_REF_PATTERN = (
    r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:issue:[1-9][0-9]*"
)
_REFERENCE_SOURCES = ["github", "doc", "slack", "posthog", "url"]
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
_FIELD_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "slug",
        "name": "Stable slug",
        "field_type": "text",
        "required": True,
        "options": [],
        "default_value": None,
        "validation": {
            "immutable": True,
            "max_length": 80,
            "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        },
        "searchable": True,
        "sortable": True,
    },
    {
        "key": "title",
        "name": "Title",
        "field_type": "text",
        "required": True,
        "options": [],
        "default_value": None,
        "validation": {"max_length": 500},
        "searchable": True,
        "sortable": True,
    },
    {
        "key": "goal",
        "name": "Done-means goal",
        "field_type": "long_text",
        "required": True,
        "options": [],
        "default_value": None,
        "validation": {
            "max_length": 4000,
            "pattern": r"(?is)^done means\s+\S.*$",
        },
        "searchable": True,
        "sortable": False,
    },
    {
        "key": "kind",
        "name": "Kind",
        "field_type": "enum",
        "required": True,
        "options": ["feature", "incident", "quality", "gtm"],
        "default_value": None,
        "validation": {},
        "searchable": True,
        "sortable": True,
    },
    {
        "key": "state",
        "name": "State",
        "field_type": "enum",
        "required": True,
        "options": ["exploring", "building", "shipping", "verifying", "done", "paused"],
        "default_value": None,
        "validation": {},
        "searchable": True,
        "sortable": True,
    },
    {
        "key": "owner",
        "name": "Next-action owner",
        "field_type": "text",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {"max_length": 120},
        "searchable": True,
        "sortable": True,
    },
    {
        "key": "refs",
        "name": "Typed references",
        "field_type": "json",
        "required": True,
        "options": [],
        "default_value": None,
        "validation": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "ref"],
                "additional_properties": False,
                "properties": {
                    "source": {"type": "string", "enum": _REFERENCE_SOURCES},
                    "ref": {"type": "string", "min_length": 1},
                    "title": {"type": "string", "min_length": 1},
                },
                "source_ref_patterns": {"github": _GITHUB_ISSUE_REF_PATTERN},
            },
        },
        "searchable": False,
        "sortable": False,
    },
    {
        "key": "parent_issue",
        "name": "GitHub mirror parent issue",
        "field_type": "text",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {"pattern": f"^{_GITHUB_ISSUE_REF_PATTERN}$"},
        "searchable": True,
        "sortable": False,
    },
    {
        "key": "next_step",
        "name": "Next most valuable step",
        "field_type": "text",
        "required": True,
        "options": [],
        "default_value": None,
        "validation": {"max_length": 500, "pattern": r"^[^\r\n]+$"},
        "searchable": True,
        "sortable": False,
    },
    {
        "key": "progress_note",
        "name": "Progress note",
        "field_type": "long_text",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {"max_length": 2000},
        "searchable": True,
        "sortable": False,
    },
    {
        "key": "created_at",
        "name": "Source created at",
        "field_type": "datetime",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {},
        "searchable": False,
        "sortable": True,
    },
    {
        "key": "updated_at",
        "name": "Source updated at",
        "field_type": "datetime",
        "required": False,
        "options": [],
        "default_value": None,
        "validation": {},
        "searchable": False,
        "sortable": True,
    },
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


def _active_condition(table: sa.Table) -> sa.ColumnElement[bool]:
    if "archived_at" not in table.c:
        return sa.true()
    return table.c.archived_at.is_(None)


def _changed_values(
    row: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> dict[str, Any]:
    return {key: value for key, value in desired.items() if row.get(key) != value}


def _next_sort_order(bind: sa.Connection, object_types: sa.Table, domain_id: int) -> int:
    if "sort_order" not in object_types.c:
        return 0
    current = bind.scalar(
        sa.select(sa.func.max(object_types.c.sort_order)).where(
            object_types.c.domain_id == domain_id,
            _active_condition(object_types),
        )
    )
    return int(current or 0) + 1


def _ensure_object_type(
    bind: sa.Connection,
    object_types: sa.Table,
    domain_id: int,
) -> int:
    existing = bind.execute(
        sa.select(object_types).where(
            object_types.c.domain_id == domain_id,
            object_types.c.key == _OBJECT_KEY,
        )
    ).mappings().first()
    desired = dict(_OBJECT_VALUES)
    if "archived_at" in object_types.c:
        desired["archived_at"] = None

    if existing is None:
        insert_values: dict[str, Any] = {
            "domain_id": domain_id,
            "key": _OBJECT_KEY,
            **desired,
        }
        if "sort_order" in object_types.c:
            insert_values["sort_order"] = _next_sort_order(bind, object_types, domain_id)
        result = bind.execute(object_types.insert().values(**insert_values))
        return int(result.inserted_primary_key[0])

    changed = _changed_values(existing, desired)
    if _CONTRACT_MARKER not in str(existing.get("description") or ""):
        changed["description"] = _OBJECT_VALUES["description"]
    if changed:
        if "updated_at" in object_types.c:
            changed["updated_at"] = sa.func.now()
        bind.execute(
            object_types.update()
            .where(object_types.c.id == existing["id"])
            .values(**changed)
        )
    return int(existing["id"])


def _ensure_fields(
    bind: sa.Connection,
    fields: sa.Table,
    *,
    domain_id: int,
    object_type_id: int,
) -> None:
    for spec in _FIELD_SPECS:
        desired = {column: spec[column] for column in _FIELD_DEFINITION_COLUMNS}
        if "archived_at" in fields.c:
            desired["archived_at"] = None
        existing = bind.execute(
            sa.select(fields).where(
                fields.c.object_type_id == object_type_id,
                fields.c.key == spec["key"],
            )
        ).mappings().first()
        if existing is None:
            bind.execute(
                fields.insert().values(
                    domain_id=domain_id,
                    object_type_id=object_type_id,
                    key=spec["key"],
                    **desired,
                )
            )
            continue

        changed = _changed_values(existing, desired)
        if not changed:
            continue
        if "updated_at" in fields.c:
            changed["updated_at"] = sa.func.now()
        bind.execute(
            fields.update().where(fields.c.id == existing["id"]).values(**changed)
        )


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
    domain_ids = bind.scalars(
        sa.select(domains.c.id).where(
            domains.c.slug == _TRACKER_SLUG,
            _active_condition(domains),
        )
    ).all()
    for domain_id in domain_ids:
        object_type_id = _ensure_object_type(bind, object_types, int(domain_id))
        _ensure_fields(
            bind,
            fields,
            domain_id=int(domain_id),
            object_type_id=object_type_id,
        )


def upgrade() -> None:
    _upgrade(op.get_bind())


def downgrade() -> None:
    # Deliberate no-op: removing the schema would strand existing chantier
    # records and typed cross-repository references.
    return None
