"""Remove intelligence-tier model routing.

Revision ID: 0021_remove_intelligence_tiers
Revises: 0020_reconstructive_memory
Create Date: 2026-06-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0021_remove_intelligence_tiers"
down_revision = "0020_reconstructive_memory"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name, schema=_schema()))


def _uuid_type():
    return postgresql.UUID(as_uuid=False) if op.get_bind().dialect.name == "postgresql" else sa.String()


def _migrate_openai_default_models() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if not _table_exists("org_provider_model_mappings") or not _table_exists("orgs"):
        return
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    org_id,
                    model_name,
                    ROW_NUMBER() OVER (
                        PARTITION BY org_id
                        ORDER BY CASE intelligence_level
                            WHEN 'medium' THEN 0
                            WHEN 'high' THEN 1
                            WHEN 'low' THEN 2
                            ELSE 3
                        END
                    ) AS rn
                FROM org_provider_model_mappings
                WHERE provider = 'openai'
                  AND model_name IS NOT NULL
                  AND btrim(model_name) <> ''
            )
            UPDATE orgs
            SET memory_model_config =
                COALESCE(memory_model_config, '{}'::jsonb)
                || jsonb_build_object(
                    'default_provider', 'openai',
                    'default_model',
                    CASE
                        WHEN ranked.model_name LIKE 'openai/%' THEN ranked.model_name
                        WHEN ranked.model_name LIKE 'openai:%' THEN replace(ranked.model_name, 'openai:', 'openai/')
                        ELSE 'openai/' || ranked.model_name
                    END
                )
            FROM ranked
            WHERE ranked.rn = 1
              AND orgs.id = ranked.org_id
            """
        )
    )


def upgrade() -> None:
    _migrate_openai_default_models()
    if _column_exists("skills", "model_tier"):
        op.drop_column("skills", "model_tier")
    if _table_exists("org_provider_model_mappings"):
        op.drop_table("org_provider_model_mappings")


def downgrade() -> None:
    if not _table_exists("org_provider_model_mappings"):
        op.create_table(
            "org_provider_model_mappings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("intelligence_level", sa.String(length=20), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
            sa.UniqueConstraint(
                "org_id",
                "provider",
                "intelligence_level",
                name="uq_org_provider_model_mappings_org_provider_level",
            ),
        )
        op.create_index("ix_org_provider_model_mappings_org_id", "org_provider_model_mappings", ["org_id"])
    if _table_exists("skills") and not _column_exists("skills", "model_tier"):
        op.add_column(
            "skills",
            sa.Column("model_tier", sa.String(length=20), server_default="medium", nullable=False),
        )
