"""One-use Google sign-in. Codes never enter a brain run or the event ledger."""

import hashlib
import json
import logging
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from illo.connectors import google, store
from illo.db import one


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def callback_url(ctx) -> str:
    return ctx.config.public_url.rstrip("/") + "/oauth/google/callback"


async def issue(ctx, person_id: int) -> str:
    config = google.google_config(ctx)
    if not config:
        raise ValueError("The Google connector is not enabled")
    async with ctx.pool.connection() as conn:
        await google.credentials(ctx, conn)
        verifier = secrets.token_urlsafe(48)
        # The encrypted state authenticates its owner and nonce. The ledger enforces one use.
        state = (
            store.vault(ctx)
            .encrypt(json.dumps({"person": person_id, "nonce": secrets.token_urlsafe(32)}))
            .decode()
        )
        await conn.execute(
            """INSERT INTO connector_oauth_states
            (state_hash,connector_key,person_id,secret,expires_at)
            VALUES (%s,%s,%s,%s,clock_timestamp()+interval '10 minutes')""",
            (
                digest(state),
                config.key,
                person_id,
                store.vault(ctx).encrypt(
                    json.dumps({"verifier": verifier, "redirect_uri": callback_url(ctx)})
                ),
            ),
        )
    return ctx.config.public_url.rstrip("/") + "/oauth/google/start?" + urlencode({"state": state})


async def start(ctx, state: str) -> str:
    async with ctx.pool.connection() as conn:
        row = await one(
            conn,
            """UPDATE connector_oauth_states SET started_at=clock_timestamp()
            WHERE state_hash=%s AND expires_at>clock_timestamp() AND started_at IS NULL
            AND consumed_at IS NULL AND EXISTS (SELECT 1 FROM people WHERE id=person_id AND active)
            RETURNING *""",
            (digest(state),),
        )
        if not row:
            raise ValueError("The sign-in link is invalid or expired")
        details = json.loads(store.vault(ctx).decrypt(row["secret"]))
        client_id, _ = await google.credentials(ctx, conn)
    config = google.google_config(ctx)
    import base64

    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(details["verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return (
        google.AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": details["redirect_uri"],
                "response_type": "code",
                "scope": " ".join(config.scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


async def complete(ctx, state: str, code: str, *, person_id: int | None = None) -> str:
    # Consume before the network call. A failed exchange cannot replay the code.
    async with ctx.pool.connection() as conn:
        row = await one(
            conn,
            """UPDATE connector_oauth_states SET consumed_at=clock_timestamp()
            WHERE state_hash=%s AND consumed_at IS NULL AND started_at IS NOT NULL
            AND expires_at>clock_timestamp() AND (%s::bigint IS NULL OR person_id=%s)
            AND EXISTS (SELECT 1 FROM people WHERE id=person_id AND active) RETURNING *""",
            (digest(state), person_id, person_id),
        )
        if not row:
            raise ValueError("The sign-in state is invalid or expired")
        details = json.loads(store.vault(ctx).decrypt(row["secret"]))
        client_id, client_secret = await google.credentials(ctx, conn)
    config = google.google_config(ctx)
    if not config or config.key != row["connector_key"]:
        raise ValueError("The Google connector is not enabled")
    token = await ctx.google_client.http.request(
        "POST",
        google.TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": details["redirect_uri"],
            "code_verifier": details["verifier"],
        },
    )
    if not isinstance(token, dict) or not token.get("refresh_token"):
        raise ValueError("Google did not provide a refresh token. Connect Google again")
    scopes = token.get("scope", " ".join(config.scopes)).split()
    if not set(scopes) <= set(config.scopes):
        raise ValueError("Google returned a scope that Illo does not allow")
    async with ctx.pool.connection() as conn:
        await store.save(conn, ctx, config.key, row["person_id"], token, scopes)
    return "Google is connected. Illo can read your mail, calendar, and drive."


def is_callback(text: str) -> bool:
    return "/oauth/google/callback" in text or (
        ("http://localhost" in text or "http://127.0.0.1" in text)
        and ("code=" in text or "state=" in text)
    )


def redact_payload(payload):
    if isinstance(payload, str):
        return "[Google sign-in address removed]" if is_callback(payload) else payload
    if isinstance(payload, dict):
        return {key: redact_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(value) for value in payload]
    return payload


class OAuthLogFilter(logging.Filter):
    def filter(self, record):
        # Access loggers include the query string. Drop OAuth requests before formatting.
        message = record.getMessage()
        return not ("/oauth/google/" in message or "code=" in message or "state=" in message)


async def claim(ctx, event, authority) -> str | None:
    pasted = is_callback(event.text)
    if not pasted and event.text.strip().lower() not in {
        "connect my google",
        "disconnect my google",
    }:
        return None
    if authority.profile != "person_dm" or authority.tainted or event.channel != "slack":
        return "Use a DM to connect your Google account."
    try:
        if pasted:
            raw = event.text.strip().strip("<>").split("|", 1)[0]
            parsed = urlsplit(raw)
            expected = urlsplit(callback_url(ctx))
            if (parsed.scheme, parsed.netloc, parsed.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ):
                raise ValueError("The redirect address is invalid")
            params = parse_qs(parsed.query, strict_parsing=True)
            if len(params.get("state", [])) != 1 or len(params.get("code", [])) != 1:
                raise ValueError("The redirect address is invalid")
            return await complete(
                ctx, params["state"][0], params["code"][0], person_id=authority.person_id
            )
        if event.text.strip().lower() == "connect my google":
            link = await issue(ctx, authority.person_id)
            if urlsplit(ctx.config.public_url).hostname in {"localhost", "127.0.0.1", "::1"}:
                state = parse_qs(urlsplit(link).query)["state"][0]
                link = await start(ctx, state)
                return (
                    "Open this link to connect Google: "
                    + link
                    + "\nThe final local address can fail to open. Paste that full address here."
                )
            return "Open this link to connect your Google account: " + link
        async with ctx.pool.connection() as conn:
            revoked = await ctx.google_client.disconnect(
                conn, ctx, google.google_config(ctx), authority.person_id
            )
        if not revoked:
            return (
                "Google is disconnected from Illo. Google did not confirm revocation. "
                "Remove Illo's access in your Google account settings."
            )
        return "Google is disconnected."
    except Exception:
        return "Google sign-in did not finish. Send ‘connect my Google’ to try again."


async def setup(ctx) -> None:
    async def expire_states():
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM connector_oauth_states WHERE expires_at<clock_timestamp()-interval '1 day'"
            )

    ctx.every(300, expire_states)
    ctx.gateway.register_claim(claim)
    ctx.gateway.register_payload_filter(redact_payload)
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(OAuthLogFilter())

    async def start_route(request: Request):
        try:
            url = await start(ctx, request.query_params.get("state", ""))
            return RedirectResponse(
                url, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
        except Exception:
            raise HTTPException(400, "The sign-in link is invalid or expired") from None

    async def callback_route(request: Request):
        try:
            if request.query_params.get("error"):
                raise ValueError("Google sign-in was cancelled")
            text = await complete(
                ctx, request.query_params.get("state", ""), request.query_params.get("code", "")
            )
            return PlainTextResponse(
                text, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
        except Exception:
            raise HTTPException(400, "Google sign-in did not finish") from None

    ctx.app.add_api_route("/oauth/google/start", start_route, methods=["GET"])
    ctx.app.add_api_route("/oauth/google/callback", callback_route, methods=["GET"])
