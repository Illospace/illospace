"""Fake HTTP covers Google refresh, projection, shared scope, and retry limits."""

import base64

import httpx
import pytest

from illo.connectors import store
from illo.connectors.config import Connector
from illo.connectors.google import GoogleClient
from illo.connectors.http import BoundedHTTP
from illo.ledger import people, runs


async def seed(ctx, config=None, owner="ada"):
    config = config or ctx.workspace.connectors[0]
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, owner) if owner else None
        await store.save(
            conn,
            ctx,
            config.key,
            person["id"] if person else None,
            {"access_token": "test-access", "refresh_token": "test-refresh", "expires_in": 3600},
            config.scopes,
        )
    return person


async def test_refresh_and_reuse(google_app, monkeypatch):
    ctx = google_app.state.ctx
    config = ctx.workspace.connectors[0]
    person = await seed(ctx)
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("ILLO_GOOGLE_CLIENT_SECRET", "test-secret")
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"messages": []})

    client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        for _ in range(2):
            result = await client.call(
                conn, ctx, config, person["id"], "mail_search", {"query": "invoice", "limit": 5}
            )
            assert result["type"] == "outside_data"
        _, token = await store.load(conn, ctx, "google", person["id"])
        assert token["refresh_token"] == "test-refresh"
    assert len([r for r in seen if r.url.path == "/token"]) == 1


async def test_mail_text_only_and_calendar_window(google_app):
    ctx = google_app.state.ctx
    person = await seed(ctx)
    seen = []

    def respond(request):
        seen.append(request)
        if "/calendar/" in request.url.path:
            assert request.url.params["timeMin"] == "2026-01-01T00:00:00Z"
            return httpx.Response(200, json={"items": [{"id": "meeting"}]})
        return httpx.Response(
            200,
            json={
                "id": "mail-1",
                "payload": {
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": base64.urlsafe_b64encode(b"Hello").decode()},
                        },
                        {"mimeType": "text/html", "body": {"data": "PHNjcmlwdD4="}},
                        {"filename": "invoice.pdf", "body": {"attachmentId": "do-not-fetch"}},
                    ]
                },
            },
        )

    client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        result = await client.call(
            conn, ctx, ctx.workspace.connectors[0], person["id"], "mail_get", {"id": "mail-1"}
        )
        assert result["data"]["text"] == "Hello"
        assert result["data"]["attachments"] == ["invoice.pdf"]
        result = await client.call(
            conn,
            ctx,
            ctx.workspace.connectors[0],
            person["id"],
            "calendar_list",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
        )
        assert result["data"]["items"][0]["id"] == "meeting"
    assert len(seen) == 2


async def test_drive_exports_and_metadata(google_app):
    ctx = google_app.state.ctx
    person = await seed(ctx)
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/export"):
            return httpx.Response(200, text="A document")
        return httpx.Response(
            200,
            json={
                "id": "doc-1",
                "name": "Plan",
                "mimeType": "application/vnd.google-apps.document",
            },
        )

    client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        result = await client.call(
            conn, ctx, ctx.workspace.connectors[0], person["id"], "drive_get", {"id": "doc-1"}
        )
        assert result["data"]["text"] == "A document"
    assert seen[-1].url.params["mimeType"] == "text/plain"


async def test_shared_drive_team_run_has_no_personal_read(google_app, rpc):
    ctx = google_app.state.ctx
    shared = Connector(
        key="drive-shared", kind="google", scope="team", drive_id="acme-drive", team_grant=True
    )
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [shared]})
    await seed(ctx, shared, owner=None)
    async with ctx.pool.connection() as conn:
        run = await runs.create(
            conn,
            principal="team",
            person_id=None,
            profile="team_channel",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(
        token, "tools/call", {"name": "drive_shared_search", "arguments": {"query": "plan"}}
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "allow"
    assert ctx.google_client.calls[0]["person_id"] is None
    async with ctx.pool.connection() as conn:
        assert not (await runs.get(conn, run["id"]))["personal_read"]


async def test_shared_drive_get_rejects_outside_file(google_app):
    ctx = google_app.state.ctx
    shared = Connector(
        key="drive-shared", kind="google", scope="team", folder_ids=["folder-1"], team_grant=True
    )
    await seed(ctx, shared, owner=None)

    def respond(request):
        return httpx.Response(
            200,
            json={
                "id": "doc-1",
                "parents": ["other-folder"],
                "mimeType": "application/vnd.google-apps.document",
            },
        )

    client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError, match="outside"):
            await client.call(conn, ctx, shared, None, "drive_get", {"id": "doc-1"})


async def test_http_backoff_and_size_limit():
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) < 3 else 200, json={"ok": True})

    http = BoundedHTTP(httpx.MockTransport(respond))
    assert await http.request("GET", "https://api.example.com/read") == {"ok": True}
    assert len(attempts) == 3
    http = BoundedHTTP(httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 1025)))
    with pytest.raises(ValueError, match="too large"):
        await http.request("GET", "https://api.example.com/read", max_bytes=1024)


async def test_failed_revoke_still_deletes_local_grant(google_app):
    ctx = google_app.state.ctx
    person = await seed(ctx)
    client = GoogleClient(
        BoundedHTTP(
            httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_token"}))
        )
    )
    async with ctx.pool.connection() as conn:
        assert not await client.disconnect(conn, ctx, ctx.workspace.connectors[0], person["id"])
        with pytest.raises(ValueError, match="sign-in"):
            await store.load(conn, ctx, "google", person["id"])


async def test_service_account_signs_fixed_read_only_scope(google_app):
    import json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    ctx = google_app.state.ctx
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    account = {
        "client_email": "service@acme.example",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }
    cfg = Connector(
        key="drive-shared",
        kind="google",
        scope="team",
        drive_id="acme-drive",
        auth={"type": "service_account", "secret": "acme-service"},
    )
    requests = []

    def respond(request):
        from urllib.parse import parse_qs

        requests.append(request)
        if request.url.path == "/token":
            assertion = parse_qs(request.content.decode())["assertion"][0]
            header, body, signature = assertion.split(".")

            def decode(value):
                return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

            claims = json.loads(decode(body))
            assert claims["scope"] == "https://www.googleapis.com/auth/drive.readonly"
            assert claims["aud"] == "https://oauth2.googleapis.com/token"
            assert claims["exp"] - claims["iat"] == 3600
            key.public_key().verify(
                decode(signature),
                (header + "." + body).encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return httpx.Response(200, json={"access_token": "test-access"})
        assert request.url.params["driveId"] == "acme-drive"
        assert request.url.params["corpora"] == "drive"
        return httpx.Response(200, json={"files": []})

    client = GoogleClient(BoundedHTTP(httpx.MockTransport(respond)))
    async with ctx.pool.connection() as conn:
        await store.vault(ctx).set(conn, "acme-service", json.dumps(account))
        await client.call(conn, ctx, cfg, None, "drive_search", {"query": "plan"})
    assert len(requests) == 2
