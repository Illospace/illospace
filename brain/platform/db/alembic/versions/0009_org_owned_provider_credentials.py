"""Replace user API keys with org keys plus user Codex connections.

Revision ID: 0009_org_owned_provider_credentials
Revises: 0008_org_owned_vault
Create Date: 2026-05-20
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_org_owned_provider_credentials"
down_revision = "0008_org_owned_vault"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def _encrypted_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def _classify_legacy_user_key(provider: str, encrypted_key: Any) -> str:
    """Return codex_subscription or org_api_key for a legacy user key."""
    if provider != "openai":
        return "org_api_key"
    try:
        from brain.platform.integrations.openai_codex_auth import parse_codex_auth_payload
        from brain.systems.vault import _decrypt

        credential = parse_codex_auth_payload(_decrypt(_encrypted_bytes(encrypted_key)))
        if credential.auth_mode == "chatgpt":
            return "codex_subscription"
        return "org_api_key"
    except Exception as exc:
        raise RuntimeError(
            "Cannot classify legacy OpenAI user_api_keys during org credential migration. "
            "Set VAULT_MASTER_KEY so encrypted rows can be decrypted before upgrading."
        ) from exc


def _ensure_user_codex_connections() -> None:
    if _table_exists("user_codex_connections"):
        return
    op.create_table(
        "user_codex_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("encrypted_credential", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.String(length=100), server_default="Codex / ChatGPT", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_tokens_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_user_codex_connections_user"),
    )


def _row_exists(sql: str, params: dict[str, Any]) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def _migrate_legacy_user_keys() -> None:
    if not _table_exists("user_api_keys"):
        return

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT
                k.id,
                k.user_id,
                users.org_id,
                k.provider,
                k.encrypted_key,
                k.label,
                k.last_used_at,
                k.total_tokens_used,
                k.estimated_cost_usd,
                k.created_at
            FROM user_api_keys AS k
            JOIN users ON users.id = k.user_id
            WHERE k.is_active = TRUE
            ORDER BY k.created_at DESC, k.id DESC
            """
        )
    ).mappings().all()

    for row in rows:
        provider = str(row["provider"] or "").strip().lower()
        if not provider:
            continue
        encrypted_key = _encrypted_bytes(row["encrypted_key"])
        label = str(row["label"] or "").strip()
        created_at = row["created_at"] or datetime.utcnow()

        if _classify_legacy_user_key(provider, encrypted_key) == "codex_subscription":
            if _row_exists(
                "SELECT id FROM user_codex_connections WHERE user_id = :user_id LIMIT 1",
                {"user_id": row["user_id"]},
            ):
                continue
            bind.execute(
                sa.text(
                    """
                    INSERT INTO user_codex_connections (
                        user_id,
                        encrypted_credential,
                        label,
                        is_active,
                        last_used_at,
                        total_tokens_used,
                        estimated_cost_usd,
                        created_at
                    )
                    VALUES (
                        :user_id,
                        :encrypted_credential,
                        :label,
                        TRUE,
                        :last_used_at,
                        :total_tokens_used,
                        :estimated_cost_usd,
                        :created_at
                    )
                    """
                ),
                {
                    "user_id": row["user_id"],
                    "encrypted_credential": encrypted_key,
                    "label": label or "Codex / ChatGPT",
                    "last_used_at": row["last_used_at"],
                    "total_tokens_used": row["total_tokens_used"] or 0,
                    "estimated_cost_usd": row["estimated_cost_usd"] or 0,
                    "created_at": created_at,
                },
            )
            continue

        if _row_exists(
            "SELECT id FROM org_api_keys WHERE org_id = :org_id AND provider = :provider LIMIT 1",
            {"org_id": row["org_id"], "provider": provider},
        ):
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO org_api_keys (
                    org_id,
                    provider,
                    encrypted_key,
                    label,
                    last_used_at,
                    total_tokens_used,
                    estimated_cost_usd,
                    created_at
                )
                VALUES (
                    :org_id,
                    :provider,
                    :encrypted_key,
                    :label,
                    :last_used_at,
                    :total_tokens_used,
                    :estimated_cost_usd,
                    :created_at
                )
                """
            ),
            {
                "org_id": row["org_id"],
                "provider": provider,
                "encrypted_key": encrypted_key,
                "label": label or "main",
                "last_used_at": row["last_used_at"],
                "total_tokens_used": row["total_tokens_used"] or 0,
                "estimated_cost_usd": row["estimated_cost_usd"] or 0,
                "created_at": created_at,
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _ensure_user_codex_connections()
    _migrate_legacy_user_keys()

    if _table_exists("api_key_shares"):
        op.drop_table("api_key_shares")
    if _table_exists("user_api_keys"):
        op.drop_table("user_api_keys")
    _drop_column_if_exists("users", "default_api_key_id")
    _drop_column_if_exists("users", "default_provider")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    raise RuntimeError("0009_org_owned_provider_credentials is intentionally one-way")
