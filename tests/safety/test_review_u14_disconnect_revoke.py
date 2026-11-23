"""Disconnect attempts issuer revocation before it deletes the encrypted grant."""

from urllib.parse import parse_qs

import httpx
import pytest

from illo.connectors import chatgpt
from illo.db import one
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("failure", [False, True])
async def test_u14_disconnect_revokes_then_deletes(chatgpt_app, replay, caplog, failure):
    ctx = chatgpt_app[0].state.ctx
    person = await save_grant(ctx)
    requests = []

    async def respond(request):
        async with ctx.pool.connection() as conn:
            assert await one(
                conn, "SELECT 1 FROM connector_grants WHERE person_id=%s", (person["id"],)
            )
        assert str(request.url) == chatgpt.REVOCATION_URL
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(
            503 if failure else 200, json={"error": "fake-revoke-body"} if failure else {}
        )

    ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(respond))
    result = await replay(text="disconnect my ChatGPT")
    assert result["text"] == "ChatGPT is disconnected from Illo."
    assert [r["token_type_hint"][0] for r in requests] == ["refresh_token", "access_token"]
    assert requests[0]["token"] == ["fake-refresh"]
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM connector_grants")
    assert "fake-refresh" not in caplog.text and "fake-revoke-body" not in caplog.text
