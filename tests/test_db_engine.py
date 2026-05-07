"""Engine and session infrastructure."""
import warnings
from unittest.mock import MagicMock, patch

from brain.platform.db import SessionFactory, engine, get_cursor, get_conn


def test_engine_exists():
    """SQLAlchemy engine is created at import time."""
    assert engine is not None
    assert "postgresql" in str(engine.url)


def test_session_factory_exists():
    assert SessionFactory is not None


def test_get_cursor_emits_deprecation():
    """get_cursor remains as a deprecated transitional shim."""
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with patch("brain.platform.db._get_pool", return_value=mock_pool):
            with get_cursor(commit=False) as cur:
                assert cur is mock_conn.cursor.return_value
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()


def test_get_conn_emits_deprecation():
    """get_conn remains as a deprecated transitional shim."""
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with patch("brain.platform.db._get_pool", return_value=mock_pool):
            with get_conn(commit=False) as conn:
                assert conn is mock_conn
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()
