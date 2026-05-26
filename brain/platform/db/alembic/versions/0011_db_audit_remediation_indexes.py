"""Apply DB audit remediation indexes and integrity checks.

Revision ID: 0011_db_audit_remediation_indexes
Revises: 0010_thread_context_and_discussion
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_db_audit_remediation_indexes"
down_revision = "0010_thread_context_and_discussion"
branch_labels = None
depends_on = None


def _sql_string_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_string_list(values: tuple[str, ...]) -> str:
    return ", ".join(_sql_string_literal(value) for value in values)


def _check_in_constraint(column_name: str, values: tuple[str, ...]) -> str:
    if not values:
        raise ValueError("check constraint values cannot be empty")
    return f"{column_name} IN ({_sql_string_list(values)})"


# Historical migration snapshot. Keep these values frozen; future status changes
# should alter constraints in a new revision rather than mutating this one.
MIGRATION_AGENT_RUN_DB_STATUS_VALUES = (
    "queued",
    "starting",
    "running",
    "paused",
    "verifying",
    "completed",
    "failed",
    "canceled",
    "expired",
    "cancelled",
    "error",
    "blocked",
)
MIGRATION_IDEA_STATUS_VALUES = (
    "emerged",
    "queued",
    "active",
    "working",
    "needs_input",
    "unread_reply",
    "blocked",
    "failed",
    "resolved",
    "stale",
    "paused",
    "done",
    "archived",
    "exploring",
    "building",
    "testing",
)
MIGRATION_INBOUND_EVENT_STATUS_VALUES = (
    "received",
    "processed",
    "review_required",
    "quarantined",
    "failed",
)
MIGRATION_EXTERNAL_AGENT_TASK_STATUS_VALUES = (
    "queued",
    "claimed",
    "running",
    "submitted",
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "blocked",
    "expired",
)

STATUS_CHECKS = {
    "agent_runs": (
        "ck_agent_runs_status",
        _check_in_constraint("status", MIGRATION_AGENT_RUN_DB_STATUS_VALUES),
    ),
    "ideas": (
        "ck_ideas_status",
        _check_in_constraint("status", MIGRATION_IDEA_STATUS_VALUES),
    ),
    "inbound_events": (
        "ck_inbound_events_status",
        _check_in_constraint("status", MIGRATION_INBOUND_EVENT_STATUS_VALUES),
    ),
    "external_agent_tasks": (
        "ck_external_agent_tasks_status",
        _check_in_constraint("status", MIGRATION_EXTERNAL_AGENT_TASK_STATUS_VALUES),
    ),
}


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _try_create_pg_stat_statements_extension() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements';
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'Skipping pg_stat_statements extension: %', SQLERRM;
            END $$;
            """
        )
    )


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in _inspector().get_table_names(schema=_schema())


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        index.get("name") == index_name
        for index in _inspector().get_indexes(table_name, schema=_schema())
    )


def _foreign_key_exists(
    table_name: str,
    constraint_name: str,
    *,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> bool:
    if not _table_exists(table_name):
        return False
    for foreign_key in _inspector().get_foreign_keys(table_name, schema=_schema()):
        if foreign_key.get("name") == constraint_name:
            return True
        if (
            foreign_key.get("constrained_columns") == constrained_columns
            and foreign_key.get("referred_table") == referred_table
            and foreign_key.get("referred_columns") == referred_columns
        ):
            return True
    return False


def _check_constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        constraint.get("name") == constraint_name
        for constraint in _inspector().get_check_constraints(table_name, schema=_schema())
    )


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _create_index_if_missing(
    index_name: str,
    table_name: str,
    columns: list[str],
    **dialect_kwargs: object,
) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns, **dialect_kwargs)


def _create_agent_api_calls_fk() -> None:
    constraint_name = "fk_agent_api_calls_run_id_agent_runs"
    if not (_table_exists("agent_api_calls") and _table_exists("agent_runs")):
        return
    if _foreign_key_exists(
        "agent_api_calls",
        constraint_name,
        constrained_columns=["run_id"],
        referred_table="agent_runs",
        referred_columns=["id"],
    ):
        return
    if not _is_postgresql():
        return

    op.execute(
        sa.text(
            """
            UPDATE agent_api_calls calls
            SET run_id = NULL
            WHERE run_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_runs runs
                  WHERE runs.id = calls.run_id
              )
            """
        )
    )
    op.create_foreign_key(
        constraint_name,
        "agent_api_calls",
        "agent_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _create_check_constraint_if_missing(table_name: str, constraint_name: str, expression: str) -> None:
    if not _table_exists(table_name) or _check_constraint_exists(table_name, constraint_name):
        return

    if _is_postgresql():
        op.execute(
            sa.text(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({expression}) NOT VALID"
            )
        )


def upgrade() -> None:
    if _is_postgresql():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        _try_create_pg_stat_statements_extension()

    _create_agent_api_calls_fk()
    _create_index_if_missing(
        "ix_agent_api_calls_run_created",
        "agent_api_calls",
        ["run_id", "created_at"],
    )
    _create_index_if_missing(
        "ix_agent_api_calls_created_run",
        "agent_api_calls",
        ["created_at", "run_id"],
    )
    _create_index_if_missing(
        "ix_idea_state_log_idea_changed",
        "idea_state_log",
        ["idea_id", "changed_at", "id"],
    )

    _drop_index_if_exists("domain_records", "ix_domain_records_search_text")
    if _is_postgresql():
        _create_index_if_missing(
            "ix_domain_records_search_text_trgm",
            "domain_records",
            ["search_text"],
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        )
    else:
        _create_index_if_missing(
            "ix_domain_records_search_text_trgm",
            "domain_records",
            ["search_text"],
        )

    _drop_index_if_exists("chat_conversation_members", "ix_chat_conversation_members_conversation_user")
    _drop_index_if_exists("chat_messages", "ix_chat_messages_conversation_seq")

    for table_name, (constraint_name, expression) in STATUS_CHECKS.items():
        _create_check_constraint_if_missing(table_name, constraint_name, expression)


def downgrade() -> None:
    return None
