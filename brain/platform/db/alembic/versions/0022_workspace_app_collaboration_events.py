"""Add collaborative workspace app events.

Revision ID: 0022_workspace_app_collaboration_events
Revises: 0021_remove_intelligence_tiers
Create Date: 2026-06-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0022_workspace_app_collaboration_events"
down_revision = "0021_remove_intelligence_tiers"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {column["name"] for column in _inspector().get_columns(table_name, schema=_schema())}


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {index["name"] for index in _inspector().get_indexes(table_name, schema=_schema())}


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    constraints = _inspector().get_unique_constraints(table_name, schema=_schema())
    constraints += _inspector().get_foreign_keys(table_name, schema=_schema())
    return constraint_name in {constraint["name"] for constraint in constraints}


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb") if op.get_bind().dialect.name == "postgresql" else sa.text(f"'{value}'")


def _json_type():
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _uuid_type():
    return postgresql.UUID(as_uuid=False) if op.get_bind().dialect.name == "postgresql" else sa.String()


def _create_index_if_missing(name: str, columns: list[str]) -> None:
    if _table_exists("workspace_app_events") and not _index_exists("workspace_app_events", name):
        op.create_index(name, "workspace_app_events", columns)


def upgrade() -> None:
    if _table_exists("workspace_app_states") and not _column_exists("workspace_app_states", "version"):
        op.add_column(
            "workspace_app_states",
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        )

    if not _table_exists("workspace_app_events"):
        op.create_table(
            "workspace_app_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("app_id", _uuid_type(), sa.ForeignKey("workspace_apps.id", ondelete="CASCADE"), nullable=False),
            sa.Column("thread_id", _uuid_type(), nullable=True),
            sa.Column("event_type", sa.String(length=120), nullable=False),
            sa.Column("idempotency_key", sa.String(length=180), nullable=True),
            sa.Column("actor_kind", sa.String(length=40), nullable=False, server_default="user"),
            sa.Column("actor_user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("actor_display", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("payload", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("state_key", sa.String(length=120), nullable=False, server_default="default"),
            sa.Column("state_patch", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("metadata", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("app_id", "idempotency_key", name="uq_workspace_app_events_app_idempotency"),
        )

    _create_index_if_missing("ix_workspace_app_events_org_app_created", ["org_id", "app_id", "created_at"])
    _create_index_if_missing("ix_workspace_app_events_app_type_created", ["app_id", "event_type", "created_at"])
    _create_index_if_missing("ix_workspace_app_events_thread_created", ["thread_id", "created_at"])


def downgrade() -> None:
    # Keep collaborative event history intact on downgrade; repo policy forbids
    # broad destructive table drops in post-baseline migrations.
    if _table_exists("workspace_app_states") and _column_exists("workspace_app_states", "version"):
        op.drop_column("workspace_app_states", "version")
