"""MCP migration graph and PostgreSQL upgrade/downgrade checks."""
import importlib
import io
from types import SimpleNamespace
import uuid

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text


def test_external_mcp_migration_graph_has_one_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == ["0071_external_mcp_servers"]
    assert scripts.get_revision("0071_external_mcp_servers").down_revision == "0070_tool_policies"


@pytest.mark.requires_db
async def test_external_mcp_migration_upgrade_existing_database(db_session, monkeypatch):
    migration = importlib.import_module("brain.platform.db.alembic.versions.0071_external_mcp_servers")
    schema = "mcp_test_" + uuid.uuid4().hex
    # Use the async test session for DDL, as the shared test fixtures do.
    await db_session.execute(text(f'CREATE SCHEMA "{schema}"'))
    await db_session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    for statement in ("CREATE TABLE orgs (id UUID PRIMARY KEY)",
                      "CREATE TABLE users (id UUID PRIMARY KEY)",
                      "CREATE TABLE agent_runs (id INTEGER PRIMARY KEY)"):
        await db_session.execute(text(statement))

    async def table_names():
        return list((await db_session.scalars(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = :schema"
        ), {"schema": schema})).all())

    async def apply(operation):
        existing = await table_names()
        # Alembic emits PostgreSQL DDL offline. The async session executes it below.
        monkeypatch.setattr(migration.sa, "inspect", lambda _: SimpleNamespace(get_table_names=lambda: existing))
        output = io.StringIO()
        context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
        with Operations.context(context):
            operation()
        for statement in output.getvalue().split(";"):
            if statement.strip():
                await db_session.execute(text(statement))

    await apply(migration.upgrade)
    await apply(migration.upgrade)
    assert {"external_mcp_servers", "external_mcp_audit_events"} <= set(await table_names())
    columns = set((await db_session.scalars(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = 'external_mcp_audit_events'"
    ), {"schema": schema})).all())
    assert not {"arguments", "result", "token"} & columns
    await apply(migration.downgrade)
    assert "external_mcp_servers" not in await table_names()
    await db_session.execute(text("SET LOCAL search_path TO public"))
