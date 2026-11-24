from datetime import UTC, datetime

import pytest
from slack_sdk.errors import SlackApiError

from illo.db import one
from illo.ledger import people, tokens
from tests.fakes import FakeSlackClient
from tests.mcp.client import AgentClient


@pytest.fixture
def client(app, http):
    async def create(person="ada", *, agent=None, scope="agent"):
        ctx = app.state.ctx
        async with ctx.pool.connection() as conn:
            owner = await people.get(conn, person)
            token = await tokens.create(conn, owner["id"], "Board test", scopes=[scope],
                                        agent_key=agent, workspace=ctx.workspace)
            row = await tokens.verify(conn, token)
        return AgentClient(app, http, token, row["id"], owner["id"])

    return create


@pytest.fixture
def task(app):
    async def create(title="Prepare the release", *, ticket=None, owner=None):
        async with app.state.ctx.pool.connection() as conn:
            row = await one(conn, """INSERT INTO tasks
                (list_key,title,brief,created_by,owner_person_id,visibility)
                VALUES ('bugs',%s,'','person:ada',%s,%s) RETURNING id""",
                (title, owner, "private" if owner else "team"))
            if ticket:
                await conn.execute("INSERT INTO task_tickets(url,task_id) VALUES (%s,%s)",
                                   (ticket, row["id"]))
            return row["id"]

    return create


@pytest.fixture
def coordinator_ctx(app, fake_brain):
    ctx = app.state.ctx
    cfg = ctx.workspace.config.board.coordinator
    cfg.enabled = True
    cfg.quiet_hours = False
    cfg.thread_cooldown_minutes = 0
    cfg.settle_seconds = 0
    ctx.workspace.config.pages.enabled = True

    fake_brain.script = []
    return ctx


class BoardSlack(FakeSlackClient):
    on_send = None
    refused = False
    lose_receipt = False
    lose_history = False

    async def conversations_open(self, **fields):
        await self._record("conversations_open", **fields)
        return {"channel": {"id": "D" + fields["users"]}}

    async def chat_postMessage(self, **fields):
        if self.refused and fields.get("metadata"):
            raise SlackApiError("Refused", {"error": "not_allowed"})
        result = await self._record("chat_postMessage", **fields)
        ts = f"{datetime.now(UTC).timestamp():.6f}"
        self.messages[ts] = {**fields, "ts": ts, "user": "U_ACME_ILLO"}
        if self.on_send and fields.get("metadata"):
            await self.on_send(fields, ts)
        if self.lose_receipt and fields.get("metadata"):
            self.lose_receipt = False
            raise TimeoutError
        return result | {"ts": ts}

    async def conversations_history(self, **fields):
        if self.lose_history:
            self.lose_history = False
            raise TimeoutError
        result = await super().conversations_history(**fields)
        result["messages"] = [m for m in result["messages"] if m["channel"] == fields["channel"]
                              and (not fields.get("latest") or m["ts"] == fields["latest"])]
        return result

    def roots(self):
        return [x for x in self.messages.values() if x.get("metadata")]


@pytest.fixture
def slack(coordinator_ctx, channel):
    ctx = coordinator_ctx
    ctx.gateway.register("slack", channel)
    ctx.slack_client = BoardSlack()
    return ctx.slack_client
