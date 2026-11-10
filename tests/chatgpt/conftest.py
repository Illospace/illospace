"""Fake endpoints and encrypted grants. No host credential or live provider call."""

import base64
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.connectors import chatgpt
from illo.ledger import chatgpt as grants
from illo.ledger import people


def jwt(**claims):
    return (
        "fake."
        + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        + ".fake"
    )


def tokens(access="fake-access", refresh="fake-refresh", account="acme-account", expires=3600):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires,
        "id_token": jwt(
            email="ada@example.com",
            **{
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account,
                    "chatgpt_plan_type": "plus",
                }
            }
        ),
    }


class FakeTokenEndpoint:
    def __init__(self):
        self.requests = []
        self.refresh_count = 0
        self.broken = False
        self.device = False
        self.pending = True
        self.delay = 0
        self.on_exchange = None

    async def respond(self, request):
        import asyncio

        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if request.url.path.endswith("/deviceauth/usercode"):
            return (
                httpx.Response(
                    200,
                    json={
                        "device_auth_id": "fake-device",
                        "user_code": "ABCD-EFGH",
                        "interval": "5",
                    },
                )
                if self.device
                else httpx.Response(404)
            )
        if request.url.path.endswith("/deviceauth/token"):
            return (
                httpx.Response(403)
                if self.pending
                else httpx.Response(
                    200,
                    json={
                        "authorization_code": "fake-device-code",
                        "code_verifier": "fake-device-verifier",
                        "code_challenge": "fake-challenge",
                    },
                )
            )
        if request.headers.get("content-type") == "application/json":
            body = json.loads(request.content)
            assert body["client_id"] == chatgpt.CLIENT_ID
            assert body["grant_type"] == "refresh_token"
            self.refresh_count += 1
            if self.broken:
                return httpx.Response(
                    400, json={"error": "refresh_token_reused", "message": "fake-refresh"}
                )
            return httpx.Response(200, json=tokens("fresh-access", "rotated-refresh"))
        if self.on_exchange:
            await self.on_exchange()
        return httpx.Response(200, json=tokens())


@pytest.fixture
async def chatgpt_app(app):
    ctx = app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    # The example workspace is local to this app instance.
    ctx.workspace.config.brain.runtime = "openai_responses"
    ctx.workspace.config.brain.auth = "chatgpt"
    backend = FakeTokenEndpoint()
    ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(backend.respond))
    return app, backend


async def save_grant(ctx, key="ada", *, expires=3600, access="fake-access"):
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, key)
        token = chatgpt.credential(tokens(access=access, account="account-" + key))
        token["expires_at"] = time.time() + expires
        await grants.save(conn, ctx, person["id"], token)
    return person
