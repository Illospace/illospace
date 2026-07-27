"""Extend the open-ask ledger to carry run deferral obligations.

Revision ID: 0044_open_ask_deferral_obligations
Revises: 0042_scheduler_failure_rate_guard
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0044_open_ask_deferral_obligations"
down_revision = "0042_scheduler_failure_rate_guard"
branch_labels = None
depends_on = None

_TABLE = "open_asks"
_NOTICE_TABLE = "obligation_notices"
_DEFERRAL_INDEX = "uq_open_asks_run_deferral"
_HUMAN_INDEX = "uq_open_asks_slack_requester"
_KIND_CONSTRAINT = "ck_open_asks_obligation_kind"
_HUMAN_SHAPE_CONSTRAINT = "ck_open_asks_human_requester"
_RUN_SHAPE_CONSTRAINT = "ck_open_asks_run_origin"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(_TABLE, schema=_schema())
    }


def _column_is_nullable(column_name: str) -> bool:
    for column in _inspector().get_columns(_TABLE, schema=_schema()):
        if column["name"] == column_name:
            return bool(column.get("nullable"))
    return False


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(table_name, schema=_schema())
    }


def _unique_constraint_exists(constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_unique_constraints(
            _TABLE,
            schema=_schema(),
        )
    }


def _constraint_exists(constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_check_constraints(
            _TABLE,
            schema=_schema(),
        )
    }


def _uuid_type():
    if op.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects import postgresql

        return postgresql.UUID(as_uuid=False)
    return sa.String()


def _make_requester_nullable_and_replace_uniqueness() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        if _unique_constraint_exists(_HUMAN_INDEX):
            op.drop_constraint(_HUMAN_INDEX, _TABLE, type_="unique")
        op.alter_column(
            _TABLE,
            "requester_slack_id",
            existing_type=sa.String(length=80),
            nullable=True,
        )
        return

    # SQLite cannot alter nullability, remove a table constraint, or add the
    # shape checks directly. Batch mode rebuilds this unmerged shape in place.
    has_human_unique = _unique_constraint_exists(_HUMAN_INDEX)
    missing_checks = [
        (name, condition)
        for name, condition in (
            (
                _KIND_CONSTRAINT,
                "obligation_kind IN ('human_ask', 'run_deferral')",
            ),
            (
                _HUMAN_SHAPE_CONSTRAINT,
                "obligation_kind != 'human_ask' OR requester_slack_id IS NOT NULL",
            ),
            (
                _RUN_SHAPE_CONSTRAINT,
                "obligation_kind != 'run_deferral' OR origin_run_id IS NOT NULL",
            ),
        )
        if not _constraint_exists(name)
    ]
    if (
        has_human_unique
        or not _column_is_nullable("requester_slack_id")
        or missing_checks
    ):
        with op.batch_alter_table(_TABLE) as batch:
            if has_human_unique:
                batch.drop_constraint(_HUMAN_INDEX, type_="unique")
            if not _column_is_nullable("requester_slack_id"):
                batch.alter_column(
                    "requester_slack_id",
                    existing_type=sa.String(length=80),
                    nullable=True,
                )
            for name, condition in missing_checks:
                batch.create_check_constraint(name, condition)


def _create_notice_table() -> None:
    if not _table_exists(_NOTICE_TABLE):
        op.create_table(
            _NOTICE_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("obligation_id", sa.Integer(), nullable=False),
            sa.Column("org_id", _uuid_type(), nullable=False),
            sa.Column("condition", sa.String(length=160), nullable=False),
            sa.Column("idempotency_key", sa.String(length=240), nullable=False),
            sa.Column(
                "state",
                sa.String(length=20),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column("channel_id", sa.String(length=80), nullable=False),
            sa.Column("thread_ts", sa.String(length=40), nullable=False),
            sa.Column("post_thread_ts", sa.String(length=40), nullable=True),
            sa.Column("bot_user_id", sa.String(length=80), nullable=True),
            sa.Column("notice_text", sa.Text(), nullable=False),
            sa.Column(
                "attempts",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_message_ts", sa.String(length=40), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "state IN ('pending', 'posting', 'delivered', 'superseded')",
                name="ck_obligation_notices_state",
            ),
            sa.ForeignKeyConstraint(
                ["obligation_id"],
                ["open_asks.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "obligation_id",
                "condition",
                name="uq_obligation_notices_obligation_condition",
            ),
            sa.UniqueConstraint(
                "idempotency_key",
                name="uq_obligation_notices_idempotency_key",
            ),
        )
    if not _index_exists(_NOTICE_TABLE, "ix_obligation_notices_org_state"):
        op.create_index(
            "ix_obligation_notices_org_state",
            _NOTICE_TABLE,
            ["org_id", "state"],
        )


def upgrade() -> None:
    if not _column_exists("obligation_kind"):
        op.add_column(
            _TABLE,
            sa.Column(
                "obligation_kind",
                sa.String(length=32),
                server_default=sa.text("'human_ask'"),
                nullable=False,
            ),
        )
    # A pre-fix development database may have run the earlier unmerged shape.
    # Delivery attempt history belongs in obligation_notices, never OpenAsk.
    if _column_exists("notice_conditions"):
        op.drop_column(_TABLE, "notice_conditions")

    _make_requester_nullable_and_replace_uniqueness()

    if op.get_bind().dialect.name == "postgresql":
        for name, condition in (
            (
                _KIND_CONSTRAINT,
                "obligation_kind IN ('human_ask', 'run_deferral')",
            ),
            (
                _HUMAN_SHAPE_CONSTRAINT,
                "obligation_kind != 'human_ask' OR requester_slack_id IS NOT NULL",
            ),
            (
                _RUN_SHAPE_CONSTRAINT,
                "obligation_kind != 'run_deferral' OR origin_run_id IS NOT NULL",
            ),
        ):
            if not _constraint_exists(name):
                op.create_check_constraint(name, _TABLE, condition)

    if not _index_exists(_TABLE, _HUMAN_INDEX):
        op.create_index(
            _HUMAN_INDEX,
            _TABLE,
            ["org_id", "channel_id", "thread_ts", "requester_slack_id"],
            unique=True,
            postgresql_where=sa.text("obligation_kind = 'human_ask'"),
            sqlite_where=sa.text("obligation_kind = 'human_ask'"),
        )
    if not _index_exists(_TABLE, _DEFERRAL_INDEX):
        op.create_index(
            _DEFERRAL_INDEX,
            _TABLE,
            ["org_id", "channel_id", "thread_ts", "origin_run_id"],
            unique=True,
            postgresql_where=sa.text("obligation_kind = 'run_deferral'"),
            sqlite_where=sa.text("obligation_kind = 'run_deferral'"),
        )

    _create_notice_table()


def downgrade() -> None:
    # Preserve unresolved obligations and their notification history.
    return None
