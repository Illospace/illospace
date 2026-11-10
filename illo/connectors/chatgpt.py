"""Per-person ChatGPT credentials. Only the main app signs in and refreshes."""

import asyncio
import base64
import json
import math
import time
from datetime import UTC, datetime, timedelta
from json import loads
from uuid import NAMESPACE_URL, uuid5

import httpx

from illo.brain.client import ProviderAuth
from illo.connectors.store import vault
from illo.db import one
from illo.ledger import chatgpt as grants
from illo.ledger import sensors
from illo.slackapi import dm_person

ISSUER = "https://auth.openai.com"
TOKEN_URL = ISSUER + "/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
SCOPES = "openid profile email offline_access"
CONNECTED = "ChatGPT is connected. I use your subscription for your requests."
MISSING = 'I have no model sign-in for you yet. Say "connect my ChatGPT".'
BROKEN = 'Your ChatGPT sign-in for Illo stopped working. Say "connect my ChatGPT" to sign in again.'


class TokenError(ValueError):
    def __init__(self, status=None):
        super().__init__("ChatGPT sign-in did not finish")
        self.status = status


def claims(token):
    """Read metadata from a token received directly from the fixed HTTPS endpoint."""
    try:
        payload = token.split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError, TypeError, AttributeError):
        return {}


def credential(data, previous=None):
    previous = previous or {}
    access = data.get("access_token")
    identity = claims(data.get("id_token", ""))
    access_claims = claims(access)
    auth = {
        **access_claims.get("https://api.openai.com/auth", {}),
        **identity.get("https://api.openai.com/auth", {}),
    }
    account = auth.get("chatgpt_account_id") or previous.get("account_id")
    refresh = data.get("refresh_token") or previous.get("refresh_token")
    # Use the access-token expiry, never the ID-token expiry. No fixed token lifetime.
    expires = access_claims.get("exp")
    if expires is None and isinstance(data.get("expires_in"), (int, float)):
        expires = time.time() + data["expires_in"]
    if (
        not all(
            isinstance(v, str) and v and "\n" not in v and "\r" not in v
            for v in (access, refresh, account)
        )
        or not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or not 0 < expires < 253402300800
    ):
        raise TokenError()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account,
        "expires_at": expires,
        "plan": auth.get("chatgpt_plan_type") or previous.get("plan"),
    }


class ChatGPTClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def request(self, url, *, data=None, json=None, pending=False):
        # OAuth codes and refresh tokens are one-use. Never retry a token exchange.
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, trust_env=False, follow_redirects=False
            ) as http:
                async with http.stream("POST", url, data=data, json=json) as response:
                    if pending and response.status_code in (403, 404):
                        return None
                    if response.status_code != 200:
                        raise TokenError(response.status_code)
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 262144:
                            raise TokenError()
                    result = loads(body)
                    if not isinstance(result, dict):
                        raise TokenError()
                    return result
        except Exception:
            # Exception text, causes, response bodies and URLs can hold credentials.
            raise TokenError() from None

    async def exchange(self, code, verifier, redirect_uri=REDIRECT_URI):
        return await self.request(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )

    async def refresh(self, token):
        return await self.request(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": token["refresh_token"],
            },
        )


async def access(ctx, person):
    """Commit a rotation before a run can fail. Serialize refreshes across app processes."""
    auth = None

    # Shield the refresh and its commit from run cancellation after a token rotates.
    async def locked_refresh():
        nonlocal auth
        async with ctx.pool.connection() as conn:
            async with conn.transaction():
                row = await grants.load(conn, person["id"], lock=True)
                if not row or row["revoked_at"] or row["broken_at"]:
                    return
                try:
                    token = json.loads(vault(ctx).decrypt(row["secret"]))
                    if not row["expires_at"] or row["expires_at"] <= datetime.now(UTC) + timedelta(
                        minutes=5
                    ):
                        fresh = await ctx.chatgpt_client.refresh(token)
                        # Save the rotated token in this transaction. Nothing else runs before commit.
                        token = credential(fresh, token)
                        await grants.save(conn, ctx, person["id"], token)
                    auth = ProviderAuth(
                        kind="chatgpt",
                        access_token=token["access_token"],
                        account_id=token["account_id"],
                    )
                except Exception:
                    await conn.execute(
                        "UPDATE connector_grants SET broken_at=clock_timestamp() WHERE id=%s",
                        (row["id"],),
                    )
                    await sensors.error(
                        conn, "chatgpt:" + person["key"], "The ChatGPT sign-in stopped working"
                    )

    work = asyncio.create_task(locked_refresh())
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise
    if auth is None:
        await notify_broken(ctx, person)
    return auth


async def notify_broken(ctx, person):
    # The row lock and notified_at prevent duplicate DMs, including concurrent runs.
    async with ctx.pool.connection() as conn:
        row = await grants.load(conn, person["id"], lock=True)
        if not row or not row["broken_at"] or row["notified_at"]:
            return
        owner = await one(
            conn, "SELECT slack_id FROM people WHERE id=%s AND active", (person["id"],)
        )
        if not ctx.slack_client or not owner or not owner["slack_id"]:
            return
        try:
            # Use this connection for the lookup. A small pool must not deadlock on a second one.
            opened = await ctx.slack_client.conversations_open(users=owner["slack_id"])
            sent = await ctx.slack_client.chat_postMessage(
                channel=opened["channel"]["id"],
                text=BROKEN,
                client_msg_id=str(
                    uuid5(NAMESPACE_URL, f"chatgpt:{row['id']}:{row['broken_at'].isoformat()}")
                ),
            )
        except Exception:
            return
        if sent:
            await conn.execute(
                "UPDATE connector_grants SET notified_at=clock_timestamp() WHERE id=%s",
                (row["id"],),
            )


async def notify_team_missing(ctx):
    async with ctx.pool.connection() as conn:
        await sensors.error(conn, "chatgpt:team", "The team has no model sign-in")
        # An atomic ledger write also caps notices across app processes.
        row = await one(
            conn,
            """INSERT INTO kv (key,value) VALUES
            ('chatgpt:team-notice',jsonb_build_object('at',clock_timestamp()))
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            WHERE (kv.value->>'at')::timestamptz < clock_timestamp()-interval '1 hour'
            RETURNING key""",
        )
    if row:
        try:
            await dm_person(ctx, ctx.workspace.config.default_owner, MISSING)
        except Exception:
            pass
