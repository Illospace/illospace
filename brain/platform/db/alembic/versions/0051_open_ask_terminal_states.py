"""Add routed and expired states to the open-ask ledger.

Revision ID: 0051_open_ask_terminal_states
Revises: 0050_scheduler_cold_start
Create Date: 2026-08-04
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0051_open_ask_terminal_states"
down_revision = "0050_scheduler_cold_start"
branch_labels = None
depends_on = None

_TABLE = "open_asks"
_STATUS_CONSTRAINT = "ck_open_asks_status"
_STATUS_VALUES = ("open", "answered", "routed", "expired")
_BACKFILL_REASON = (
    "Backfilled by migration 0051: run deferral remained open for more than 72h."
)
_BACKFILL_ROWS = (
    (5, "2026-07-28T10:13:17+00:00", "New issue created: TypeError"),
    (6, "2026-07-28T13:17:56+00:00", "Rollbar #2326 New error"),
    (7, "2026-07-28T13:17:56+00:00", "Rollbar #2323 10th error"),
    (8, "2026-07-28T14:26:22+00:00", "Rollbar #2328 New error"),
    (9, "2026-07-28T14:26:23+00:00", "Retool issue — mroyer@pentagone.com"),
    (10, "2026-07-28T17:07:30+00:00", "New issue created: Error"),
    (11, "2026-07-28T19:38:33+00:00", "Rollbar #2330 New error"),
    (12, "2026-07-29T08:59:52+00:00", "Rollbar #2332 New error"),
    (19, "2026-07-29T16:38:29+00:00", "New issue created: DOMException"),
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


def _constraint_exists(name: str) -> bool:
    return name in {
        constraint["name"]
        for constraint in _inspector().get_check_constraints(
            _TABLE,
            schema=_schema(),
        )
    }


def _status_expression(statuses: tuple[str, ...]) -> str:
    values = ", ".join(f"'{status}'" for status in statuses)
    return f"status IN ({values})"


def _replace_status_constraint(statuses: tuple[str, ...]) -> None:
    exists = _constraint_exists(_STATUS_CONSTRAINT)
    if op.get_bind().dialect.name == "postgresql":
        if exists:
            op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
        op.create_check_constraint(
            _STATUS_CONSTRAINT,
            _TABLE,
            _status_expression(statuses),
        )
        return

    with op.batch_alter_table(_TABLE) as batch:
        if exists:
            batch.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            _STATUS_CONSTRAINT,
            _status_expression(statuses),
        )


def _add_state_columns() -> None:
    columns = _columns()
    additions = (
        ("routed_to_name", sa.String(length=120)),
        ("routed_to_slack_id", sa.String(length=80)),
        ("routed_at", sa.DateTime(timezone=True)),
        ("expired_at", sa.DateTime(timezone=True)),
        ("status_reason", sa.Text()),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column(
                _TABLE,
                sa.Column(name, column_type, nullable=True),
            )


def _backfill_stuck_run_deferrals() -> None:
    open_asks = sa.table(
        _TABLE,
        sa.column("id", sa.Integer()),
        sa.column("obligation_kind", sa.String()),
        sa.column("status", sa.String()),
        sa.column("opened_at", sa.DateTime(timezone=True)),
        sa.column("ask_text", sa.Text()),
        sa.column("expired_at", sa.DateTime(timezone=True)),
        sa.column("status_reason", sa.Text()),
    )
    for row_id, opened_at, ask_text in _BACKFILL_ROWS:
        # Match every fact from #667 so a database with different rows is untouched.
        op.get_bind().execute(
            sa.update(open_asks)
            .where(
                open_asks.c.id == row_id,
                open_asks.c.obligation_kind == "run_deferral",
                open_asks.c.status == "open",
                open_asks.c.opened_at
                == datetime.fromisoformat(opened_at).astimezone(timezone.utc),
                open_asks.c.ask_text == ask_text,
            )
            .values(
                status="expired",
                expired_at=sa.func.now(),
                status_reason=_BACKFILL_REASON,
            )
        )


def upgrade() -> None:
    _add_state_columns()
    _replace_status_constraint(_STATUS_VALUES)
    _backfill_stuck_run_deferrals()


def downgrade() -> None:
    open_asks = sa.table(
        _TABLE,
        sa.column("status", sa.String()),
        sa.column("status_reason", sa.Text()),
    )
    op.get_bind().execute(
        sa.update(open_asks)
        .where(open_asks.c.status.in_(("routed", "expired")))
        .values(status="open", status_reason=None)
    )
    _replace_status_constraint(("open", "answered"))

    removable = (
        "routed_to_name",
        "routed_to_slack_id",
        "routed_at",
        "expired_at",
        "status_reason",
    )
    existing = _columns()
    if op.get_bind().dialect.name == "postgresql":
        for name in removable:
            if name in existing:
                op.drop_column(_TABLE, name)
        return
    with op.batch_alter_table(_TABLE) as batch:
        for name in removable:
            if name in existing:
                batch.drop_column(name)
