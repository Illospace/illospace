"""One-use ChatGPT sign-in. A gateway claim removes pasted codes before a run."""

import base64
import hashlib
import json
import logging
import secrets
import time
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

from illo.connectors import chatgpt, oauth
from illo.connectors.store import vault
from illo.db import all_rows, one
from illo.ledger import chatgpt as grants
from illo.ledger import sensors
from illo.slackapi import dm_person

FAILED = 'ChatGPT sign-in did not finish. Say "connect my ChatGPT" to try again.'
DEVICE_BASE = chatgpt.ISSUER + "/api/accounts/deviceauth"


def binding(event):
    return {"channel_id": event.payload.get("channel_id"), "thread": event.reply_to}


def is_dm(event, authority):
    return oauth.person_dm(event, authority) and not any(
        event.payload.get(k) for k in ("bot", "bot_id", "webhook", "form", "contains_outside_text")
    )


async def issue(ctx, person_id, event, *, conn=None):
    async def insert(conn):
        await conn.execute("SELECT id FROM people WHERE id=%s AND active FOR UPDATE", (person_id,))
        state = (
            vault(ctx)
            .encrypt(json.dumps({"person": person_id, "nonce": secrets.token_urlsafe(32)}))
            .decode()
        )
        details = {**binding(event), "verifier": secrets.token_urlsafe(48)}
        # A new attempt cancels the person's previous pending sign-in.
        await conn.execute(
            "UPDATE connector_oauth_states SET cancelled_at=clock_timestamp(), "
            "consumed_at=coalesce(consumed_at,clock_timestamp()) "
            "WHERE connector_key='chatgpt' AND person_id=%s",
            (person_id,),
        )
        await conn.execute(
            """INSERT INTO connector_oauth_states
            (state_hash,connector_key,person_id,secret,expires_at,provider)
            VALUES (%s,'chatgpt',%s,%s,clock_timestamp()+interval '10 minutes','chatgpt')""",
            (oauth.digest(state), person_id, vault(ctx).encrypt(json.dumps(details))),
        )
        return state

    if conn is not None:
        return await insert(conn)
    async with ctx.pool.connection() as conn:
        return await insert(conn)


def authorize(state, verifier):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return (
        chatgpt.ISSUER
        + "/oauth/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": chatgpt.CLIENT_ID,
                "redirect_uri": chatgpt.REDIRECT_URI,
                "scope": chatgpt.SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "originator": "illo-brain",
            }
        )
    )


async def start(ctx, state, *, paste=False):
    async with ctx.pool.connection() as conn:
        row = await grants.state(conn, oauth.digest(state), lock=True)
        if not row or row["started_at"]:
            raise ValueError(FAILED)
        details = json.loads(vault(ctx).decrypt(row["secret"]))
        await conn.execute(
            "UPDATE connector_oauth_states SET started_at=clock_timestamp() WHERE state_hash=%s",
            (row["state_hash"],),
        )
        device = None
        if not paste:
            try:
                device = await ctx.chatgpt_client.request(
                    DEVICE_BASE + "/usercode", json={"client_id": chatgpt.CLIENT_ID}
                )
                if not all(
                    isinstance(device.get(k), str) and device[k]
                    for k in ("device_auth_id", "user_code")
                ):
                    raise chatgpt.TokenError()
                details.update(
                    device_auth_id=device["device_auth_id"],
                    user_code=device["user_code"],
                    interval=max(5, min(60, int(device.get("interval", 5)))),
                    next_poll=0,
                )
            except Exception:
                device = None
        details["mode"] = "device" if device else "paste"
        await conn.execute(
            "UPDATE connector_oauth_states SET secret=%s WHERE state_hash=%s",
            (vault(ctx).encrypt(json.dumps(details)), row["state_hash"]),
        )
    if device:
        return "device", (
            "1. Open https://auth.openai.com/codex/device and sign in to your own ChatGPT account.\n"
            f"2. Enter this one-time code: {device['user_code']}\n"
            "The code expires 10 minutes after your request. Illo sends you a DM after sign-in.\n"
            "Use this code only if you asked Illo to connect your ChatGPT.\n"
            "If ChatGPT refuses the code, turn on the device-code sign-in in the ChatGPT "
            'security settings, or write "connect my ChatGPT with a link".'
        )
    return "paste", authorize(state, details["verifier"])


class AccountMismatch(ValueError):
    pass


async def verify_account(ctx, conn, person_id, data):
    # The ID token comes directly from the fixed HTTPS token endpoint, never the paste.
    # Decode only for this comparison. It grants no authority or tool access.
    identity = chatgpt.claims(data.get("id_token", ""))
    email = identity.get("email")
    person = await one(conn, "SELECT key,emails FROM people WHERE id=%s AND active", (person_id,))
    if (
        identity.get("email_verified") is False
        or not isinstance(email, str)
        or email.count("@") != 1
        or any(c.isspace() or c in "<>|&" for c in email)
    ):
        await ctx.chatgpt_client.revoke(data)
        raise ValueError(FAILED)
    allowed = (
        person["emails"] if person else []
    ) + ctx.workspace.config.brain.chatgpt_allowed_accounts
    if email.casefold() not in {address.casefold() for address in allowed}:
        await ctx.chatgpt_client.revoke(data)
        if person:
            async with ctx.pool.connection() as warning_conn:
                await sensors.error(
                    warning_conn,
                    "chatgpt:" + person["key"],
                    "The ChatGPT account does not match the person",
                )
        local, domain = email.split("@")
        masked = local[:1] + "***@" + domain
        raise AccountMismatch(
            f"That ChatGPT account ({masked}) is not one of your addresses in people.yaml. "
            "Sign in with your own account."
        )
    return email


async def complete(ctx, state, code, person_id, event):
    # Commit consumption before exchange. A failed exchange cannot reuse the code.
    async with ctx.pool.connection() as conn:
        row = await grants.state(conn, oauth.digest(state), lock=True)
        if not row or row["person_id"] != person_id or not row["started_at"]:
            raise ValueError(FAILED)
        details = json.loads(vault(ctx).decrypt(row["secret"]))
        if details.get("mode") != "paste" or any(
            details.get(k) != v for k, v in binding(event).items()
        ):
            raise ValueError(FAILED)
        await conn.execute(
            "UPDATE connector_oauth_states SET consumed_at=clock_timestamp() WHERE state_hash=%s",
            (row["state_hash"],),
        )
    data = await ctx.chatgpt_client.exchange(code, details["verifier"])
    token = chatgpt.credential(data)
    async with ctx.pool.connection() as conn:
        # Serialize with disconnect and confirm this is still the newest attempt.
        await conn.execute("SELECT id FROM people WHERE id=%s AND active FOR UPDATE", (person_id,))
        current = await one(
            conn,
            """SELECT 1 FROM connector_oauth_states
            WHERE state_hash=%s AND cancelled_at IS NULL AND expires_at>clock_timestamp()
            AND EXISTS (SELECT 1 FROM people WHERE id=person_id AND active)""",
            (row["state_hash"],),
        )
        if not current:
            raise ValueError(FAILED)
        email = await verify_account(ctx, conn, person_id, data)
        await grants.save(conn, ctx, person_id, token)
        person = await one(conn, "SELECT key FROM people WHERE id=%s AND active", (person_id,))
        await sensors.ok(conn, "chatgpt:" + person["key"])
    return chatgpt.CONNECTED.format(email=email)


async def poll_devices(ctx):
    async with ctx.pool.connection() as conn:
        expired = await all_rows(
            conn,
            """UPDATE connector_oauth_states SET consumed_at=clock_timestamp()
            WHERE connector_key='chatgpt' AND consumed_at IS NULL AND cancelled_at IS NULL
            AND started_at IS NOT NULL AND expires_at<=clock_timestamp()
            RETURNING secret,(SELECT key FROM people WHERE id=person_id AND active) AS person""",
        )
    for row in expired:
        try:
            details = json.loads(vault(ctx).decrypt(row["secret"]))
            if row["person"] and details.get("mode") == "device":
                await dm_person(ctx, row["person"], FAILED)
        except Exception:
            pass
    async with ctx.pool.connection() as conn:
        rows = await all_rows(
            conn,
            """SELECT state_hash FROM connector_oauth_states
            WHERE connector_key='chatgpt' AND consumed_at IS NULL AND started_at IS NOT NULL
            AND expires_at>clock_timestamp()""",
        )
    for candidate in rows:
        await poll_device(ctx, candidate["state_hash"])


async def poll_device(ctx, state_hash):
    person_key, answer = None, None
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "SELECT id FROM people WHERE id=(SELECT person_id FROM connector_oauth_states WHERE state_hash=%s) FOR UPDATE",
            (state_hash,),
        )
        row = await grants.state(conn, state_hash, lock=True)
        if not row:
            return
        details = json.loads(vault(ctx).decrypt(row["secret"]))
        if details.get("mode") != "device" or details.get("next_poll", 0) > time.time():
            return
        try:
            try:
                data = await ctx.chatgpt_client.request(
                    DEVICE_BASE + "/token",
                    json={
                        "device_auth_id": details["device_auth_id"],
                        "user_code": details["user_code"],
                    },
                    pending=True,
                )
            except chatgpt.PendingPoll:
                details["interval"] = min(60, details["interval"] * 2)
                data = None
            if data is None:
                details["next_poll"] = time.time() + details["interval"]
                await conn.execute(
                    "UPDATE connector_oauth_states SET secret=%s WHERE state_hash=%s",
                    (vault(ctx).encrypt(json.dumps(details)), state_hash),
                )
                return
            # The device endpoint supplies its own PKCE verifier and code.
            exchanged = await ctx.chatgpt_client.exchange(
                data["authorization_code"],
                data["code_verifier"],
                chatgpt.ISSUER + "/deviceauth/callback",
            )
            email = await verify_account(ctx, conn, row["person_id"], exchanged)
            await grants.save(conn, ctx, row["person_id"], chatgpt.credential(exchanged))
            answer = chatgpt.CONNECTED.format(email=email)
        except AccountMismatch as error:
            answer = str(error)
        except Exception:
            answer = FAILED
        await conn.execute(
            "UPDATE connector_oauth_states SET consumed_at=clock_timestamp() WHERE state_hash=%s",
            (state_hash,),
        )
        person = await one(
            conn, "SELECT key FROM people WHERE id=%s AND active", (row["person_id"],)
        )
        if person:
            person_key = person["key"]
            if answer.startswith("ChatGPT is connected as "):
                await sensors.ok(conn, "chatgpt:" + person_key)
    if person_key:
        try:
            await dm_person(ctx, person_key, answer)
        except Exception:
            pass


def callback_address(text):
    raw = unescape(text).strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].split("|", 1)[0]
    for host in ("127.0.0.1", "localhost"):
        address = oauth.callback_address(raw, f"http://{host}:1455/auth/callback")
        if address:
            return address
    return None


def redact_payload(payload):
    for expected in ("http://127.0.0.1:1455/auth/callback", "http://localhost:1455/auth/callback"):
        payload = oauth.redact_payload(payload, expected, "[ChatGPT sign-in address removed]")
    return payload


async def prepare(ctx, event, authority):
    if is_dm(event, authority) and callback_address(event.text):
        return None
    return event.model_copy(
        update={"text": redact_payload(event.text), "payload": redact_payload(event.payload)}
    )


async def claim(ctx, event, authority):
    if not is_dm(event, authority):
        return None
    pasted = callback_address(event.text)
    command = event.text.strip().lower()
    if not pasted and command not in {
        "connect my chatgpt",
        "connect my chatgpt with a link",
        "disconnect my chatgpt",
        "chatgpt status",
    }:
        return None
    try:
        if pasted:
            parsed = urlsplit(pasted)
            params = parse_qs(parsed.query, strict_parsing=True)
            if parsed.fragment or any(len(params.get(k, [])) != 1 for k in ("state", "code")):
                raise ValueError(FAILED)
            return await complete(
                ctx, params["state"][0], params["code"][0], authority.person_id, event
            )
        if command in {"connect my chatgpt", "connect my chatgpt with a link"}:
            if (
                ctx.workspace.config.brain.person_credential == "team"
                and authority.principal != "person:" + ctx.workspace.config.brain.team_credential
            ):
                return chatgpt.team_signin(ctx.workspace) + " You do not need to connect your ChatGPT."
            state = await issue(ctx, authority.person_id, event)
            mode, answer = await start(ctx, state, paste=command.endswith(" with a link"))
            if mode == "device":
                return answer
            return (
                "Open this one-time link to connect your ChatGPT:\n"
                + answer
                + (
                    "\nIf you use the paste flow, the final local address can fail to open. "
                    "Paste that full address here, in this same DM and thread."
                )
            )
        if command == "disconnect my chatgpt":
            await chatgpt.disconnect(ctx, authority.person_id)
            if ctx.workspace.config.brain.person_credential == "team":
                if authority.principal == "person:" + ctx.workspace.config.brain.team_credential:
                    return "ChatGPT is disconnected from Illo. Illo now has no model sign-in for all members."
                return "Your ChatGPT sign-in is disconnected from Illo. " + chatgpt.team_signin(ctx.workspace)
            return "ChatGPT is disconnected from Illo."
        async with ctx.pool.connection() as conn:
            return await signin_status(conn, ctx.workspace, authority.person_id)
    except AccountMismatch as error:
        return str(error)
    except Exception:
        return FAILED


def status_text(row):
    if row["broken_at"]:
        return "Your ChatGPT sign-in stopped working."
    if not row["expires_at"] or row["revoked_at"]:
        return "ChatGPT is not connected."
    return "ChatGPT is connected. The access token expires " + row["expires_at"].isoformat() + "."


async def signin_status(conn, workspace, person_id):
    if workspace.config.brain.person_credential == "team":
        person = await one(
            conn, "SELECT id FROM people WHERE key=%s AND active",
            (workspace.config.brain.team_credential,),
        )
        rows = await grants.status(conn, person["id"])
        return chatgpt.team_signin(workspace) + " " + status_text(rows[0]).replace("Your ", "The ")
    return status_text((await grants.status(conn, person_id))[0])


class LogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not any(
            s in message
            for s in ("/oauth/chatgpt/", "/oauth/token", "/deviceauth/", "code=", "state=")
        )


async def setup(ctx):
    ctx.chatgpt_client = chatgpt.ChatGPTClient()
    ctx.gateway.register_preparer(prepare)
    ctx.gateway.register_claim(claim)
    ctx.gateway.register_payload_filter(redact_payload)
    ctx.every(5, lambda: poll_devices(ctx))
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(LogFilter())
