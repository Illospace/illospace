import pytest
import time
from unittest.mock import MagicMock
from fastapi import HTTPException
from brain.app.api.deps import rate_limit, _rate_store, _is_internal


@pytest.fixture(autouse=True)
def clear_rate_store():
    _rate_store.clear()
    yield
    _rate_store.clear()


def test_is_internal():
    assert _is_internal("127.0.0.1")
    assert _is_internal("::1")
    assert _is_internal("[::1]")
    assert _is_internal("100.64.0.1")
    assert _is_internal("100.93.9.74")    # Tailscale/CGNAT range
    assert _is_internal("100.127.255.254")
    assert not _is_internal("100.63.255.255")
    assert not _is_internal("100.128.0.1")
    assert not _is_internal("10.0.0.1")
    assert not _is_internal("localhost")
    assert not _is_internal(None)


def test_rate_limit_allows_localhost():
    mock_request = MagicMock()
    mock_request.client.host = "127.0.0.1"
    rate_limit(mock_request)  # Should not raise


def test_rate_limit_blocks_excess():
    now = time.time()
    _rate_store["10.0.0.1"] = [now] * 300
    mock_request = MagicMock()
    mock_request.client.host = "10.0.0.1"
    with pytest.raises(HTTPException) as exc_info:
        rate_limit(mock_request)
    assert exc_info.value.status_code == 429
