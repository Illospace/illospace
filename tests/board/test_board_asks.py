"""Keep every board answer behind an authenticated channel action."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from illo.asks import actions, service
from illo.board import coordinator, escalate
from illo.db import all_rows, one
from illo.ledger import asks, board, people
from illo.tools import asks as tools
from illo.tools.registry import ToolContext
from tests.asks.test_request_pages import click, login
from tests.board.test_board import DECISION
from tests.board.test_coordinator import action, admission
from tests.board.test_escalate import escalation, queue, reply
from tests.tasks.test_pages import fields


@pytest.mark.parametrize("kind", ["old_m1", "new_m1", "m3"])
async def test_ask_guards_block_approved_drafts_and_rules(coordinator_ctx, client, kind):
    ctx = coordinator_ctx
    agent = await client()
    q = await agent.call("board_post", {"title": "Work", "text": "What next?", **DECISION})
    ask_id = q["ask_id"]
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,))
        assert row["source_ref"] == f"board:{q['thread_id']}:{q['post_id']}:decision"
        assert not row["allow_rule"]
        if kind == "old_m1":
            await conn.execute("UPDATE asks SET source_ref=NULL WHERE id=%s", (ask_id,))
        if kind == "m3":
            row = await asks.create(conn, owner_person_id=row["owner_person_id"], question="How?",
                                    options=row["options"], source_ref=f"board:{q['thread_id']}:0:esc:99")
            ask_id = row["id"]
        await conn.execute("UPDATE asks SET allow_rule=true WHERE id=%s", (ask_id,))
        assert await asks.answer(conn, ask_id, person_id=row["owner_person_id"], option=row["options"][0]["id"],
                                 via="approved_draft") is None
        assert await asks.answer_with_rule(conn, ask_id, 1) is None
        owner = await one(conn, "SELECT key FROM people WHERE id=%s", (row["owner_person_id"],))
        context = ToolContext(conn, {"person_id": row["owner_person_id"], "tainted": False,
                                     "principal": f"person:{owner['key']}"}, ctx)
        args = {"id": ask_id, "option": row["options"][0]["id"], "text": None}
        assert (await tools.answer_preflight(context, args)).outcome == "deny"
        with pytest.raises(ValueError, match="open for this owner"):
            await tools.asks_answer(context, args)
        with pytest.raises(ValueError, match="with its buttons"):
            await tools.asks_answer_with_rule(context, {"id": ask_id, "rule_id": 1})
        assert (await one(conn, "SELECT state FROM asks WHERE id=%s", (ask_id,)))["state"] == "open"


class TraceConnection:
    def __init__(self, conn, seen):
        self.conn, self.seen = conn, seen

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def execute(self, query, *args, **kwargs):
        if isinstance(query, str) and "FOR UPDATE" in query:
            for table in ("board_threads", "board_episodes", "board_escalations", "asks"):
                if f"FROM {table} " in query:
                    self.seen.append(table)
        return await self.conn.execute(query, *args, **kwargs)


@pytest.mark.parametrize("caller", ["button", "text", "web", "service"])
async def test_every_ask_answer_uses_the_same_lock_order(coordinator_ctx, slack, client, http, monkeypatch, caller):
    ctx = coordinator_ctx
    await queue(ctx, client, "card", summary=True)
    await escalate.send_due(ctx)
    ask_id = (await escalation(ctx))["ask_id"]
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,))
        ada = await people.get(conn, "ada")
        if caller == "text":
            await conn.execute("UPDATE asks SET awaiting_text=true WHERE id=%s", (ask_id,))
    if caller == "web":
        await login(ctx, http, "ada")
        path = f"/web/a/{ask_id}"
        form = fields(await http.get(path))
    seen = []
    original = board.lock_for_ask

    async def trace(conn, ask_id):
        return await original(TraceConnection(conn, seen), ask_id)

    monkeypatch.setattr(board, "lock_for_ask", trace)
    if caller == "button":
        await actions.handle(ctx, ada, click(row, "opt:o1"))
    elif caller == "text":
        assert (await ctx.gateway.receive(reply(slack.roots()[0])))["text"] == "Answer saved."
    elif caller == "web":
        response = await http.post(path + "/answer", data={**form, "option": "o1"})
        assert response.status_code == 303
    else:
        async with ctx.pool.connection() as conn:
            await service.answer(conn, ask_id, ada["id"], option="o1", workspace=ctx.workspace)
    assert seen[:4] == ["board_threads", "board_episodes", "board_escalations", "asks"]
    assert (await escalation(ctx))["state"] == "answered"


async def test_card_button_reports_board_error_and_refreshes(coordinator_ctx, slack, client, monkeypatch):
    ctx = coordinator_ctx
    await queue(ctx, client, "card", summary=True)
    await escalate.send_due(ctx)
    pending = await escalation(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (pending["ask_id"],))
        ada = await people.get(conn, "ada")
        await conn.execute("INSERT INTO agent_oauth_limits (kind,key,attempts) VALUES ('board_human',%s,30)",
                           (str(ada["id"]),))
    refresh = AsyncMock(wraps=actions.refresh_ask)
    monkeypatch.setattr(actions, "refresh_ask", refresh)

    await actions.handle(ctx, ada, click(row, "opt:o1"))

    refresh.assert_awaited_once_with(ctx, row["id"])
    [notice] = [m for m in slack.messages.values() if m.get("thread_ts") == row["slack_ts"]]
    assert "30 board changes" in notice["text"]
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM asks WHERE id=%s", (row["id"],)))["state"] == "open"
        assert not await one(conn, "SELECT 1 FROM board_posts WHERE is_human")


@pytest.mark.parametrize("kind", ["decision", "card", "regular"])
async def test_answer_returns_the_exact_board_post_id(coordinator_ctx, slack, client, kind):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
    if kind == "card":
        thread = await queue(ctx, client, "card", summary=True)
        await escalate.send_due(ctx)
        ask_id = (await escalation(ctx))["ask_id"]
    elif kind == "decision":
        thread = await (await client()).call("board_post", {"title": "Work", "text": "Choose.", **DECISION})
        ask_id = thread["ask_id"]
    else:
        async with ctx.pool.connection() as conn:
            ask_id = (await asks.create(conn, owner_person_id=ada["id"], question="How?", options=[]))["id"]
    async with ctx.pool.connection() as conn:
        ask = await one(conn, "SELECT owner_person_id,options FROM asks WHERE id=%s", (ask_id,))
        answer = {"option": ask["options"][0]["id"]} if ask["options"] else {"text": "Done."}
        answered = await service.answer(conn, ask_id, ask["owner_person_id"], workspace=ctx.workspace, **answer)
    if kind == "regular":
        assert answered["state"] == "answered" and "board_post_id" not in answered
        return
    async with ctx.pool.connection() as conn:
        post = await one(conn, "SELECT thread_id,reply_to_post_id,is_human FROM board_posts WHERE id=%s",
                         (answered["board_post_id"],))
    assert post == {"thread_id": thread["thread_id"], "reply_to_post_id": thread["post_id"], "is_human": True}


async def test_card_m1_answer_web_reply_and_apply_share_one_lock_order(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    async with ctx.pool.connection() as conn:
        ada, ben = await people.get(conn, "ada"), await people.get(conn, "ben")
    for i in range(50):
        # Use fresh rows so every operation competes for the same thread lock.
        q = await queue(ctx, client, "card", summary=True)
        await escalate.send_due(ctx)
        card = await escalation(ctx)
        d = await (await client()).call("board_post", {"thread_id": q["thread_id"], "text": "Choose.", **DECISION})
        await service.tick(ctx)
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE board_episodes SET next_check_at=now() WHERE id=%s", (card["episode_id"],))
            m1 = await one(conn, "SELECT * FROM asks WHERE id=%s", (d["ask_id"],))
            m3 = await one(conn, "SELECT * FROM asks WHERE id=%s", (card["ask_id"],))
            # Keep the test within the route's configured hourly budget and token limits.
            await conn.execute("DELETE FROM agent_oauth_limits WHERE kind='board_human'")
            await conn.execute("DELETE FROM audit WHERE tool IN ('board_mutation','asks_create')")
            await conn.execute("UPDATE board_coordinator_runs SET created_at=now()-interval '2 days'")
            await conn.execute("UPDATE board_escalations SET created_at=now()-interval '2 days'")
        # Add a candidate run that can become stale while the three replies compete.
        event = await admission(ctx)

        async def web_reply():
            async with ctx.pool.connection() as conn:
                return await board.human_post(
                    conn, workspace=ctx.workspace, person=ben, thread_id=q["thread_id"], text="Done.",
                    reply_to_post_id=q["post_id"], source_ref=f"web:parallel-{i}", via="web")

        await asyncio.wait_for(asyncio.gather(
            actions.handle(ctx, ada, click(m3, "opt:o1")),
            actions.handle(ctx, ben, click(m1, "opt:first")),
            web_reply(),
            coordinator.apply(ctx, event, action(event, "wait")),
        ), 8)
        async with ctx.pool.connection() as conn:
            posts = await all_rows(conn, "SELECT kind FROM board_posts WHERE thread_id=%s AND is_human",
                                   (q["thread_id"],))
            assert posts
