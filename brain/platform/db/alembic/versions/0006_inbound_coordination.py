"""Add inbound coordination tables.

Revision ID: 0006_inbound_coordination
Revises: 0005_project_profile_privacy
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_inbound_coordination"
down_revision = "0005_project_profile_privacy"
branch_labels = None
depends_on = None


INBOUND_TABLES = {
    "inbound_source_policies",
    "inbound_domain_projections",
    "inbound_events",
    "inbound_decision_receipts",
}


def _existing_inbound_tables() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names(schema="public"))
    return tables.intersection(INBOUND_TABLES)


def upgrade() -> None:
    existing_tables = _existing_inbound_tables()
    if existing_tables:
        missing_tables = INBOUND_TABLES.difference(existing_tables)
        if not missing_tables:
            return
        missing = ", ".join(sorted(missing_tables))
        existing = ", ".join(sorted(existing_tables))
        raise RuntimeError(
            "Refusing to apply partial inbound coordination migration state. "
            f"Existing tables: {existing}. Missing tables: {missing}."
        )

    op.create_table(
        "inbound_source_policies",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("origin_patterns", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("envelope_kinds", postgresql.JSONB(), nullable=False, server_default=sa.text("'[\"signal\"]'::jsonb")),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("schema_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("allowed_actions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("auto_execute_actions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("auto_execute_min_confidence", sa.Float(), nullable=False, server_default=sa.text("0.85")),
        sa.Column("review_mode", sa.String(length=30), nullable=False, server_default="review_required"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_inbound_source_policies_connection_priority",
        "inbound_source_policies",
        ["connection_id", "enabled", "priority"],
    )
    op.create_index(
        "ix_inbound_source_policies_org_enabled",
        "inbound_source_policies",
        ["org_id", "enabled"],
    )

    op.create_table(
        "inbound_domain_projections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("inbound_source_policies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("object_key", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("external_id_path", sa.Text(), nullable=False),
        sa.Column("external_id_field", sa.String(length=80), nullable=False),
        sa.Column("field_mapping", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("title_path", sa.Text(), nullable=True),
        sa.Column("upsert_mode", sa.String(length=30), nullable=False, server_default="upsert"),
        sa.Column("validation_failure_status", sa.String(length=30), nullable=False, server_default="review_required"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_inbound_domain_projections_policy",
        "inbound_domain_projections",
        ["policy_id", "enabled"],
    )
    op.create_index(
        "ix_inbound_domain_projections_domain",
        "inbound_domain_projections",
        ["domain_id", "object_key"],
    )

    op.create_table(
        "inbound_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("external_agent_connection_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("inbound_source_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "domain_projection_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("inbound_domain_projections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default="signal"),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("envelope", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ingress_context", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_actor", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("authority_user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="received"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("action_type", sa.String(length=80), nullable=True),
        sa.Column("action_result", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("connection_id", "idempotency_key", name="uq_inbound_events_connection_idempotency"),
    )
    op.create_index(
        "ix_inbound_events_connection_created",
        "inbound_events",
        ["connection_id", "created_at"],
    )
    op.create_index(
        "ix_inbound_events_origin_created",
        "inbound_events",
        ["org_id", "origin", "created_at"],
    )
    op.create_index(
        "ix_inbound_events_status_created",
        "inbound_events",
        ["org_id", "status", "created_at"],
    )

    op.create_table(
        "inbound_decision_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("inbound_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("inbound_source_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tool_use", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reasoning_summary", sa.Text(), nullable=True),
        sa.Column("reusable_pattern_candidate", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_inbound_decision_receipts_event",
        "inbound_decision_receipts",
        ["event_id", "created_at"],
    )
    op.create_index(
        "ix_inbound_decision_receipts_org_created",
        "inbound_decision_receipts",
        ["org_id", "created_at"],
    )


def downgrade() -> None:
    return None
