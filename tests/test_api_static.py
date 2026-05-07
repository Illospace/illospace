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
async def test_api_404_returns_json(client):
    resp = await client.get("/api/nonexistent")
    assert resp.status_code == 404
    assert resp.json() == {"error": "Not found"}
