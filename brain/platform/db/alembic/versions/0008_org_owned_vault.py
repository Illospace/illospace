"""Rebuild vault ownership around orgs.

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

UUID = postgresql.UUID(as_uuid=False)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema="public"))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(table_name, schema="public")
    }


def _column_nullable(table_name: str, column_name: str) -> bool:
    for column in _inspector().get_columns(table_name, schema="public"):
        if column["name"] == column_name:
            return bool(column.get("nullable"))
    return False


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    constraints = _inspector().get_unique_constraints(table_name, schema="public")
    constraints += _inspector().get_foreign_keys(table_name, schema="public")
    return constraint_name in {constraint["name"] for constraint in constraints}


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(table_name, schema="public")
    }


def _add_uuid_fk_column(
    table_name: str,
    column_name: str,
    target: str,
    *,
    nullable: bool = True,
    ondelete: str = "SET NULL",
) -> None:
    if not _table_exists(table_name) or _column_exists(table_name, column_name):
        return
    op.add_column(
        table_name,
        sa.Column(
            column_name,
            UUID,
            sa.ForeignKey(target, ondelete=ondelete),
            nullable=nullable,
        ),
    )


def _drop_constraint_if_exists(table_name: str, constraint_name: str, type_: str) -> None:
    if _constraint_exists(table_name, constraint_name):
        op.drop_constraint(constraint_name, table_name, type_=type_)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


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
        and not _constraint_exists(table_name, constraint_name)
    ):
        op.create_unique_constraint(constraint_name, table_name, columns)


def _set_not_null(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name) and _column_nullable(table_name, column_name):
        op.alter_column(table_name, column_name, existing_type=UUID, nullable=False)


def _raise_if_rows(sql: str, label: str) -> None:
    rows = op.get_bind().execute(sa.text(sql)).mappings().all()
    if not rows:
        return
    examples = ", ".join("/".join(str(value) for value in row.values()) for row in rows[:10])
    raise RuntimeError(f"Cannot complete org-owned vault migration; unresolved {label}: {examples}")


def _raise_if_duplicate_rows(sql: str, label: str) -> None:
    rows = op.get_bind().execute(sa.text(sql)).mappings().all()
    if not rows:
        return
    examples = ", ".join("/".join(str(value) for value in row.values()) for row in rows[:10])
    raise RuntimeError(f"Cannot create org-owned vault uniqueness; duplicate {label}: {examples}")


def _normalize_secrets() -> None:
    if not _table_exists("secrets"):
        return
    _add_uuid_fk_column("secrets", "org_id", "orgs.id", ondelete="CASCADE")
    _add_uuid_fk_column("secrets", "created_by_user_id", "users.id")
    _add_uuid_fk_column("secrets", "updated_by_user_id", "users.id")
    if _column_exists("secrets", "user_id"):
        op.execute(
            sa.text(
                """
                UPDATE secrets AS secret
                SET org_id = COALESCE(secret.org_id, users.org_id),
                    created_by_user_id = COALESCE(secret.created_by_user_id, secret.user_id),
                    updated_by_user_id = COALESCE(secret.updated_by_user_id, secret.user_id)
                FROM users
                WHERE secret.user_id = users.id
                """
            )
        )
    _raise_if_rows(
        "SELECT id, key_name FROM secrets WHERE org_id IS NULL ORDER BY id LIMIT 10",
        "secrets without org_id",
    )
    _raise_if_duplicate_rows(
        """
        SELECT org_id::text AS org_id, key_name, count(*) AS duplicate_count
        FROM secrets
        GROUP BY org_id, key_name
        HAVING count(*) > 1
        ORDER BY org_id, key_name
        """,
        "secrets by org/key",
    )
    _drop_constraint_if_exists("secrets", "secrets_user_key_unique", "unique")
    _drop_column_if_exists("secrets", "user_id")
    _set_not_null("secrets", "org_id")
    _create_index_if_missing("secrets", "ix_secrets_org_id", ["org_id"])
    _create_unique_if_missing("secrets", "secrets_org_key_unique", ["org_id", "key_name"])


def _normalize_project_bindings() -> None:
    if not _table_exists("vault_project_bindings"):
        return
    _add_uuid_fk_column("vault_project_bindings", "org_id", "orgs.id", ondelete="CASCADE")
    _add_uuid_fk_column("vault_project_bindings", "created_by_user_id", "users.id")
    if _column_exists("vault_project_bindings", "user_id"):
        op.execute(
            sa.text(
                """
                UPDATE vault_project_bindings AS binding
                SET org_id = COALESCE(binding.org_id, users.org_id),
                    created_by_user_id = COALESCE(binding.created_by_user_id, binding.user_id)
                FROM users
                WHERE binding.user_id = users.id
                """
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE vault_project_bindings AS binding
            SET org_id = COALESCE(binding.org_id, secret.org_id)
            FROM secrets AS secret
            WHERE binding.secret_id = secret.id
            """
        )
    )
    _raise_if_rows(
        """
        SELECT id, project_slug, env_name
        FROM vault_project_bindings
        WHERE org_id IS NULL
        ORDER BY id
        LIMIT 10
        """,
        "project bindings without org_id",
    )
    _raise_if_duplicate_rows(
        """
        SELECT org_id::text AS org_id, project_slug, env_name, count(*) AS duplicate_count
        FROM vault_project_bindings
        GROUP BY org_id, project_slug, env_name
        HAVING count(*) > 1
        ORDER BY org_id, project_slug, env_name
        """,
        "project bindings by org/project/env",
    )
    _drop_constraint_if_exists(
        "vault_project_bindings",
        "uq_vault_project_bindings_user_project_env",
        "unique",
    )
    _drop_column_if_exists("vault_project_bindings", "user_id")
    _set_not_null("vault_project_bindings", "org_id")
    _create_index_if_missing("vault_project_bindings", "ix_vault_project_bindings_org_id", ["org_id"])
    _create_unique_if_missing(
        "vault_project_bindings",
        "uq_vault_project_bindings_org_project_env",
        ["org_id", "project_slug", "env_name"],
    )


def _normalize_access_log() -> None:
    if not _table_exists("vault_access_log"):
        return
    _add_uuid_fk_column("vault_access_log", "org_id", "orgs.id", ondelete="CASCADE")
    _add_uuid_fk_column("vault_access_log", "actor_user_id", "users.id")
    if _column_exists("vault_access_log", "user_id"):
        op.execute(
            sa.text(
                """
                UPDATE vault_access_log AS log
                SET actor_user_id = COALESCE(log.actor_user_id, log.user_id)
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE vault_access_log AS log
                SET org_id = COALESCE(log.org_id, users.org_id)
                FROM users
                WHERE log.user_id = users.id
                """
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE vault_access_log AS log
            SET org_id = COALESCE(log.org_id, secret.org_id)
            FROM secrets AS secret
            WHERE log.secret_id = secret.id
            """
        )
    )
    _raise_if_rows(
        "SELECT id, key_name FROM vault_access_log WHERE org_id IS NULL ORDER BY id LIMIT 10",
        "access log rows without org_id",
    )
    _drop_column_if_exists("vault_access_log", "user_id")
    _set_not_null("vault_access_log", "org_id")
    _create_index_if_missing("vault_access_log", "ix_vault_access_log_org_id", ["org_id"])


def _normalize_org_actor_table(table_name: str, actor_column: str) -> None:
    if not _table_exists(table_name):
        return
    _add_uuid_fk_column(table_name, "org_id", "orgs.id", ondelete="CASCADE")
    _add_uuid_fk_column(table_name, actor_column, "users.id")
    if _column_exists(table_name, "user_id"):
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name} AS row
                SET org_id = COALESCE(row.org_id, users.org_id),
                    {actor_column} = COALESCE(row.{actor_column}, row.user_id)
                FROM users
                WHERE row.user_id = users.id
                """
            )
        )
    _raise_if_rows(
        f"SELECT id FROM {table_name} WHERE org_id IS NULL ORDER BY id LIMIT 10",
        f"{table_name} rows without org_id",
    )
    _drop_column_if_exists(table_name, "user_id")
    _set_not_null(table_name, "org_id")
    _create_index_if_missing(table_name, f"ix_{table_name}_org_id", ["org_id"])


def _normalize_sessions() -> None:
    if not _table_exists("vault_sessions"):
        return
    _add_uuid_fk_column("vault_sessions", "org_id", "orgs.id", ondelete="CASCADE")
    _add_uuid_fk_column("vault_sessions", "actor_user_id", "users.id")
    if _column_exists("vault_sessions", "user_id"):
        op.execute(
            sa.text(
                """
                UPDATE vault_sessions AS session
                SET org_id = COALESCE(session.org_id, users.org_id),
                    actor_user_id = COALESCE(session.actor_user_id, session.user_id)
                FROM users
                WHERE session.user_id = users.id
                """
            )
        )
    _raise_if_rows(
        "SELECT token_hash FROM vault_sessions WHERE org_id IS NULL ORDER BY token_hash LIMIT 10",
        "vault sessions without org_id",
    )
    _drop_column_if_exists("vault_sessions", "user_id")
    _set_not_null("vault_sessions", "org_id")
    _create_index_if_missing("vault_sessions", "ix_vault_sessions_org_id", ["org_id"])


def _normalize_vault_config() -> None:
    if not _table_exists("vault_config"):
        return

    op.alter_column(
        "vault_config",
        "key",
        existing_type=sa.String(length=64),
        type_=sa.String(length=160),
        existing_nullable=False,
    )
    op.execute(
        sa.text(
            """
            UPDATE vault_config AS config
            SET key = 'pin:org:' || users.org_id::text || ':user:' || users.id::text
                || ':' || substring(config.key from ':(hash|failures|lockout)$')
            FROM users
            WHERE config.key IN (
                'pin:' || users.id::text || ':hash',
                'pin:' || users.id::text || ':failures',
                'pin:' || users.id::text || ':lockout'
            )
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _normalize_vault_config()
    _normalize_secrets()
    _normalize_project_bindings()
    _normalize_access_log()
    _normalize_org_actor_table("vault_agent_grants", "requested_by_user_id")
    _normalize_org_actor_table("vault_missing_requests", "actor_user_id")
    _normalize_sessions()

    if _table_exists("vault_shares"):
        op.drop_table("vault_shares")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    raise RuntimeError("0008_org_owned_vault is intentionally one-way")
