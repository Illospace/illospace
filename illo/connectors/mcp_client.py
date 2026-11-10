"""Team MCP connectors. A remote server cannot add its own authority."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from illo.connectors import store
from illo.connectors.http import BoundedHTTP, marked
from illo.ledger import sensors


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.limit:
                raise ValueError("The MCP response is too large")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class LimitedTransport(httpx.AsyncBaseTransport):
    def __init__(self, limit, inner=None):
        self.inner = inner or httpx.AsyncHTTPTransport()
        self.limit = limit

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        response.stream = LimitedStream(response.stream, self.limit)
        return response

    async def aclose(self):
        await self.inner.aclose()


class MCPClient:
    def __init__(self, session_factory=None, http=None, transport=None):
        self.schemas = {}
        self.transport = transport
        self.session_factory = session_factory or self.session
        self.http = http or BoundedHTTP()

    async def bearer(self, ctx, conn, config) -> str | None:
        auth = config.auth
        if not auth:
            return None
        if auth.type == "bearer":
            value = await store.vault(ctx).get(conn, auth.secret)
            if not value:
                raise ValueError("The MCP bearer secret is missing")
            return value
        if auth.type != "oauth":
            raise ValueError("The MCP auth type is invalid")
        try:
            row, token = await store.load(conn, ctx, config.key, None, lock=True)
        except ValueError:
            if auth.secret:
                raw = await store.vault(ctx).get(conn, auth.secret)
                if not raw:
                    raise ValueError("The MCP OAuth secret is missing") from None
                token = json.loads(raw)
            elif auth.client_id and auth.client_secret and auth.token_url:
                token = {}
            else:
                raise ValueError("Run illo connector login for this connector") from None
            row = {"expires_at": None, "scopes": auth.scopes}
        expires = row["expires_at"]
        if token.get("access_token") and (
            not expires or (expires - datetime.now(UTC)).total_seconds() > 60
        ):
            return token["access_token"]
        endpoint = auth.token_url or token.get("token_url")
        if not endpoint:
            raise ValueError("The MCP token endpoint is missing")
        from illo.connectors.config import validate_url

        validate_url(endpoint)
        data = {"client_id": auth.client_id or token.get("client_id"), "resource": config.url}
        if token.get("refresh_token"):
            data.update(grant_type="refresh_token", refresh_token=token["refresh_token"])
        else:
            data.update(grant_type="client_credentials", scope=" ".join(auth.scopes))
        if auth.client_secret:
            data["client_secret"] = await store.vault(ctx).get(conn, auth.client_secret)
        fresh = await self.http.request("POST", endpoint, data=data)
        token.update(fresh)
        await store.save(conn, ctx, config.key, None, token, row["scopes"])
        return token["access_token"]

    @asynccontextmanager
    async def session(self, config, bearer):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        # Bound the wire response too, before the SDK parses JSON or SSE.
        async with httpx.AsyncClient(
            headers=headers,
            transport=LimitedTransport(config.max_bytes, self.transport),
            timeout=config.timeout_s,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(config.url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session

    async def _schemas(self, session, config) -> dict:
        key = config.model_dump_json()
        if key not in self.schemas:
            allowed = {tool.name for tool in config.tools}
            schemas, cursor, seen = {}, None, set()
            for _ in range(100):
                listing = (
                    await session.list_tools(cursor=cursor)
                    if cursor
                    else await session.list_tools()
                )
                schemas.update(
                    {tool.name: tool.inputSchema for tool in listing.tools if tool.name in allowed}
                )
                if len(json.dumps(schemas).encode()) > config.max_bytes:
                    raise ValueError("The connector schemas are too large")
                cursor = listing.nextCursor
                if not cursor:
                    self.schemas[key] = schemas
                    break
                if cursor in seen:
                    raise ValueError("The connector repeats a tool page")
                seen.add(cursor)
            else:
                raise ValueError("The connector has too many tool pages")
        return self.schemas[key]

    async def catalog(self, ctx, conn, config) -> dict:
        key = config.model_dump_json()
        if key in self.schemas:
            return self.schemas[key]
        async with asyncio.timeout(config.timeout_s):
            bearer = await self.bearer(ctx, conn, config)
            async with self.session_factory(config, bearer) as session:
                return await self._schemas(session, config)

    async def call(self, ctx, conn, config, name: str, arguments: dict) -> dict:
        if name not in {tool.name for tool in config.tools}:
            raise ValueError("The connector file does not list this tool")
        try:
            async with asyncio.timeout(config.timeout_s):
                bearer = await self.bearer(ctx, conn, config)
                async with self.session_factory(config, bearer) as session:
                    schemas = await self._schemas(session, config)
                    if name not in schemas:
                        raise ValueError("The remote server does not list this tool")
                    result = await session.call_tool(name, arguments)
                    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if len(json.dumps(data).encode()) > config.max_bytes:
                        raise ValueError("The MCP result is too large")
            await sensors.ok(conn, f"connector:{config.key}")
            return marked(f"connector:{config.key}", data)
        except Exception:
            raise ValueError("The connector call failed") from None


async def perform_draft(ctx, draft: dict) -> dict:
    if draft["state"] != "approved":
        raise ValueError("Only an approved draft can call a connector")
    args = draft["content"]["arguments"]
    from illo.tools.connectors import find

    config, _ = find(ctx, args)
    async with ctx.pool.connection() as conn:
        return await ctx.mcp_client.call(ctx, conn, config, args["tool"], args["arguments"])


async def setup(ctx) -> None:
    ctx.mcp_client = MCPClient()
    ctx.draft_performers["connector_call"] = perform_draft
    ctx.draft_performers["connectors_call"] = perform_draft
