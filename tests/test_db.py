"""Tests for db.py — connection pool and context managers."""

import os
import sys
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def test_get_cursor_yields_dict_cursor():
    """get_cursor should yield a RealDictCursor and commit on success."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("brain.platform.db.legacy._get_pool", return_value=mock_pool):
        import brain.platform.db.legacy as db
        # Reset the module-level pool
        with db.get_cursor() as cur:
            assert cur is mock_cur

        mock_conn.commit.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)


def test_get_cursor_rollback_on_error():
    """get_cursor should rollback on exception."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("brain.platform.db.legacy._get_pool", return_value=mock_pool):
        import brain.platform.db.legacy as db
        try:
            with db.get_cursor() as cur:
                raise ValueError("test error")
        except ValueError:
            pass

        mock_conn.rollback.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)


def test_get_conn_yields_connection():
    """get_conn should yield the raw connection."""
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("brain.platform.db.legacy._get_pool", return_value=mock_pool):
        import brain.platform.db.legacy as db
        with db.get_conn() as conn:
            assert conn is mock_conn

        mock_conn.commit.assert_called_once()


def test_close_pool():
    """close_pool should close all connections and reset _pool."""
    import brain.platform.db.legacy as db
    mock_pool = MagicMock()
    db._pool = mock_pool
    db.close_pool()
    mock_pool.closeall.assert_called_once()
    assert db._pool is None


def test_close_pool_when_none():
    """close_pool should be safe when pool is None."""
    import brain.platform.db.legacy as db
    db._pool = None
    db.close_pool()  # Should not raise
    assert db._pool is None
