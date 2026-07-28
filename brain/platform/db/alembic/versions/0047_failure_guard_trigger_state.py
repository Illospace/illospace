"""Move failure-guard trigger state out of owners and alert latches.

Revision ID: 0047_failure_guard_trigger_state
Revises: 0046_cycle_failure_guard
Create Date: 2026-07-28
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0047_failure_guard_trigger_state"
down_revision = "0046_cycle_failure_guard"
branch_labels = None
depends_on = None

_STATE_STORES = (
    (
        "scheduler_failure_guard_trigger_states",
        "scheduler_jobs",
        "job_id",
    ),
    (
        "cycle_failure_guard_trigger_states",
        "cycles",
        "cycle_id",
    ),
)
_LEGACY_COUNT_COLUMN = "consecutive_failure_count"


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


def _ensure_state_store(
    state_table: str,
    owner_table: str,
    owner_key: str,
) -> None:
    if _table_exists(state_table):
        return
    op.create_table(
        state_table,
        sa.Column(
            owner_key,
            sa.Integer(),
            sa.ForeignKey(f"{owner_table}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trigger_kind", sa.String(length=40), primary_key=True),
        sa.Column(
            "trigger_state",
            _state_type(),
            nullable=False,
            server_default=_state_default(),
        ),
    )


def _backfill_missing_consecutive_state(
    state_table: str,
    owner_table: str,
    owner_key: str,
) -> None:
    """Idempotently convert positive legacy counts with one set-based insert."""
    if _LEGACY_COUNT_COLUMN not in _columns(owner_table):
        return
    json_expression = (
        f"jsonb_build_object('count', owner.{_LEGACY_COUNT_COLUMN})"
        if op.get_bind().dialect.name == "postgresql"
        else f"json_object('count', owner.{_LEGACY_COUNT_COLUMN})"
    )
    op.execute(
        sa.text(
            f"INSERT INTO {state_table} "
            f"({owner_key}, trigger_kind, trigger_state) "
            f"SELECT owner.id, 'consecutive', {json_expression} "
            f"FROM {owner_table} AS owner "
            f"WHERE owner.{_LEGACY_COUNT_COLUMN} > 0 "
            "AND NOT EXISTS ("
            f"SELECT 1 FROM {state_table} AS state "
            f"WHERE state.{owner_key} = owner.id "
            "AND state.trigger_kind = 'consecutive')"
        )
    )


def _drop_legacy_count(owner_table: str) -> None:
    if _LEGACY_COUNT_COLUMN in _columns(owner_table):
        op.drop_column(owner_table, _LEGACY_COUNT_COLUMN)


def upgrade() -> None:
    for state_table, owner_table, owner_key in _STATE_STORES:
        _ensure_state_store(state_table, owner_table, owner_key)
    for state_table, owner_table, owner_key in _STATE_STORES:
        _backfill_missing_consecutive_state(
            state_table,
            owner_table,
            owner_key,
        )
    for _state_table, owner_table, _owner_key in _STATE_STORES:
        _drop_legacy_count(owner_table)


def _decoded_state(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(
            "Cannot downgrade failure-guard state with a non-object document"
        )
    return value


def _downgrade_counts(
    state_table: str,
    owner_key: str,
) -> dict[int, int]:
    if not _table_exists(state_table):
        return {}
    rows = op.get_bind().execute(
        sa.text(
            f"SELECT {owner_key}, trigger_kind, trigger_state "
            f"FROM {state_table}"
        )
    ).mappings()
    counts: dict[int, int] = {}
    for row in rows:
        if row["trigger_kind"] != "consecutive":
            raise RuntimeError(
                "Cannot downgrade failure-guard state with "
                f"unrepresentable trigger kind: {row['trigger_kind']}"
            )
        state = _decoded_state(row["trigger_state"])
        count = state.get("count")
        if (
            set(state) != {"count"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise RuntimeError(
                "Cannot downgrade consecutive failure-guard state with "
                f"unrepresentable document: {state!r}"
            )
        counts[int(row[owner_key])] = count
    return counts


def _ensure_legacy_count(owner_table: str) -> None:
    if _LEGACY_COUNT_COLUMN in _columns(owner_table):
        return
    op.add_column(
        owner_table,
        sa.Column(
            _LEGACY_COUNT_COLUMN,
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    # Validate every state document before changing schema. Custom state cannot be
    # represented by the legacy integer column, so downgrade rejects it explicitly.
    counts_by_store = [
        (
            state_table,
            owner_table,
            owner_key,
            _table_exists(state_table),
            _downgrade_counts(state_table, owner_key),
        )
        for state_table, owner_table, owner_key in _STATE_STORES
    ]

    for (
        state_table,
        owner_table,
        owner_key,
        state_store_exists,
        counts,
    ) in counts_by_store:
        _ensure_legacy_count(owner_table)
        if not state_store_exists:
            continue
        op.execute(
            sa.text(
                f"UPDATE {owner_table} "
                f"SET {_LEGACY_COUNT_COLUMN} = 0"
            )
        )
        update_count = sa.text(
            f"UPDATE {owner_table} "
            f"SET {_LEGACY_COUNT_COLUMN} = :count "
            "WHERE id = :owner_id"
        )
        for owner_id, count in counts.items():
            op.get_bind().execute(
                update_count,
                {"owner_id": owner_id, "count": count},
            )
        if _table_exists(state_table):
            op.drop_table(state_table)
