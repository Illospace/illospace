"""Team MCP connectors. A remote server cannot add its own authority."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from illo.connectors import discovery, store
from illo.connectors.addresses import PublicTransport, public_address
from illo.connectors.http import BoundedHTTP, HTTPFailure, marked, private_http_logs
from illo.db import one
from illo.ledger import runs, sensors
from illo.tools.registry import Refusal


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


class SignInEnded(Refusal):
    def __init__(self, text, row):
        super().__init__(text)
        self.row = row


async def record_ended(ctx, conn, config, failure):
    row = await one(
        conn,
        """UPDATE connector_grants SET broken_at=COALESCE(broken_at,clock_timestamp())
        WHERE id=%s AND secret=%s RETURNING id""",
        (failure.row["id"], failure.row["secret"]),
    )
    if row:
        await sensors.error(conn, "connector:" + config.key, "The connector sign-in ended")


class MCPClient:
    def __init__(self, session_factory=None, http=None, transport=None):
        self.schemas = {}
        self.transport = transport
        self.session_factory = session_factory or self.session
        self.http = http or BoundedHTTP(public=True)

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

        async def ended():
            person = await one(
                conn, "SELECT name FROM people WHERE id=%s", (token.get("signed_in_by"),)
            )
            name = person["name"] if person else "The owner"
            failure = SignInEnded(
                f"The sign-in to {config.key} ended. {name} must sign in again.", row
            )
            await record_ended(ctx, conn, config, failure)
            raise failure

        if row.get("broken_at"):
            await ended()
        expires = row["expires_at"]
        if token.get("access_token") and (
            not expires
            or (expires - datetime.now(UTC)).total_seconds()
            > (60 if token.get("refresh_token") else 0)
        ):
            return token["access_token"]
        if token.get("access_token") and not token.get("refresh_token"):
            await ended()
        endpoint = auth.token_url or token.get("token_url")
        if not endpoint:
            raise ValueError("The MCP token endpoint is missing")
        public_address(endpoint)
        data = {
            "client_id": auth.client_id or token.get("client_id"),
            "resource": token.get("resource", config.url),
        }
        if token.get("refresh_token"):
            data.update(grant_type="refresh_token", refresh_token=token["refresh_token"])
        else:
            data.update(grant_type="client_credentials", scope=" ".join(auth.scopes))
        client_secret = (
            await store.vault(ctx).get(conn, auth.client_secret)
            if auth.client_secret
            else token.get("client_secret")
        )
        try:
            fresh = await discovery.token_request(
                self.http,
                {
                    "token_endpoint": endpoint,
                    "token_endpoint_auth_method": token.get(
                        "token_auth_method", "client_secret_post" if client_secret else "none"
                    ),
                },
                data,
                client_secret,
            )
        except HTTPFailure as exc:
            if exc.error == "invalid_grant" and row.get("id"):
                await ended()
            raise
        if not isinstance(fresh, dict) or not fresh.get("access_token"):
            raise ValueError("The server did not provide an access token")
        if fresh.get("scope") and not set(fresh["scope"].split()) <= set(row["scopes"]):
            raise ValueError("The server returned a scope that Illo does not allow")
        token.update(fresh)
        await store.save(conn, ctx, config.key, None, token, row["scopes"])
        return token["access_token"]

    @asynccontextmanager
    async def session(self, config, bearer):
        public_address(config.url)
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        # Bound the wire response too, before the SDK parses JSON or SSE.
        async with httpx.AsyncClient(
            headers=headers,
            transport=LimitedTransport(config.max_bytes, PublicTransport(self.transport)),
            timeout=config.timeout_s,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            with private_http_logs():
                async with streamable_http_client(config.url, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        yield session

    async def private_values(self, ctx, conn, config, bearer):
        values = [bearer]
        if config.auth and config.auth.type == "oauth":
            try:
                _, token = await store.load(conn, ctx, config.key, None)
            except ValueError:
                token = {}
            values.extend(
                token.get(key)
                for key in ("access_token", "refresh_token", "id_token", "client_secret")
            )
            if config.auth.client_secret:
                values.append(await store.vault(ctx).get(conn, config.auth.client_secret))
        return [value for value in values if isinstance(value, str) and value]

    @staticmethod
    def check_private(data, private):
        if isinstance(data, str):
            if any(value in data for value in private):
                raise ValueError("The server returned a private value")
        elif isinstance(data, dict):
            for key, value in data.items():
                MCPClient.check_private(key, private)
                MCPClient.check_private(value, private)
        elif isinstance(data, (list, tuple)):
            for value in data:
                MCPClient.check_private(value, private)

    async def _schemas(self, session, config, private=()) -> dict:
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
                self.check_private(schemas, private)
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
        async with asyncio.timeout(config.timeout_s):
            bearer = await self.bearer(ctx, conn, config)
            private = await self.private_values(ctx, conn, config, bearer)
            if key in self.schemas:
                self.check_private(self.schemas[key], private)
                return self.schemas[key]
            async with self.session_factory(config, bearer) as session:
                return await self._schemas(session, config, private)

    async def discover(self, ctx, conn, config, *, bearer=None) -> dict[str, str]:
        """Read the bounded tool list without granting a tool."""
        async with asyncio.timeout(config.timeout_s):
            if bearer is None:
                bearer = await self.bearer(ctx, conn, config)
            async with self.session_factory(config, bearer) as session:
                tools, cursor, seen = {}, None, set()
                for _ in range(100):
                    page = (
                        await session.list_tools(cursor=cursor)
                        if cursor
                        else await session.list_tools()
                    )
                    for tool in page.tools:
                        if (
                            not tool.name
                            or len(tool.name) > 200
                            or any(c.isspace() or ord(c) < 32 for c in tool.name)
                        ):
                            raise ValueError("The server lists an invalid tool name")
                        tools[tool.name] = tool.description or ""
                    if len(json.dumps(tools).encode()) > config.max_bytes:
                        raise ValueError("The tool list is too large")
                    cursor = page.nextCursor
                    if not cursor:
                        return tools
                    if cursor in seen:
                        raise ValueError("The connector repeats a tool page")
                    seen.add(cursor)
                raise ValueError("The connector has too many tool pages")

    async def call(self, ctx, conn, config, name: str, arguments: dict) -> dict:
        if name not in {tool.name for tool in config.tools}:
            raise ValueError("The connector file does not list this tool")
        try:
            async with asyncio.timeout(config.timeout_s):
                bearer = await self.bearer(ctx, conn, config)
                async with self.session_factory(config, bearer) as session:
                    private = await self.private_values(ctx, conn, config, bearer)
                    schemas = await self._schemas(session, config, private)
                    self.check_private(schemas, private)
                    if name not in schemas:
                        raise ValueError("The remote server does not list this tool")
                    result = await session.call_tool(name, arguments)
                    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                    self.check_private(data, private)
                    if len(json.dumps(data).encode()) > config.max_bytes:
                        raise ValueError("The MCP result is too large")
            await sensors.ok(conn, f"connector:{config.key}")
            return marked(f"connector:{config.key}", data)
        except SignInEnded:
            raise
        except Exception:
            raise ValueError("The connector call failed") from None


async def perform_draft(ctx, draft: dict) -> dict:
    if draft["state"] != "approved":
        raise ValueError("Only an approved draft can call a connector")
    from illo.tools.connectors import call_connector, resolve
    from illo.tools.registry import ToolContext

    tool = ctx.registry.tools["connectors_call"]
    args = tool.arguments.model_validate(draft["content"]["arguments"]).model_dump()
    if draft["kind"] != tool.name or draft["content"].get("tool") != tool.name:
        raise ValueError("The draft does not contain a connector call")
    async with ctx.pool.connection() as conn:
        run = await runs.get(conn, draft["run_id"])
        if not run or run["person_id"] != draft["approver_person_id"]:
            raise ValueError("The draft approver is not the requester")
        tc = ToolContext(conn, run, ctx)
        if resolve(tc, args).destination(args) != draft["destination"]:
            raise ValueError("The draft destination does not match the stored action")
        return await call_connector(tc, args)


async def setup(ctx) -> None:
    ctx.mcp_client = MCPClient()
    ctx.draft_performers["connectors_call"] = perform_draft
