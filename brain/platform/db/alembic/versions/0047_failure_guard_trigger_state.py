"""Persist generic mutable state beside failure-guard trigger latches.

Revision ID: 0047_failure_guard_trigger_state
Revises: 0046_cycle_failure_guard
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0047_failure_guard_trigger_state"
down_revision = "0046_cycle_failure_guard"
branch_labels = None
depends_on = None

_TRIGGER_TABLES = (
    ("scheduler_failure_guard_latches", "scheduler_jobs", "job_id"),
    ("cycle_failure_guard_latches", "cycles", "cycle_id"),
)
_STATE_COLUMN = "trigger_state"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _columns(table_name: str) -> dict[str, dict]:
    if not _table_exists(table_name):
        return {}
    return {
        column["name"]: column
        for column in _inspector().get_columns(table_name, schema=_schema())
    }


def _state_type():
    if op.get_bind().dialect.name == "postgresql":
        return JSONB()
    return sa.JSON()


def _state_default():
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def _make_trigger_rows_stateful(table_name: str) -> bool:
    columns = _columns(table_name)
    if not columns:
        return False
    added_state = _STATE_COLUMN not in columns
    with op.batch_alter_table(table_name, schema=_schema()) as batch:
        if added_state:
            batch.add_column(
                sa.Column(
                    _STATE_COLUMN,
                    _state_type(),
                    nullable=False,
                    server_default=_state_default(),
                )
            )
        if not columns["alerted_at"]["nullable"]:
            batch.alter_column(
                "alerted_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
            )
    return added_state


def _backfill_consecutive_state(
    latch_table: str,
    owner_table: str,
    owner_key: str,
) -> None:
    if not _table_exists(owner_table):
        return
    bind = op.get_bind()
    owners = bind.execute(
        sa.text(
            f"SELECT id, consecutive_failure_count FROM {owner_table} "
            "WHERE consecutive_failure_count > 0"
        )
    ).mappings()
    update_state = sa.text(
        f"UPDATE {latch_table} SET {_STATE_COLUMN} = :trigger_state "
        f"WHERE {owner_key} = :owner_id AND trigger_kind = 'consecutive'"
    ).bindparams(sa.bindparam("trigger_state", type_=sa.JSON()))
    insert_state = sa.text(
        f"INSERT INTO {latch_table} "
        f"({owner_key}, trigger_kind, {_STATE_COLUMN}, alerted_at) "
        "VALUES (:owner_id, 'consecutive', :trigger_state, NULL)"
    ).bindparams(sa.bindparam("trigger_state", type_=sa.JSON()))
    for owner in owners:
        parameters = {
            "owner_id": owner["id"],
            "trigger_state": {
                "count": int(owner["consecutive_failure_count"] or 0)
            },
        }
        updated = bind.execute(update_state, parameters)
        if updated.rowcount == 0:
            bind.execute(insert_state, parameters)


def upgrade() -> None:
    for latch_table, owner_table, owner_key in _TRIGGER_TABLES:
        if not _make_trigger_rows_stateful(latch_table):
            continue
        _backfill_consecutive_state(latch_table, owner_table, owner_key)


def downgrade() -> None:
    for latch_table, _owner_table, _owner_key in _TRIGGER_TABLES:
        columns = _columns(latch_table)
        if not columns:
            continue
        if _STATE_COLUMN in columns:
            op.execute(
                sa.text(
                    f"DELETE FROM {latch_table} WHERE alerted_at IS NULL"
                )
            )
        with op.batch_alter_table(latch_table, schema=_schema()) as batch:
            if _STATE_COLUMN in columns:
                batch.drop_column(_STATE_COLUMN)
            if columns["alerted_at"]["nullable"]:
                batch.alter_column(
                    "alerted_at",
                    existing_type=sa.DateTime(timezone=True),
                    nullable=False,
                )
