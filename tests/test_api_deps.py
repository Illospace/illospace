import pytest
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from fastapi import HTTPException
from brain.app.api.config import RATE_LIMIT, RATE_LIMIT_GLOBAL
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
    mock_request = _request("10.0.0.1", path="/api/cortex/bootstrap")
    _rate_store["route:ip:10.0.0.1:GET /api/cortex/bootstrap"] = [now] * RATE_LIMIT
    with pytest.raises(HTTPException) as exc_info:
        rate_limit(mock_request)
    assert exc_info.value.status_code == 429


def test_rate_limit_uses_authenticated_user_not_shared_ip():
    now = time.time()
    _rate_store["route:user:user-a:GET /api/cortex/bootstrap"] = [now] * RATE_LIMIT

    blocked = _request("10.0.0.1", path="/api/cortex/bootstrap", user_id="user-a")
    allowed = _request("10.0.0.1", path="/api/cortex/bootstrap", user_id="user-b")

    with pytest.raises(HTTPException):
        rate_limit(blocked)
    rate_limit(allowed)


def test_rate_limit_uses_route_buckets_before_global_bucket():
    now = time.time()
    _rate_store["route:user:user-a:GET /api/cortex/bootstrap"] = [now] * RATE_LIMIT

    request = _request("10.0.0.1", path="/api/cortex/ideas", user_id="user-a")

    rate_limit(request)


def test_rate_limit_global_bucket_still_caps_authenticated_users():
    now = time.time()
    _rate_store["global:user:user-a"] = [now] * RATE_LIMIT_GLOBAL
    request = _request("10.0.0.1", path="/api/cortex/ideas", user_id="user-a")

    with pytest.raises(HTTPException) as exc_info:
        rate_limit(request)

    assert exc_info.value.status_code == 429


def _request(addr: str, *, path: str = "/api/test", method: str = "GET", user_id: str | None = None):
    request = MagicMock()
    request.client = SimpleNamespace(host=addr)
    request.method = method
    request.headers = {}
    request.session = {"user_id": user_id} if user_id else {}
    request.scope = {"route": SimpleNamespace(path=path)}
    request.url = SimpleNamespace(path=path)
    return request
