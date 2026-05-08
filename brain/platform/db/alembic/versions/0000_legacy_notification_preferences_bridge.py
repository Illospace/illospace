"""Legacy notification preferences compatibility bridge.

Revision ID: 0006_user_notification_preferences
Revises:
Create Date: 2026-05-08
"""

from __future__ import annotations


revision = "0006_user_notification_preferences"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing illo-brain databases may already be stamped at this legacy head."""


def downgrade() -> None:
    """No schema changes are associated with the legacy stamp."""
