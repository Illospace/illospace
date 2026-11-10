"""Read-only Google APIs. Authority selects the grant, never a tool argument."""

import asyncio
import base64
import os
import time
from datetime import UTC, datetime
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from illo.connectors import store
from illo.connectors.config import GOOGLE_SCOPES, Connector
from illo.connectors.http import BoundedHTTP, marked
from illo.db import one

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com"


def google_config(ctx, scope: str = "personal") -> Connector | None:
    return next(
        (c for c in ctx.workspace.connectors if c.kind == "google" and c.scope == scope), None
    )


async def credentials(ctx, conn) -> tuple[str, str]:
    client_id = os.environ.get("ILLO_GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("ILLO_GOOGLE_CLIENT_SECRET")
    if not secret:
        secret = await store.vault(ctx).get(conn, "google-client-secret")
    if not client_id or not secret:
        raise ValueError("Set the Google client ID and client secret")
    return client_id, secret


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GoogleClient:
    def __init__(self, http: BoundedHTTP | None = None) -> None:
        self.http = http or BoundedHTTP()

    async def access_token(self, conn, ctx, config: Connector, person_id: int | None) -> str:
        if config.auth and config.auth.type == "service_account":
            import json

            value = await store.vault(ctx).get(conn, config.auth.secret)
            account = json.loads(value)
            # The endpoint is fixed. A credential file cannot choose an outbound URL.
            now = int(time.time())
            claims = {
                "iss": account["client_email"],
                "scope": GOOGLE_SCOPES[2],
                "aud": TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            }
            header = _b64(b'{"alg":"RS256","typ":"JWT"}')
            body = _b64(json.dumps(claims, separators=(",", ":")).encode())
            message = header + b"." + body
            key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
            signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            token = await self.http.request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": (message + b"." + _b64(signature)).decode(),
                },
            )
            return token["access_token"]
        row, token = await store.load(conn, ctx, config.key, person_id, lock=True)
        if (
            token.get("access_token")
            and row["expires_at"]
            and (row["expires_at"] - datetime.now(UTC)).total_seconds() > 60
        ):
            return token["access_token"]
        client_id, client_secret = await credentials(ctx, conn)
        fresh = await self.http.request(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        token.update(fresh)
        await store.save(conn, ctx, config.key, person_id, token, row["scopes"])
        return token["access_token"]

    async def call(
        self, conn, ctx, config: Connector, person_id: int | None, method: str, args: dict
    ) -> dict:
        async with asyncio.timeout(config.timeout_s):
            return await self._call(conn, ctx, config, person_id, method, args)

    async def _call(
        self, conn, ctx, config: Connector, person_id: int | None, method: str, args: dict
    ) -> dict:
        required = GOOGLE_SCOPES[
            0 if method.startswith("mail_") else 1 if method.startswith("calendar_") else 2
        ]
        if required not in config.scopes:
            raise ValueError("The connector does not allow this scope")
        token = await self.access_token(conn, ctx, config, person_id)
        headers = {"Authorization": f"Bearer {token}"}

        async def get(path, params=None):
            return await self.http.request(
                "GET", API + path, headers=headers, params=params, max_bytes=config.max_bytes
            )

        if method == "mail_search":
            data = await get(
                "/gmail/v1/users/me/messages", {"q": args["query"], "maxResults": args["limit"]}
            )
            data = {
                "messages": data.get("messages", []),
                "nextPageToken": data.get("nextPageToken"),
            }
        elif method == "mail_get":
            data = await get(
                f"/gmail/v1/users/me/messages/{quote(args['id'], safe='')}", {"format": "full"}
            )
            payload = data.get("payload", {})
            texts, attachments = [], []

            def walk(part):
                if part.get("filename"):
                    attachments.append(part["filename"])
                elif part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    raw = part["body"]["data"]
                    texts.append(
                        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
                            "utf-8", "replace"
                        )
                    )
                for child in part.get("parts", []):
                    walk(child)

            walk(payload)
            data = {
                "id": data["id"],
                "headers": [
                    h
                    for h in payload.get("headers", [])
                    if h.get("name", "").lower() in {"from", "to", "subject", "date"}
                ],
                "text": "\n".join(texts),
                "attachments": attachments,
            }
        elif method == "calendar_list":
            data = await get(
                "/calendar/v3/calendars/primary/events",
                {
                    "timeMin": args["from"],
                    "timeMax": args["to"],
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 100,
                    "fields": "items(id,summary,description,start,end,location,htmlLink),nextPageToken",
                },
            )
        elif method == "drive_search":
            query = args["query"].replace("\\", "\\\\").replace("'", "\\'")
            q = f"trashed = false and fullText contains '{query}'"
            params = {
                "q": q,
                "pageSize": 50,
                "fields": "files(id,name,mimeType,webViewLink,driveId),nextPageToken",
            }
            if config.scope == "team":
                if config.drive_id:
                    params.update(
                        corpora="drive",
                        driveId=config.drive_id,
                        includeItemsFromAllDrives="true",
                        supportsAllDrives="true",
                    )
                else:
                    params["q"] += (
                        " and (" + " or ".join(f"'{f}' in parents" for f in config.folder_ids) + ")"
                    )
            data = await get("/drive/v3/files", params)
        elif method == "drive_get":
            file_id = quote(args["id"], safe="")
            data = await get(
                f"/drive/v3/files/{file_id}",
                {
                    "fields": "id,name,mimeType,webViewLink,driveId,parents",
                    "supportsAllDrives": "true",
                },
            )
            if config.scope == "team" and not (
                (config.drive_id and data.get("driveId") == config.drive_id)
                or (not config.drive_id and set(data.get("parents", [])) & set(config.folder_ids))
            ):
                raise ValueError("The file is outside the shared drive")
            mime = data.get("mimeType")
            exports = {
                "application/vnd.google-apps.document": "text/plain",
                "application/vnd.google-apps.spreadsheet": "text/csv",
                "application/vnd.google-apps.presentation": "text/plain",
            }
            if mime in exports:
                data["text"] = await get(
                    f"/drive/v3/files/{file_id}/export", {"mimeType": exports[mime]}
                )
            data.pop("parents", None)
        else:
            raise ValueError("The Google tool is unknown")
        return marked(f"google:{config.key}", data)

    async def disconnect(self, conn, ctx, config: Connector, person_id: int) -> bool:
        _, token = await store.load(conn, ctx, config.key, person_id, lock=True)
        revoked = True
        try:
            await self.http.request(
                "POST",
                REVOKE_URL,
                data={"token": token.get("refresh_token", token.get("access_token"))},
            )
        except Exception:
            revoked = False
        # Stop local access even when Google's revoke endpoint is unavailable.
        await conn.execute(
            "DELETE FROM connector_grants WHERE connector_key=%s AND person_id=%s",
            (config.key, person_id),
        )
        return revoked


async def personal_call(ctx, method: str, args: dict) -> dict:
    run = ctx.run
    person = await one(
        ctx.conn, "SELECT id FROM people WHERE 'person:' || key=%s AND active", (run["principal"],)
    )
    if (
        run["tainted"]
        or run["profile"] not in {"person_dm", "agent_request"}
        or not person
        or person["id"] != run["person_id"]
    ):
        raise ValueError("A personal tool needs its owner's run")
    config = google_config(ctx.app)
    if not config:
        raise ValueError("The Google connector is not enabled")
    result = await ctx.app.google_client.call(ctx.conn, ctx.app, config, person["id"], method, args)
    # Optional search providers can enter this handler without a personal Tool declaration.
    await ctx.conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    run["personal_read"] = True
    return result


async def shared_call(ctx, method: str, args: dict) -> dict:
    config = google_config(ctx.app, "team")
    if not config or ctx.run["tainted"] or ctx.run["principal"] == "outsider":
        raise ValueError("The shared drive is not available in this run")
    return await ctx.app.google_client.call(ctx.conn, ctx.app, config, None, method, args)


async def _search_provider(ctx, principal, query, kinds, limit, *, shared=False, mail=False):
    from illo.sources.search import Hit

    config = google_config(ctx, "team" if shared else "personal")
    kind = "mail" if mail else "doc"
    if not config or (kinds is not None and kind not in kinds):
        return []
    if not shared and not principal.startswith("person:"):
        return []
    async with ctx.pool.connection() as conn:
        person_id = None
        if not shared:
            person = await one(
                conn, "SELECT id FROM people WHERE 'person:' || key=%s AND active", (principal,)
            )
            if not person:
                return []
            person_id = person["id"]
        if not (config.auth and config.auth.type == "service_account"):
            grant = await one(
                conn,
                "SELECT id FROM connector_grants WHERE connector_key=%s "
                "AND person_id IS NOT DISTINCT FROM %s AND revoked_at IS NULL",
                (config.key, person_id),
            )
            if not grant:
                return []
        args = {"query": query}
        if mail:
            args["limit"] = max(1, min(limit, 50))
        result = await ctx.google_client.call(
            conn, ctx, config, person_id, "mail_search" if mail else "drive_search", args
        )
    data = result["data"]
    rows = data.get("messages" if mail else "files", [])[:limit]
    return [
        Hit(
            source_ref=f"google:{config.key}:{kind}:{row['id']}",
            kind=kind,
            source="mail" if mail else "drive_shared" if shared else "drive",
            title=row.get("name") or f"Mail message {row['id']}",
            snippet=row.get("mimeType", "Use mail_get with this message ID to read the text."),
            link=row.get("webViewLink"),
            owner=principal[7:] if not shared else None,
            personal=not shared,
        )
        for row in rows
    ]


async def mail_provider(ctx, principal, query, kinds, limit):
    return await _search_provider(ctx, principal, query, kinds, limit, mail=True)


async def drive_provider(ctx, principal, query, kinds, limit):
    return await _search_provider(ctx, principal, query, kinds, limit)


async def shared_provider(ctx, principal, query, kinds, limit):
    return await _search_provider(ctx, principal, query, kinds, limit, shared=True)


async def setup(ctx) -> None:
    ctx.google_client = GoogleClient()
    from illo.sources import search

    register = getattr(search, "register_provider", None)
    if register:
        providers = []
        if google_config(ctx):
            providers.extend((("mail", mail_provider, True), ("drive", drive_provider, True)))
        if google_config(ctx, "team"):
            providers.append(("drive_shared", shared_provider, False))
        for name, fn, personal in providers:
            # Several app instances can share this module. Providers take their app as an argument.
            if getattr(search, "_providers", {}).get(name) != (fn, personal, False):
                register(name, fn, personal=personal, tainted=False)
