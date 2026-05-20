"""Scope vault secrets by org.

Revision ID: 0008_org_owned_vault
Revises: 0007_backfill_signal_submit_scope
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0008_org_owned_vault"
down_revision = "0007_backfill_signal_submit_scope"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema="public"))


def _column_exists(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(table_name, schema="public")
    }


def _unique_constraint_exists(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_unique_constraints(table_name, schema="public")
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(table_name, schema="public")
    }


def _add_org_id_column(table_name: str) -> None:
    if not _table_exists(table_name) or _column_exists(table_name, "org_id"):
        return
    op.add_column(
        table_name,
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if (
        _table_exists(table_name)
        and all(_column_exists(table_name, column) for column in columns)
        and not _index_exists(table_name, index_name)
    ):
        op.create_index(index_name, table_name, columns)


def _create_unique_if_missing(table_name: str, constraint_name: str, columns: list[str]) -> None:
    if (
        _table_exists(table_name)
        and all(_column_exists(table_name, column) for column in columns)
        and not _unique_constraint_exists(table_name, constraint_name)
    ):
        op.create_unique_constraint(constraint_name, table_name, columns)


def _raise_if_duplicate_rows(sql: str, label: str) -> None:
    rows = op.get_bind().execute(sa.text(sql)).mappings().all()
    if not rows:
        return
    examples = ", ".join(
        "/".join(str(value) for value in row.values())
        for row in rows[:10]
    )
    raise RuntimeError(f"Cannot create org-scoped vault uniqueness; duplicate {label}: {examples}")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in ("secrets", "vault_project_bindings", "vault_access_log"):
        _add_org_id_column(table_name)

    if _table_exists("secrets") and _table_exists("users") and _column_exists("secrets", "org_id"):
        op.execute(
            sa.text(
                """
                UPDATE secrets AS secret
                SET org_id = users.org_id
                FROM users
                WHERE secret.user_id = users.id
                  AND secret.org_id IS NULL
                  AND users.org_id IS NOT NULL
                """
            )
        )

    if (
        _table_exists("vault_project_bindings")
        and _table_exists("users")
        and _column_exists("vault_project_bindings", "org_id")
    ):
        op.execute(
            sa.text(
                """
                UPDATE vault_project_bindings AS binding
                SET org_id = users.org_id
                FROM users
                WHERE binding.user_id = users.id
                  AND binding.org_id IS NULL
                  AND users.org_id IS NOT NULL
                """
            )
        )

    if _table_exists("vault_access_log") and _column_exists("vault_access_log", "org_id"):
        if _table_exists("secrets"):
            op.execute(
                sa.text(
                    """
                    UPDATE vault_access_log AS log
                    SET org_id = secret.org_id
                    FROM secrets AS secret
                    WHERE log.secret_id = secret.id
                      AND log.org_id IS NULL
                      AND secret.org_id IS NOT NULL
                    """
                )
            )
        if _table_exists("users"):
            op.execute(
                sa.text(
                    """
                    UPDATE vault_access_log AS log
                    SET org_id = users.org_id
                    FROM users
                    WHERE log.user_id = users.id
                      AND log.org_id IS NULL
                      AND users.org_id IS NOT NULL
                    """
                )
            )

    if _table_exists("secrets") and _column_exists("secrets", "org_id"):
        _raise_if_duplicate_rows(
            """
            SELECT org_id::text AS org_id, key_name, count(*) AS duplicate_count
            FROM secrets
            WHERE org_id IS NOT NULL
            GROUP BY org_id, key_name
            HAVING count(*) > 1
            ORDER BY org_id, key_name
            """,
            "secrets by org/key",
        )
    if _table_exists("vault_project_bindings") and _column_exists("vault_project_bindings", "org_id"):
        _raise_if_duplicate_rows(
            """
            SELECT org_id::text AS org_id, project_slug, env_name, count(*) AS duplicate_count
            FROM vault_project_bindings
            WHERE org_id IS NOT NULL
            GROUP BY org_id, project_slug, env_name
            HAVING count(*) > 1
            ORDER BY org_id, project_slug, env_name
            """,
            "project bindings by org/project/env",
        )

    _create_index_if_missing("secrets", "ix_secrets_org_id", ["org_id"])
    _create_index_if_missing("vault_access_log", "ix_vault_access_log_org_id", ["org_id"])
    _create_unique_if_missing("secrets", "secrets_org_key_unique", ["org_id", "key_name"])
    _create_unique_if_missing(
        "vault_project_bindings",
        "uq_vault_project_bindings_org_project_env",
        ["org_id", "project_slug", "env_name"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    if _table_exists("vault_project_bindings") and _unique_constraint_exists(
        "vault_project_bindings",
        "uq_vault_project_bindings_org_project_env",
    ):
        op.drop_constraint(
            "uq_vault_project_bindings_org_project_env",
            "vault_project_bindings",
            type_="unique",
        )
    if _table_exists("secrets") and _unique_constraint_exists("secrets", "secrets_org_key_unique"):
        op.drop_constraint("secrets_org_key_unique", "secrets", type_="unique")
    if _table_exists("vault_access_log") and _index_exists("vault_access_log", "ix_vault_access_log_org_id"):
        op.drop_index("ix_vault_access_log_org_id", table_name="vault_access_log")
    if _table_exists("secrets") and _index_exists("secrets", "ix_secrets_org_id"):
        op.drop_index("ix_secrets_org_id", table_name="secrets")
    if _table_exists("vault_access_log") and _column_exists("vault_access_log", "org_id"):
        op.drop_column("vault_access_log", "org_id")
    if _table_exists("secrets") and _column_exists("secrets", "org_id"):
        op.drop_column("secrets", "org_id")
