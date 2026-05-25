"""Public schema baseline.

Revision ID: 0001_public_schema_baseline
Revises: 0006_user_notification_preferences
Create Date: 2026-05-07
"""

from __future__ import annotations

from alembic import op


revision = "0001_public_schema_baseline"
down_revision = "0006_user_notification_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            """
            DO $$
            BEGIN
                EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements';
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'Skipping pg_stat_statements extension: %', SQLERRM;
            END $$;
            """
        )

    from brain.platform.db.base import Base
    import brain.platform.db.models  # noqa: F401

    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from brain.platform.db.base import Base
    import brain.platform.db.models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())
