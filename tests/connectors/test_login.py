"""Host OAuth tests cover metadata, PKCE, redirect checks, and encrypted grants."""

from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from illo.cli import parser
from illo.connectors import store
from illo.connectors.config import Connector
from illo.connectors.http import BoundedHTTP
from illo.connectors.login import execute, login, metadata
from illo.connectors.mcp_client import MCPClient


def config(**auth):
    return Connector(
        key="docs",
        kind="mcp",
        scope="team",
        url="https://mcp.example.com/mcp",
        auth={"type": "oauth", "client_id": "acme-client", **auth},
    )


async def test_host_login_discovers_metadata_and_stores_encrypted_token(google_app, capsys):
    ctx = google_app.state.ctx
    seen = []
    cfg = config()

    def respond(request):
        seen.append(request)
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(200, json={"authorization_servers": ["https://auth.example.com"]})
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.com",
                    "authorization_endpoint": "https://auth.example.com/authorize",
                    "token_endpoint": "https://auth.example.com/token",
                },
            )
        assert request.url.path == "/token"
        form = parse_qs(request.content.decode())
        assert form["resource"] == [cfg.url]
        assert form["code_verifier"]
        return httpx.Response(
            200,
            json={
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
            },
        )

    def paste(prompt):
        url = capsys.readouterr().out.splitlines()[-1]
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"]
        return (
            cfg.auth.redirect_uri
            + "?"
            + urlencode({"state": query["state"][0], "code": "test-code"})
        )

    async with ctx.pool.connection() as conn:
        await login(conn, ctx, cfg, http=BoundedHTTP(httpx.MockTransport(respond)), prompt=paste)
        row, token = await store.load(conn, ctx, "docs", None)
        assert token["refresh_token"] == "test-refresh"
        assert b"test-refresh" not in row["secret"]
        assert token["token_url"] == "https://auth.example.com/token"
    assert len(seen) == 3


@pytest.mark.parametrize(
    "pasted",
    [
        "http://evil.example/callback?code=x&state=x",
        "http://127.0.0.1:8765/callback?code=x&state=wrong",
    ],
)
async def test_host_login_rejects_redirect_and_wrong_state(google_app, pasted):
    ctx = google_app.state.ctx
    cfg = config(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
    )
    http = BoundedHTTP(
        httpx.MockTransport(lambda r: pytest.fail("A failed state must not exchange a code"))
    )
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError):
            await login(conn, ctx, cfg, http=http, prompt=lambda _: pasted)
        assert not await (await conn.execute("SELECT id FROM connector_grants")).fetchall()


async def test_mcp_refresh_keeps_rotated_token_and_client_credentials(google_app):
    ctx = google_app.state.ctx
    requests = []
    cfg = config(client_secret="docs-client-secret", token_url="https://auth.example.com/token")

    def respond(request):
        requests.append(request)
        data = parse_qs(request.content.decode())
        assert data["client_secret"] == ["test-secret"]
        return httpx.Response(
            200,
            json={
                "access_token": "test-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    client = MCPClient(http=BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        await store.vault(ctx).set(conn, "docs-client-secret", "test-secret")
        await store.save(conn, ctx, cfg.key, None, {"refresh_token": "old-refresh"}, [])
        assert await client.bearer(ctx, conn, cfg) == "test-access"
        _, token = await store.load(conn, ctx, cfg.key, None)
        assert token["refresh_token"] == "new-refresh"
        assert await client.bearer(ctx, conn, cfg) == "test-access"
        assert len(requests) == 1
        assert parse_qs(requests[0].content.decode())["grant_type"] == ["refresh_token"]
        await conn.execute("DELETE FROM connector_grants")
        assert await client.bearer(ctx, conn, cfg) == "test-access"
        assert parse_qs(requests[-1].content.decode())["grant_type"] == ["client_credentials"]


async def test_cli_status_has_no_secret(google_app, capsys):
    ctx = google_app.state.ctx
    args = parser().parse_args(["connectors", "status"])
    async with ctx.pool.connection() as conn:
        await store.save(conn, ctx, "docs", None, {"access_token": "never-print-this"}, [])
        await execute(conn, args, ctx.config)
    out = capsys.readouterr().out
    assert "docs  team  connected" in out
    assert "never-print-this" not in out
    assert parser().parse_args(["connector", "login", "docs"]).name == "docs"


async def test_oauth_metadata_rejects_unsafe_endpoint():
    def respond(request):
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(200, json={"authorization_servers": ["https://auth.example.com"]})
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "http://unsafe.example.com/auth",
                "token_endpoint": "https://auth.example.com/token",
            },
        )

    with pytest.raises(ValueError, match="HTTPS"):
        await metadata(BoundedHTTP(httpx.MockTransport(respond)), config())
