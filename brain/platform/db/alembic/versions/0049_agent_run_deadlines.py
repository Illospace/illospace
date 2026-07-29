"""Persist AgentRun deadlines and graceful close-out windows.

Revision ID: 0049_agent_run_deadlines
Revises: 0048_failure_guard_trigger_state
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0049_agent_run_deadlines"
down_revision = "0048_failure_guard_trigger_state"
branch_labels = None
depends_on = None

_TABLE = "agent_runs"
_DEADLINE_INDEX = "ix_agent_runs_status_deadline"
_OPEN_STATUSES = (
    "queued",
    "starting",
    "running",
    "paused",
    "verifying",
)


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {
        column["name"]
        for column in _inspector().get_columns(_TABLE, schema=_schema())
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in _inspector().get_indexes(_TABLE, schema=_schema())
    }


def upgrade() -> None:
    columns = _columns()
    for name in ("deadline_at", "closeout_expires_at", "expired_at"):
        if name not in columns:
            op.add_column(
                _TABLE,
                sa.Column(name, sa.DateTime(timezone=True), nullable=True),
            )

    if _DEADLINE_INDEX not in _indexes():
        op.create_index(
            _DEADLINE_INDEX,
            _TABLE,
            ["status", "deadline_at"],
            unique=False,
        )

    status_list = ", ".join(f"'{status}'" for status in _OPEN_STATUSES)
    deadline_expression = (
        "COALESCE(started_at, created_at, NOW()) + INTERVAL '900 seconds'"
        if op.get_bind().dialect.name == "postgresql"
        else "datetime(COALESCE(started_at, created_at, CURRENT_TIMESTAMP), '+900 seconds')"
    )
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET deadline_at = {deadline_expression} "
            f"WHERE deadline_at IS NULL AND status IN ({status_list})"
        )
    )


def downgrade() -> None:
    if _DEADLINE_INDEX in _indexes():
        op.drop_index(_DEADLINE_INDEX, table_name=_TABLE)
    columns = _columns()
    for name in ("expired_at", "closeout_expires_at", "deadline_at"):
        if name in columns:
            op.drop_column(_TABLE, name)
