"""Reserve one canonical GitHub filing per provider-alert signature.

Revision ID: 0066_provider_alert_filing_claims
Revises: 0065_enable_automatic_reclamation
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0066_provider_alert_filing_claims"
down_revision = "0065_enable_automatic_reclamation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The public baseline already materializes current models on fresh installs.
    if sa.inspect(op.get_bind()).has_table("provider_alert_filing_claims"):
        return
    op.create_table(
        "provider_alert_filing_claims",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=False).with_variant(sa.String(), "sqlite"),
                  sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service", sa.String(120), nullable=False),
        sa.Column("subsystem", sa.String(120), nullable=False),
        sa.Column("tracked_signature", sa.String(64), nullable=False),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("state", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("org_id", "service", "subsystem", "tracked_signature",
                            name="uq_provider_alert_filing_signature"),
    )
    op.create_index("ix_provider_alert_filing_pending", "provider_alert_filing_claims", ["state", "claimed_at"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("provider_alert_filing_claims"):
        return
    op.drop_index("ix_provider_alert_filing_pending", table_name="provider_alert_filing_claims")
    op.drop_table("provider_alert_filing_claims")
