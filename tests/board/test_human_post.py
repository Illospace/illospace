"""Check the authenticated human write and its existing wake contract."""

import asyncio

import pytest

from illo.board.labels import actor_label
from illo.db import all_rows, one
from illo.ledger import board, people
from tests.board.test_board import DECISION, TICKET


async def human(ctx, thread, *, source="reply", text="We can proceed.", reply_to=None, key="ada"):
    async with ctx.pool.connection() as conn:
        return await board.human_post(
            conn, workspace=ctx.workspace, person=await people.get(conn, key),
            thread_id=thread, text=text, source_ref=f"web:{source}", via="web", reply_to_post_id=reply_to)


@pytest.mark.parametrize("trigger", ["question", "decision", "stall"])
async def test_human_snapshots_wake_before_reset(app, client, trigger):
    ctx = app.state.ctx
    worker = await client(agent="build")
    args = {"ticket_url": TICKET, "text": "An update", "mentions": ["person:ben", "agent:studio"]}
    if trigger == "question":
        args["kind"] = "question"
    if trigger == "decision":
        args.update(DECISION)
    q = await worker.call("board_post", args)
    if trigger == "stall":
        async with ctx.pool.connection() as conn:
            await conn.execute("""UPDATE board_threads SET stalled_reason='turn_cap',
                stalled_at=clock_timestamp(),consecutive_agent_posts=20 WHERE id=%s""", (q["thread_id"],))
    target = q["post_id"] if trigger == "question" else None
    first = await human(ctx, q["thread_id"], key="ben", reply_to=target)
    replay = await human(ctx, q["thread_id"], key="ben", reply_to=target)
    assert replay == first | {"replayed": True}
    await human(ctx, q["thread_id"], source="more", key="ben")
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM board_posts WHERE id=%s", (first["post_id"],))
        assert row["actor"] == "person:ben" and row["is_human"] and row["token_id"] is None
        assert row["kind"] == ("answer" if target else "message")
        assert row["mentions"] == (["agent:build"] if target else [])
        assert await all_rows(conn, "SELECT target,reader_key FROM board_mention_acks") == [
            {"target": "person:ben", "reader_key": "person:ben"}]
        thread = await one(conn, "SELECT * FROM board_threads WHERE id=%s", (q["thread_id"],))
        assert thread["consecutive_agent_posts"] == 0 and not thread["stalled_reason"] and not thread["stalled_at"]
        wakes = await all_rows(conn, "SELECT * FROM board_wakes WHERE target_kind='github_issue'")
        assert len(wakes) == 1 and wakes[0]["post_id"] == first["post_id"]
        assert wakes[0]["episode_key"].startswith("stall:" if trigger == "stall" else "post:")


async def test_human_replay_race_and_cross_thread_refusal(app, client):
    ctx = app.state.ctx
    agent = await client()
    a = await agent.call("board_post", {"title": "A", "text": "First"})
    b = await agent.call("board_post", {"title": "B", "text": "Second"})
    one_thread = await asyncio.gather(
        human(ctx, a["thread_id"], source="same"), human(ctx, a["thread_id"], source="same"))
    assert len({row["post_id"] for row in one_thread}) == 1
    assert {row["replayed"] for row in one_thread} == {True, False}
    cross_thread = await asyncio.gather(
        human(ctx, a["thread_id"], source="race"), human(ctx, b["thread_id"], source="race"),
        return_exceptions=True)
    assert len([row for row in cross_thread if isinstance(row, dict)]) == 1
    error = next(row for row in cross_thread if isinstance(row, board.BoardError))
    assert str(error) == "This reply key belongs to another thread."


async def test_human_refusals_and_reopen(app, client):
    ctx = app.state.ctx
    agent = await client()
    d = await agent.call("board_post", {"title": "Decision", "text": "Pick one.", **DECISION})
    with pytest.raises(board.BoardError, match="with its buttons"):
        await human(ctx, d["thread_id"], reply_to=d["post_id"])
    p = await agent.call("board_post", {"title": "Personal", "text": "Private", "visibility": "private"})
    for thread_id in (p["thread_id"], 999_999):
        with pytest.raises(board.BoardError, match="The thread was not found."):
            await human(ctx, thread_id, key="ben")
    for closed in ("resolved", "archived"):
        await agent.call("board_resolve", {"thread_id": p["thread_id"], "state": closed})
        with pytest.raises(board.BoardError, match=f"The thread is {closed}"):
            await human(ctx, p["thread_id"])
        async with ctx.pool.connection() as conn:
            row = await one(conn, "SELECT closed_at FROM board_threads WHERE id=%s", (p["thread_id"],))
            assert row["closed_at"]
            person = await people.get(conn, "ada")
            if closed == "archived":
                with pytest.raises(board.BoardError, match="read-only"):
                    await board.human_resolve(conn, workspace=ctx.workspace, person=person, thread_id=p["thread_id"])
            else:
                assert (await board.human_resolve(conn, workspace=ctx.workspace, person=person,
                                                  thread_id=p["thread_id"]))["state"] == "open"
                assert (await one(conn, "SELECT closed_at FROM board_threads WHERE id=%s",
                                  (p["thread_id"],)))["closed_at"] is None


async def test_human_resolve_closes_thread_and_episodes(app, client):
    ctx = app.state.ctx
    agent = await client()
    post = await agent.call("board_post", {"title": "Stale thread", "kind": "question", "text": "Need this?",
                                           "mentions": ["person:ada"]})
    thread_id = post["thread_id"]
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        with pytest.raises(ValueError, match="open or resolve"):
            await board.human_resolve(conn, workspace=ctx.workspace, person=person,
                                      thread_id=thread_id, state="archived")
        result = await board.human_resolve(conn, workspace=ctx.workspace, person=person,
                                           thread_id=thread_id, state="resolved", summary="Not needed")
        assert result["id"] == thread_id and result["state"] == "resolved"
        row = await one(conn, "SELECT state,resolved_by,resolution_summary,closed_at FROM board_threads WHERE id=%s",
                        (thread_id,))
        assert row["state"] == "resolved"
        assert row["resolved_by"] == "person:ada"
        assert row["resolution_summary"] == "Not needed"
        assert row["closed_at"] is not None
        open_episodes = await all_rows(conn, "SELECT id FROM board_episodes WHERE thread_id=%s AND state='open'",
                                       (thread_id,))
        assert open_episodes == []
        pending_esc = await all_rows(
            conn,
            "SELECT id FROM board_escalations WHERE thread_id=%s AND state IN ('pending','sending','sent')",
            (thread_id,),
        )
        assert pending_esc == []


async def test_human_hourly_cap_and_unicode_length(app, client):
    ctx = app.state.ctx
    row = await (await client()).call("board_post", {"title": "Work", "text": "Ready"})
    thread_id = row["thread_id"]
    with pytest.raises(board.BoardError, match="4,000"):
        await human(ctx, thread_id, text="😀" * 4001)
    await human(ctx, thread_id, source="long", text="😀" * 4000 + " <!-- marker -->")
    for n in range(29):
        await human(ctx, thread_id, source=str(n))
    with pytest.raises(board.BoardError, match="30 board changes"):
        await human(ctx, thread_id, source="31")
    assert (await human(ctx, thread_id, source="0"))["replayed"]


async def test_inactive_person_cannot_post_on_a_team_thread(app, client):
    ctx = app.state.ctx
    row = await (await client()).call("board_post", {"title": "Work", "text": "Ready"})
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
        ada = await one(conn, "SELECT * FROM people WHERE key='ada'")
        with pytest.raises(board.BoardError, match="account is inactive"):
            await board.human_post(conn, workspace=ctx.workspace, person=ada, thread_id=row["thread_id"],
                                   text="A reply.", reply_to_post_id=None, source_ref="web:inactive", via="web")
        assert not await one(conn, "SELECT 1 FROM board_posts WHERE is_human")


def test_actor_labels_use_stored_provenance_and_config(app):
    w = app.state.ctx.workspace
    by_key = {"ada": {"name": "Ada Example"}, "ben": {"name": "Ben Example"}}
    post = {"actor": "agent:studio", "is_human": False,
            "provenance": {"agent_key": "studio", "person_key": "ada", "client_name": "codex"}}
    studio = next(a for a in w.agents if a.key == "studio")
    studio.name, studio.owner = "Studio", "ben"
    assert actor_label(w, by_key, post).name == "Ada's Studio"
    assert actor_label(w, by_key, post).subtitle == "Agent · codex"
    w.agents.remove(studio)
    assert actor_label(w, by_key, post).name == "Ada's agent"
    post["provenance"].pop("agent_key")
    assert actor_label(w, by_key, post).name == "Ada's Codex"
    post["provenance"]["client_name"] = "<script>Ben Example" * 20
    assert actor_label(w, by_key, post).subtitle == "Agent"
    assert actor_label(w, by_key, post).name == "Ada's agent"
    assert actor_label(w, by_key, post | {"actor": "illo"}).kind == "illo"
    assert actor_label(w, by_key, post | {"is_human": True, "actor": "person:ben"}).name == "Ben Example"
