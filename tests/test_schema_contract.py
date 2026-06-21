"""Schema contract tests for the Alembic-migrated database."""

from sqlalchemy import text

from tests.conftest import requires_db


EXPECTED_WORKSPACE_APP_EVENT_COLUMNS = {
    "id",
    "org_id",
    "app_id",
    "thread_id",
    "event_type",
    "idempotency_key",
    "actor_kind",
    "actor_user_id",
    "actor_display",
    "payload",
    "state_key",
    "state_patch",
    "state_version",
    "metadata",
    "created_at",
}

EXPECTED_WORKSPACE_APP_EVENT_INDEXES = {
    "ix_workspace_app_events_org_app_created",
    "ix_workspace_app_events_app_type_created",
    "ix_workspace_app_events_thread_created",
}


async def _columns_for_table(db_session, table_name: str) -> dict[str, dict[str, str]]:
    result = await db_session.execute(
        text(
            """
            SELECT column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {
        row.column_name: {
            "is_nullable": row.is_nullable,
            "column_default": row.column_default or "",
        }
        for row in result
    }


@requires_db
async def test_workspace_app_collaboration_schema_is_applied(db_session):
    event_columns = await _columns_for_table(db_session, "workspace_app_events")
    assert EXPECTED_WORKSPACE_APP_EVENT_COLUMNS <= set(event_columns)
    assert event_columns["payload"]["is_nullable"] == "NO"
    assert event_columns["state_patch"]["is_nullable"] == "NO"
    assert event_columns["state_version"]["is_nullable"] == "NO"
    assert "0" in event_columns["state_version"]["column_default"]

    state_columns = await _columns_for_table(db_session, "workspace_app_states")
    assert "version" in state_columns
    assert state_columns["version"]["is_nullable"] == "NO"
    assert "0" in state_columns["version"]["column_default"]

    indexes_result = await db_session.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'workspace_app_events'
            """
        )
    )
    index_names = {row.indexname for row in indexes_result}
    assert EXPECTED_WORKSPACE_APP_EVENT_INDEXES <= index_names

    constraints_result = await db_session.execute(
        text(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = current_schema()
              AND table_name = 'workspace_app_events'
            """
        )
    )
    constraint_names = {row.constraint_name for row in constraints_result}
    assert "uq_workspace_app_events_app_idempotency" in constraint_names
