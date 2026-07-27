"""Normalize scheduler failure-guard alert latches by trigger.

Revision ID: 0045_scheduler_failure_guard_latches
Revises: 0044_open_ask_deferral_obligations
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0045_scheduler_failure_guard_latches"
down_revision = "0044_open_ask_deferral_obligations"
branch_labels = None
depends_on = None

_TABLE = "scheduler_failure_guard_latches"
_JOB_TABLE = "scheduler_jobs"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _job_column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(_JOB_TABLE, schema=_schema())
    }


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("trigger_kind", sa.String(length=40), nullable=False),
            sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["job_id"],
                ["scheduler_jobs.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("job_id", "trigger_kind"),
        )

    if _job_column_exists("failure_alerted_at"):
        op.execute(
            sa.text(
                "UPDATE scheduler_failure_guard_latches "
                "SET alerted_at = ("
                "SELECT failure_alerted_at FROM scheduler_jobs "
                "WHERE id = scheduler_failure_guard_latches.job_id) "
                "WHERE trigger_kind = 'consecutive' "
                "AND EXISTS ("
                "SELECT 1 FROM scheduler_jobs "
                "WHERE id = scheduler_failure_guard_latches.job_id "
                "AND failure_alerted_at IS NOT NULL)"
            )
        )
        op.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_latches "
                "(job_id, trigger_kind, alerted_at) "
                "SELECT id, 'consecutive', failure_alerted_at "
                "FROM scheduler_jobs "
                "WHERE failure_alerted_at IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM scheduler_failure_guard_latches "
                "WHERE job_id = scheduler_jobs.id "
                "AND trigger_kind = 'consecutive')"
            )
        )
    if _job_column_exists("rate_alerted_at"):
        op.execute(
            sa.text(
                "UPDATE scheduler_failure_guard_latches "
                "SET alerted_at = ("
                "SELECT rate_alerted_at FROM scheduler_jobs "
                "WHERE id = scheduler_failure_guard_latches.job_id) "
                "WHERE trigger_kind = 'rolling_window' "
                "AND EXISTS ("
                "SELECT 1 FROM scheduler_jobs "
                "WHERE id = scheduler_failure_guard_latches.job_id "
                "AND rate_alerted_at IS NOT NULL)"
            )
        )
        op.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_latches "
                "(job_id, trigger_kind, alerted_at) "
                "SELECT id, 'rolling_window', rate_alerted_at "
                "FROM scheduler_jobs "
                "WHERE rate_alerted_at IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM scheduler_failure_guard_latches "
                "WHERE job_id = scheduler_jobs.id "
                "AND trigger_kind = 'rolling_window')"
            )
        )

    if _job_column_exists("failure_alerted_at"):
        op.drop_column(_JOB_TABLE, "failure_alerted_at")
    if _job_column_exists("rate_alerted_at"):
        op.drop_column(_JOB_TABLE, "rate_alerted_at")


def downgrade() -> None:
    if _table_exists(_TABLE):
        unexpected_kind = op.get_bind().execute(
            sa.text(
                "SELECT trigger_kind "
                "FROM scheduler_failure_guard_latches "
                "WHERE trigger_kind NOT IN ('consecutive', 'rolling_window') "
                "LIMIT 1"
            )
        ).scalar()
        if unexpected_kind is not None:
            raise RuntimeError(
                "Cannot downgrade scheduler failure-guard latches with "
                f"unrepresentable trigger kind: {unexpected_kind}"
            )

    if not _job_column_exists("failure_alerted_at"):
        op.add_column(
            _JOB_TABLE,
            sa.Column(
                "failure_alerted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if not _job_column_exists("rate_alerted_at"):
        op.add_column(
            _JOB_TABLE,
            sa.Column(
                "rate_alerted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if not _table_exists(_TABLE):
        return

    op.execute(
        sa.text(
            "UPDATE scheduler_jobs SET failure_alerted_at = "
            "(SELECT alerted_at FROM scheduler_failure_guard_latches "
            "WHERE job_id = scheduler_jobs.id "
            "AND trigger_kind = 'consecutive')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scheduler_jobs SET rate_alerted_at = "
            "(SELECT alerted_at FROM scheduler_failure_guard_latches "
            "WHERE job_id = scheduler_jobs.id "
            "AND trigger_kind = 'rolling_window')"
        )
    )
    op.drop_table(_TABLE)
