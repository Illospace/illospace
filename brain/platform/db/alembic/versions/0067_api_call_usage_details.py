"""Keep subscription routing and provider-reported reasoning usage per call.

Revision ID: 0067_api_call_usage_details
Revises: 0066_provider_alert_filing_claims
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_api_call_usage_details"
down_revision = "0066_provider_alert_filing_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh installations already use the current SQLAlchemy model baseline.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_api_calls")}
    for name, column_type in (("auth_mode", sa.Text()), ("service_tier", sa.Text()), ("reasoning_tokens", sa.Integer())):
        if name not in existing:
            op.add_column("agent_api_calls", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_api_calls")}
    for name in ("reasoning_tokens", "service_tier", "auth_mode"):
        if name in existing:
            op.drop_column("agent_api_calls", name)
