"""Integration test — verify all routers are registered and OpenAPI spec is complete."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from brain.app.api.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_openapi_has_all_routes(client):
    resp = await client.get("/api/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = list(spec["paths"].keys())
    # Verify core routes exist
    assert "/api/health" in paths
    assert "/api/cortex/ideas" in paths
    assert "/api/cortex/run/events/status" in paths
    assert "/api/chat/bootstrap" in paths
    assert "/api/memory/graph" in paths
    assert "/api/login" in paths
    assert "/api/me" in paths


@pytest.mark.asyncio
async def test_health_still_works(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "degraded")
