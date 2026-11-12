"""One-use Google sign-in. Codes never enter a brain run or the event ledger."""

import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from illo.connectors import google, store
from illo.db import one

BROWSER_COOKIE = "__Host-illo-google"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def public_url(ctx) -> str:
    return (ctx.workspace.config.connect.public_url or ctx.config.public_url).rstrip("/")


def callback_url(ctx) -> str:
    return public_url(ctx) + "/oauth/google/callback"


@asynccontextmanager
async def connection(ctx, conn):
    if conn is not None:
        yield conn
    else:
        async with ctx.pool.connection() as opened:
            yield opened


async def issue(ctx, person_id: int, *, conn=None) -> str:
    config = google.google_config(ctx)
    if not config:
        raise ValueError("The Google connector is not enabled")
    async with connection(ctx, conn) as conn:
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
    return public_url(ctx) + "/oauth/google/start?" + urlencode({"state": state})


async def start(ctx, state: str, *, browser_nonce: str | None = None, conn=None) -> str:
    async with connection(ctx, conn) as conn:
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
        if browser_nonce:
            details["browser_hash"] = digest(browser_nonce)
            await conn.execute(
                "UPDATE connector_oauth_states SET secret=%s WHERE state_hash=%s",
                (store.vault(ctx).encrypt(json.dumps(details)), digest(state)),
            )
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


async def complete(
    ctx, state: str, code: str, *, person_id: int | None = None, browser_nonce: str | None = None
) -> str:
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
        if person_id is None and (
            not browser_nonce
            or not details.get("browser_hash")
            or not secrets.compare_digest(details["browser_hash"], digest(browser_nonce))
        ):
            raise ValueError("Use the browser that opened the sign-in link")
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
        owner = await one(
            conn, "SELECT emails FROM people WHERE id=%s AND active", (row["person_id"],)
        )
        await ctx.google_client.verify_account(token, owner["emails"] if owner else [])
        await store.save(conn, ctx, config.key, row["person_id"], token, scopes)
    return "Google is connected. Illo can read your mail, calendar, and drive."


def callback_address(text: str, expected_url: str) -> str | None:
    """Accept one plain URL or Slack auto-link at the configured callback endpoint."""
    raw = text.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    if not raw or any(c.isspace() or c in "<>|" for c in raw):
        return None
    try:
        parsed, expected = urlsplit(raw), urlsplit(expected_url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ):
            return None
    except ValueError:
        return None
    return raw


def redact_text(
    text: str, expected_url: str, replacement="[Google sign-in address removed]"
) -> str:
    """Remove real callback addresses without discarding unrelated message text."""
    return re.sub(
        r"https?://[^\s<>|]+",
        lambda match: replacement if callback_address(match[0], expected_url) else match[0],
        text,
    )


def redact_payload(payload, expected_url: str, replacement="[Google sign-in address removed]"):
    if isinstance(payload, str):
        return redact_text(payload, expected_url, replacement)
    if isinstance(payload, dict):
        return {
            key: redact_payload(value, expected_url, replacement) for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(value, expected_url, replacement) for value in payload]
    return payload


def person_dm(event, authority) -> bool:
    return (
        event.channel == "slack"
        and event.kind in {"dm", "message.im"}
        and authority.profile == "person_dm"
        and not authority.tainted
        and authority.person_id is not None
    )


async def prepare_callback(ctx, event, authority):
    # Only a matching DM callback can reach the claim with its code intact.
    # Other messages can continue to the brain after the callback address is removed.
    if person_dm(event, authority) and callback_address(event.text, callback_url(ctx)):
        return None
    return event.model_copy(
        update={
            "text": redact_text(event.text, callback_url(ctx)),
            "payload": redact_payload(event.payload, callback_url(ctx)),
        }
    )


class OAuthLogFilter(logging.Filter):
    def filter(self, record):
        # Access loggers include the query string. Drop OAuth requests before formatting.
        message = record.getMessage()
        return not ("/oauth/google/" in message or "code=" in message or "state=" in message)


async def claim(ctx, event, authority) -> str | None:
    if not person_dm(event, authority):
        return None
    from illo.connectors import chat

    text = event.text.strip().lower().rstrip(".!?")
    pasted = callback_address(event.text, callback_url(ctx))
    if not pasted and text not in chat.PERSONAL_PHRASES | {
        "connect google", "set up google", "disconnect my google",
    }:
        return None
    try:
        if pasted:
            parsed = urlsplit(pasted)
            params = parse_qs(parsed.query, strict_parsing=True)
            if len(params.get("state", [])) != 1 or len(params.get("code", [])) != 1:
                raise ValueError("The redirect address is invalid")
            return await complete(
                ctx, params["state"][0], params["code"][0], person_id=authority.person_id
            )
        if text != "disconnect my google":
            async with ctx.pool.connection() as conn:
                return await chat.start(ctx, conn, authority.person_id, "google",
                                        "personal" if text in chat.PERSONAL_PHRASES else "team",
                                        send=False)
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
    except google.GoogleAccountMismatch:
        return google.ACCOUNT_MISMATCH
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
    ctx.gateway.register_preparer(prepare_callback)
    ctx.gateway.register_payload_filter(lambda payload: redact_payload(payload, callback_url(ctx)))
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(OAuthLogFilter())

    async def start_route(request: Request):
        try:
            nonce = secrets.token_urlsafe(32)
            url = await start(ctx, request.query_params.get("state", ""), browser_nonce=nonce)
            response = RedirectResponse(
                url, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
            response.set_cookie(
                BROWSER_COOKIE,
                nonce,
                max_age=600,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response
        except Exception:
            raise HTTPException(400, "The sign-in link is invalid or expired") from None

    async def callback_route(request: Request):
        try:
            if request.query_params.get("error"):
                raise ValueError("Google sign-in was cancelled")
            text = await complete(
                ctx,
                request.query_params.get("state", ""),
                request.query_params.get("code", ""),
                browser_nonce=request.cookies.get(BROWSER_COOKIE),
            )
            response = PlainTextResponse(
                text, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
            response.delete_cookie(BROWSER_COOKIE, secure=True, httponly=True, samesite="lax")
            return response
        except google.GoogleAccountMismatch:
            raise HTTPException(400, google.ACCOUNT_MISMATCH) from None
        except Exception:
            raise HTTPException(400, "Google sign-in did not finish") from None

    ctx.app.add_api_route("/oauth/google/start", start_route, methods=["GET"])
    ctx.app.add_api_route("/oauth/google/callback", callback_route, methods=["GET"])
