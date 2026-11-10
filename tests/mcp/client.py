"""Agent calls use real HTTP handlers and the session's real Postgres."""

import asyncio
import json

from illo.ledger import people, tokens


class AgentClient:
    def __init__(self, app, http, token, token_id, person_id):
        self.app, self.http, self.token = app, http, token
        self.token_id, self.person_id = token_id, person_id

    @classmethod
    async def create(cls, app, http, person="ada", label="acme-laptop"):
        async with app.state.ctx.pool.connection() as conn:
            who = await people.get(conn, person)
            token = await tokens.create(conn, who["id"], label, scopes=["agent"])
            row = await tokens.verify(conn, token)
        return cls(app, http, token, row["id"], who["id"])

    async def rpc(self, method, params=None, *, path="/mcp", headers=None):
        return await self.http.post(
            path,
            headers=headers or {"Authorization": f"Bearer {self.token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )

    async def tool_response(self, name, args=None, **kwargs):
        response = await self.rpc("tools/call", {"name": name, "arguments": args or {}}, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    async def call(self, name, args=None, *, error=False, **kwargs):
        rpc = await self.tool_response(name, args, **kwargs)
        assert "error" not in rpc, rpc
        result = rpc["result"]
        assert result["isError"] is error, result
        assert result["content"][0]["type"] == "text"
        value = json.loads(result["content"][0]["text"])
        assert value == result["structuredContent"]
        return value

    async def settle(self):
        # Wait for gateway work, without joining the app's repeated timers.
        tasks = [
            t
            for t in self.app.state.ctx.scheduler.tasks
            if t.get_coro().__qualname__ in {"Gateway.receive", "deliver_event"}
        ]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
