"""Alembic env.py — uses SQLAlchemy models for autogenerate."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from brain.kernel import config as brain_config
from brain.platform.db.base import Base
from brain.platform.db.models import *  # noqa: register all models with Base.metadata

target_metadata = Base.metadata
ALEMBIC_VERSION_NUM_MAX_LENGTH = 255

alembic_cfg = context.config
if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    context.configure(
        url=brain_config.DB_SYNC_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _ensure_alembic_version_capacity(connection: Connection) -> None:
    """Allow long revision ids on existing Postgres databases."""
    if connection.dialect.name != "postgresql":
        return

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR(%d) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
            """
            % ALEMBIC_VERSION_NUM_MAX_LENGTH
        )
    )

    current_length = connection.execute(
        text(
            """
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'alembic_version'
              AND column_name = 'version_num'
            """
        )
    ).scalar_one_or_none()

    if current_length is not None and current_length < ALEMBIC_VERSION_NUM_MAX_LENGTH:
        connection.execute(
            text(
                "ALTER TABLE alembic_version "
                f"ALTER COLUMN version_num TYPE VARCHAR({ALEMBIC_VERSION_NUM_MAX_LENGTH})"
            )
        )

    if connection.in_transaction():
        connection.commit()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = create_engine(brain_config.DB_SYNC_URL)
    try:
        with connectable.connect() as connection:
            _ensure_alembic_version_capacity(connection)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
