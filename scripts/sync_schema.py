#!/usr/bin/env python3
"""
Compare SQLAlchemy models against the live database and apply missing
columns AND missing tables.

Safe to run multiple times — only adds what's missing, never drops anything.
Preserves all existing data.

Usage:
    ./venv/bin/python3 scripts/sync_schema.py          # dry-run (show what would change)
    ./venv/bin/python3 scripts/sync_schema.py --apply   # actually apply changes
"""
import argparse
import asyncio
import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable
from brain.kernel.config import DB_URL as DATABASE_URL
from brain.platform.db.models import *  # noqa — registers all models with Base.metadata
from brain.platform.db.base import Base


def get_sqlalchemy_type_ddl(col, dialect):
    """Convert a SQLAlchemy column type to a Postgres DDL string."""
    from sqlalchemy import Integer, String, Text, Boolean, Float, Double, Numeric, DateTime
    from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID
    try:
        from pgvector.sqlalchemy import Vector
    except ImportError:
        Vector = None

    t = col.type

    if Vector and isinstance(t, Vector):
        return f"vector({t.dim})"
    if isinstance(t, ARRAY):
        item_type = _simple_type(t.item_type)
        return f"{item_type}[]"
    if isinstance(t, JSONB):
        return "jsonb"
    if isinstance(t, Numeric):
        if t.precision and t.scale:
            return f"numeric({t.precision},{t.scale})"
        return "numeric"
    if isinstance(t, Boolean):
        return "boolean"
    if isinstance(t, (Float, Double)):
        return "double precision"
    if isinstance(t, Integer):
        return "integer"
    if isinstance(t, String):
        return f"varchar({t.length})" if t.length else "text"
    if isinstance(t, Text):
        return "text"
    if isinstance(t, DateTime):
        return "timestamp with time zone" if t.timezone else "timestamp"

    type_name = type(t).__name__.lower()
    if "uuid" in type_name:
        return "uuid"

    try:
        return str(t.compile(dialect=dialect))
    except Exception:
        return "text"


def _simple_type(t):
    from sqlalchemy import Integer, String, Text
    if isinstance(t, (Text, String)):
        return "text"
    if isinstance(t, Integer):
        return "integer"
    return str(t)


def get_default_clause(col):
    """Get DEFAULT clause for a column if it has a server_default."""
    if col.server_default is not None:
        sd = col.server_default
        if hasattr(sd, "arg"):
            val = sd.arg.text if hasattr(sd.arg, "text") else str(sd.arg)
            # If it's a plain string (not already a SQL expression), quote it
            if not hasattr(sd.arg, "text") and not val.startswith("'") and not val.startswith("("):
                # Check if it looks like a SQL keyword/function
                sql_keywords = {"NOW()", "TRUE", "FALSE", "NULL", "gen_random_uuid()"}
                if val.upper() not in sql_keywords and not val.replace(".", "").replace("-", "").isdigit():
                    val = f"'{val}'"
            return f" DEFAULT {val}"
    return ""


def get_not_null_clause(col):
    if not col.nullable and not col.primary_key:
        return " NOT NULL"
    return ""


def safe_not_null(type_ddl, default, not_null):
    """If NOT NULL with no default, add a safe default for existing rows."""
    if not not_null or default:
        return default, not_null
    if "boolean" in type_ddl:
        return " DEFAULT false", not_null
    elif "integer" in type_ddl:
        return " DEFAULT 0", not_null
    elif "varchar" in type_ddl or type_ddl == "text":
        return " DEFAULT ''", not_null
    elif "jsonb" in type_ddl:
        return " DEFAULT '[]'::jsonb", not_null
    elif "timestamp" in type_ddl:
        return " DEFAULT now()", not_null
    elif "uuid" in type_ddl:
        return "", ""  # Can't add NOT NULL UUID without a real value
    elif "double" in type_ddl or "numeric" in type_ddl:
        return " DEFAULT 0", not_null
    return default, not_null


async def _inspect_schema(conn):
    table_result = await conn.execute(text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_type = 'BASE TABLE'
    """))
    existing_tables = {row.table_name for row in table_result}

    column_result = await conn.execute(text("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
    """))
    existing_columns = {table_name: [] for table_name in existing_tables}
    for row in column_result:
        existing_columns.setdefault(row.table_name, []).append({"name": row.column_name})

    return existing_tables, existing_columns


async def main():
    parser = argparse.ArgumentParser(description="Sync DB schema to match SQLAlchemy models")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    args = parser.parse_args()

    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            existing_tables, existing_columns = await _inspect_schema(conn)
        dialect = engine.sync_engine.dialect

        model_tables = Base.metadata.tables

        table_creates = []
        column_adds = []

        # ── Phase 1: Missing tables ──────────────────────────────────
        for table_name, table in sorted(model_tables.items()):
            if table_name not in existing_tables:
                # Generate CREATE TABLE from the model
                table_creates.append(table_name)

        if table_creates:
            print(f"\n🆕 {len(table_creates)} missing table(s):")
            for tn in table_creates:
                print(f"   + {tn}")

        # ── Phase 2: Missing columns ─────────────────────────────────
        for table_name, table in sorted(model_tables.items()):
            if table_name not in existing_tables:
                continue  # handled in phase 1

            existing_cols = {c["name"]: c for c in existing_columns[table_name]}
            model_cols = {c.name: c for c in table.columns}

            missing = []
            for col_name, col in model_cols.items():
                if col_name not in existing_cols:
                    type_ddl = get_sqlalchemy_type_ddl(col, dialect)
                    default = get_default_clause(col)
                    not_null = get_not_null_clause(col)
                    default, not_null = safe_not_null(type_ddl, default, not_null)

                    stmt = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {type_ddl}{default}{not_null};"
                    column_adds.append(stmt)
                    missing.append(col_name)

            if missing:
                print(f"\n📋 {table_name}: {len(missing)} missing column(s)")
                for name in missing:
                    print(f"   + {name}")

        total = len(table_creates) + len(column_adds)
        if total == 0:
            print("\n✅ Database schema is up to date — no changes needed.")
            return

        print(f"\n{'=' * 60}")
        print(f"Total: {len(table_creates)} table(s) + {len(column_adds)} column(s) to add")
        print(f"{'=' * 60}\n")

        if args.apply:
            print("🔧 Applying changes...\n")

            # Create missing tables using SQLAlchemy metadata
            if table_creates:
                print("  Creating missing tables...")
                tables_to_create = [
                    table for table in Base.metadata.sorted_tables
                    if table.name in table_creates
                ]
                async with engine.begin() as conn:
                    for table in tables_to_create:
                        await conn.execute(CreateTable(table, if_not_exists=True))
                        print(f"    ✓ {table.name}")

            # Add missing columns
            if column_adds:
                print("\n  Adding missing columns...")
                async with engine.begin() as conn:
                    for stmt in column_adds:
                        print(f"    {stmt}")
                        await conn.execute(text(stmt))

            print("\n✅ All changes applied successfully.")
            print("   Restart ./illo start to pick up the new schema.")
        else:
            print("DRY RUN — changes that would be made:\n")
            if table_creates:
                print("  Tables to create:")
                for tn in table_creates:
                    print(f"    CREATE TABLE {tn}")
            if column_adds:
                print("\n  Columns to add:")
                for stmt in column_adds:
                    print(f"    {stmt}")
            print(f"\nRe-run with --apply to execute.")
            print("  ./venv/bin/python3 scripts/sync_schema.py --apply")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
