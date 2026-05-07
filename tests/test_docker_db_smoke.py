"""Smoke test for Docker test DB fixtures.

Only runs when TEST_DB_URL is set (via scripts/test-with-db.sh).
"""
import pytest
from tests.conftest import requires_db


@requires_db
class TestDockerDB:
    def test_connection_works(self, db_session):
        from sqlalchemy import text
        result = db_session.execute(text("SELECT 1 AS n")).scalar()
        assert result == 1

    def test_pgvector_extension(self, db_session):
        from sqlalchemy import text
        result = db_session.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        assert result == "vector"

    def test_session_rollback_isolation(self, db_session):
        """Insert a row via raw SQL (no FK checks needed), verify it exists within the transaction."""
        from sqlalchemy import text
        db_session.execute(text(
            "INSERT INTO memory_health_log (check_type, status) VALUES ('test', 'passed')"
        ))
        count = db_session.execute(text("SELECT count(*) FROM memory_health_log")).scalar()
        assert count == 1  # Visible within this transaction

    def test_no_leaked_data(self, db_session):
        """Confirm the previous test's insert was rolled back."""
        from sqlalchemy import text
        count = db_session.execute(text("SELECT count(*) FROM memory_health_log")).scalar()
        assert count == 0
