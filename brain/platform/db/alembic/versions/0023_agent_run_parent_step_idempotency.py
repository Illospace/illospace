"""Add durable parent-step idempotency for child AgentRuns.

Revision ID: 0023_agent_run_parent_step_idempotency
Revises: 0022_workspace_app_collaboration_events
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
from hashlib import sha256
import json
import sqlalchemy as sa


revision = "0023_agent_run_parent_step_idempotency"
down_revision = "0022_workspace_app_collaboration_events"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in _inspector().get_columns("agent_runs", schema=_schema())
    }


def _constraint_exists(constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_unique_constraints("agent_runs", schema=_schema())
    }


def _check_exists(constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_check_constraints("agent_runs", schema=_schema())
    }


def _legacy_step_hash(run_id: int, metadata) -> str:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    step_key = str(metadata.get("parent_step_key") or f"legacy-run:{run_id}").strip()
    return sha256(step_key.encode()).hexdigest()


def _backfill_parent_step_hashes() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("LOCK TABLE agent_runs IN ACCESS EXCLUSIVE MODE")
    rows = bind.execute(
        sa.text(
            "SELECT id, parent_run_id, metadata FROM agent_runs "
            "WHERE parent_run_id IS NOT NULL"
        )
    ).mappings()
    seen: dict[tuple[int, str], int] = {}
    updates: list[tuple[int, str]] = []
    for row in rows:
        run_id = int(row["id"])
        parent_run_id = int(row["parent_run_id"])
        key_hash = _legacy_step_hash(run_id, row["metadata"])
        collision_key = (parent_run_id, key_hash)
        if collision_key in seen:
            raise RuntimeError(
                "Duplicate legacy AgentRun parent step detected for runs "
                f"{seen[collision_key]} and {run_id}; reconcile before upgrading"
            )
        seen[collision_key] = run_id
        updates.append((run_id, key_hash))
    for run_id, key_hash in updates:
        bind.execute(
            sa.text(
                "UPDATE agent_runs SET parent_step_key_hash = :key_hash "
                "WHERE id = :run_id"
            ),
            {"run_id": run_id, "key_hash": key_hash},
        )


def upgrade() -> None:
    columns = {
        "parent_step_key_hash": sa.Column("parent_step_key_hash", sa.String(length=64), nullable=True),
        "execution_token": sa.Column("execution_token", sa.String(length=64), nullable=True),
        "execution_attempt": sa.Column(
            "execution_attempt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    }
    missing_columns = [column for name, column in columns.items() if not _column_exists(name)]
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("agent_runs") as batch:
            for column in missing_columns:
                batch.add_column(column)
        _backfill_parent_step_hashes()
        with op.batch_alter_table("agent_runs") as batch:
            if not _constraint_exists("uq_agent_runs_parent_step_key_hash"):
                batch.create_unique_constraint(
                    "uq_agent_runs_parent_step_key_hash",
                    ["parent_run_id", "parent_step_key_hash"],
                )
            if not _check_exists("ck_agent_runs_child_parent_step_hash"):
                batch.create_check_constraint(
                    "ck_agent_runs_child_parent_step_hash",
                    "parent_run_id IS NULL OR parent_step_key_hash IS NOT NULL",
                )
        return
    for column in missing_columns:
        op.add_column("agent_runs", column)
    _backfill_parent_step_hashes()
    if not _constraint_exists("uq_agent_runs_parent_step_key_hash"):
        op.create_unique_constraint(
            "uq_agent_runs_parent_step_key_hash",
            "agent_runs",
            ["parent_run_id", "parent_step_key_hash"],
        )
    if not _check_exists("ck_agent_runs_child_parent_step_hash"):
        op.create_check_constraint(
            "ck_agent_runs_child_parent_step_hash",
            "agent_runs",
            "parent_run_id IS NULL OR parent_step_key_hash IS NOT NULL",
        )


def downgrade() -> None:
    drop_constraint = _constraint_exists("uq_agent_runs_parent_step_key_hash")
    drop_check = _check_exists("ck_agent_runs_child_parent_step_hash")
    drop_columns = [
        name
        for name in (
            "execution_attempt",
            "execution_token",
            "parent_step_key_hash",
        )
        if _column_exists(name)
    ]
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("agent_runs") as batch:
            if drop_constraint:
                batch.drop_constraint("uq_agent_runs_parent_step_key_hash", type_="unique")
            if drop_check:
                batch.drop_constraint("ck_agent_runs_child_parent_step_hash", type_="check")
            for column_name in drop_columns:
                batch.drop_column(column_name)
        return
    if drop_constraint:
        op.drop_constraint(
            "uq_agent_runs_parent_step_key_hash",
            "agent_runs",
            type_="unique",
        )
    if drop_check:
        op.drop_constraint(
            "ck_agent_runs_child_parent_step_hash",
            "agent_runs",
            type_="check",
        )
    for column_name in drop_columns:
        op.drop_column("agent_runs", column_name)
