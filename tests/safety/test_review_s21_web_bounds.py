"""Rule 6: outside text gets one fixed web request, with public addresses only."""

import asyncio
import socket

import pytest
from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import runs
from illo.tools import web


@pytest.mark.parametrize("address", [
    "64:ff9b::a9fe:a9fe", "64:ff9b:1::7f00:1", "::127.0.0.1", "::8.8.8.8",
])
async def test_rule_06_web_refuses_translated_ipv6(monkeypatch, address):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    with pytest.raises(ValueError, match="not public"):
        await web.resolve_public("public.example", 443)


@pytest.mark.parametrize("first_call", [
    "https://first.example/page", "https://second.example/", "https://first.example/",
])
async def test_rule_06_tainted_web_fetches_first_url_at_most_once(
    app, rpc, monkeypatch, first_call
):
    calls = []
    async def fetch(url, **kwargs):
        calls.append(str(url))
        return "Outside page"
    monkeypatch.setattr(web, "fetch_page", fetch)
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "INSERT INTO events(channel,external_id,kind,payload) "
                          "VALUES ('signals','web','signal',%s) RETURNING id", (Jsonb({
                              "text": "Read https://first.example/page and https://second.example/"
                          }),))
        run = await runs.create(conn, principal="team", person_id=None, profile="tainted",
                                tainted=True, event_id=event["id"], runtime="fake", model="fake")
        token = await runs.mint_token(conn, run["id"], 60)
    async def call(url):
        return (await rpc(token, "tools/call", {"name": "web_read_public_page",
                    "arguments": {"url": url}})).json()["result"]["structuredContent"]
    first = await call(first_call)
    assert first["decision"] == ("allow" if first_call.endswith("/page") else "deny")
    assert (await call("https://first.example/page"))["decision"] == "deny"
    assert (await call("https://second.example/"))["decision"] == "deny"
    assert calls == ([first_call] if first_call.endswith("/page") else [])
