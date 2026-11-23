import asyncio

import httpx
import pytest

from illo.asks import actions, service
from illo.db import all_rows, one
from illo.ledger import asks, people, rules, tokens
from illo.tasks.pages import ask_page, signin
from illo.workspace import PagesConfig

TICKET = "https://github.com/acme-example/engine/issues/12"
MIXED_TICKET = "https://github.com/Acme-Example/Engine/issues/012"
DECISION = {
    "kind": "decision_needed",
    "text": "The release schedule needs a decision.",
    "mentions": ["person:ben"],
    "question": "Which date works for the release?",
    "options": [{"id": "first", "label": "First date"}, {"id": "second", "label": "Second date"}],
    "recommended": "first",
}


@pytest.mark.parametrize("link", ["task", "ticket", "both"])
async def test_concurrent_posts_use_one_canonical_thread(client, task, link):
    ada, ben = await client(), await client("ben")
    task_id = await task(ticket=MIXED_TICKET) if link != "ticket" else None
    first = {"ticket_url": MIXED_TICKET} if link == "ticket" else {"task_id": task_id}
    second = {"ticket_url": TICKET} if link != "task" else first
    a, b = await asyncio.gather(
        ada.call("board_post", {"text": "First update", **first}),
        ben.call("board_post", {"text": "Second update", **second}),
    )
    assert a["thread_id"] == b["thread_id"]
    assert a["post_id"] != b["post_id"]
    result = await ada.call("board_read", {"thread_id": a["thread_id"]})
    assert result["thread"]["task_id"] == task_id
    assert result["thread"]["ticket_url"] == (None if link == "task" else TICKET)
    assert len(result["posts"]) == 2


async def test_ticket_scope_and_task_agreement(client, task):
    ada, ben = await client(), await client("ben")
    task_id = await task(ticket=MIXED_TICKET, owner=ada.person_id)
    other = await task()
    mismatch = await ada.call("board_post", {
        "text": "A mismatched post", "task_id": other, "ticket_url": TICKET,
    }, error=True)
    assert "does not identify" in mismatch["error"]["message"]
    assert await ben.call("board_post", {"ticket_url": TICKET, "text": "Outside"}, error=True) == (
        await ben.call("board_post", {"task_id": 99999, "text": "Outside"}, error=True)
    )
    post = await ada.call("board_post", {
        "text": "Inside", "task_id": task_id, "ticket_url": TICKET, "visibility": "team",
    })
    result = await ada.call("board_read", {"thread_id": post["thread_id"]})
    assert result["thread"]["visibility"] == "private"
    assert result["thread"]["task_id"] == task_id
    for target in ("person:ben", "agent:studio"):
        refused = await ada.call("board_post", {
            "thread_id": post["thread_id"], "text": "Do not send", "mentions": [target],
        }, error=True)
        assert "recipient" in refused["error"]["message"]
    assert (await ben.call("board_read", {"ticket_url": MIXED_TICKET}))["threads"] == []


async def test_team_canonical_thread_does_not_depend_on_owner(app, client, task):
    ada, ben = await client(), await client("ben")
    task_id = await task()
    first = await ada.call("board_post", {"task_id": task_id, "text": "First"})
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_threads SET owner_person_id=%s WHERE id=%s",
                           (ada.person_id, first["thread_id"]))
    second = await asyncio.wait_for(ben.call("board_post", {"task_id": task_id, "text": "Second"}), 3)
    assert second["thread_id"] == first["thread_id"]


@pytest.mark.parametrize("url", [
    TICKET.replace("/issues/", "/pull/"), TICKET + "?", TICKET + "#comment", TICKET + "/",
    TICKET.replace("/12", "/0"), TICKET.replace("/12", "/-1"), TICKET.replace("https", "http"),
    TICKET.replace("github.com", "github.com.example.com"),
    TICKET.replace("github.com", "user@github.com"),
    TICKET.replace("engine", "outside"),
])
async def test_ticket_parser_rejects_invalid_or_unlisted_addresses(client, url):
    ada = await client()
    await ada.call("board_post", {"text": "No ticket here", "ticket_url": url}, error=True)


async def test_free_topics_pagination_search_and_archive(client):
    ada = await client(agent="build")
    initial = await ada.call("board_post", {
        "title": "Searchable release", "text": "Initial finding", "client_name": "test\u202e client",
        "model": "x" * 100, "mentions": ["agent:build"],
    })
    thread_id = initial["thread_id"]
    await ada.call("board_post", {"thread_id": thread_id, "text": "Private", "visibility": "private"},
                   error=True)
    second = await ada.call("board_post", {"thread_id": thread_id, "text": "Second note"})
    page = await ada.call("board_read", {"thread_id": thread_id, "limit": 1})
    assert page["posts"][0]["provenance"] == {
        "agent_key": "build", "person_key": "ada", "token_label": "Board test",
        "client_name": "test client", "model": "x" * 80,
    }
    assert page["posts"][0]["actor"] == "agent:build"
    assert page["posts"][0]["mentions"] == []
    more = await ada.call("board_read", {"thread_id": thread_id, "after_post_id": initial["post_id"]})
    assert [post["id"] for post in more["posts"]] == [second["post_id"]]
    assert more["thread"]["turn_budget_remaining"] == 18
    assert await ada.call("board_read", {"query": "release"}) == {"threads": [more["thread"]]}
    await ada.call("board_resolve", {"thread_id": thread_id, "summary": "Complete"})
    await ada.call("board_post", {"thread_id": thread_id, "text": "Closed"}, error=True)
    assert (await ada.call("board_resolve", {"thread_id": thread_id, "state": "open"}))["state"] == "open"
    await ada.call("board_resolve", {"thread_id": thread_id, "state": "archived"})
    await ada.call("board_resolve", {"thread_id": thread_id, "state": "open"}, error=True)


async def test_only_explicit_human_answer_completes_each_decision(app, client):
    ctx = app.state.ctx
    ctx.workspace.config.board.max_consecutive_agent_posts = 2
    ada = await client()
    first = await ada.call("board_post", {"title": "Release dates", **DECISION})
    thread_id = first["thread_id"]
    second = await ada.call("board_post", {"thread_id": thread_id, **DECISION})
    stalled = await ada.call("board_post", {"thread_id": thread_id, "text": "Extra"}, error=True)
    assert stalled["stalled_reason"] == "turn_cap"
    await ada.call("board_post", {"thread_id": thread_id, "text": "I decide", "kind": "decision"}, error=True)
    await ada.call("board_post", {"thread_id": thread_id, "text": "Spoof", "actor": "person:ben"}, error=True)
    await ada.call("board_resolve", {"thread_id": thread_id}, error=True)
    await ada.call("ask_cancel", {"ask_id": first["ask_id"]}, error=True)
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        rule = await rules.create(conn, owner_person_id=ben["id"], text="Use the first date",
                                  answer_option_label="First date")
        assert await asks.answer_with_rule(conn, first["ask_id"], rule["id"]) is None
        assert await asks.answer(conn, first["ask_id"], person_id=ben["id"], option="first",
                                 via="approved_draft") is None
        assert await service.answer(conn, first["ask_id"], ada.person_id, option="first") is None
        assert not await asks.cancel(conn, first["ask_id"])

    await service.tick(ctx)
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM asks ORDER BY id")
    assert [row["id"] for row in rows] == [first["ask_id"], second["ask_id"]]
    for row in rows:
        assert row["state"] == "open" and not row["allow_rule"]
        assert row["owner_person_id"] == ben["id"] and row["slack_ts"]

    row = rows[0]
    click = {"action_id": f"ask:{row['id']}:opt:second", "channel": row["slack_channel"],
             "message_ts": row["slack_ts"]}
    await actions.handle(ctx, ben, click)
    await actions.handle(ctx, ben, click)
    result = await ada.call("board_read", {"thread_id": thread_id})
    assert result["open_decision_ids"] == [second["post_id"]]
    assert result["thread"]["turn_budget_remaining"] == 2
    assert result["thread"]["stalled_reason"] is None
    decision = result["posts"][-1]
    assert (decision["kind"], decision["actor"], decision["text"], decision["is_human"]) == (
        "decision", "person:ben", "Second date", True,
    )
    assert decision["reply_to_post_id"] == first["post_id"]
    assert sum(post["kind"] == "decision" for post in result["posts"]) == 1

    await ada.call("board_post", {"thread_id": thread_id, "text": "Ben says yes", "kind": "answer",
                                 "reply_to_post_id": second["post_id"]})
    await ada.call("board_resolve", {"thread_id": thread_id, "state": "archived"}, error=True)
    async with ctx.pool.connection() as conn:
        assert await service.answer(conn, second["ask_id"], ben["id"], text="Use the first date.")
    assert (await ada.call("board_resolve", {"thread_id": thread_id}))["state"] == "resolved"


async def test_inbox_ack_and_agent_transfer(app, client):
    sender = await client("ben")
    named = await client(agent="build")
    first, second = await client(), await client()
    post = await sender.call("board_post", {
        "title": "Review results", "text": "Can you review?", "kind": "question",
        "mentions": ["person:ada", "agent:build", "person:ada"],
    })

    async def targets(recipient):
        inbox = await recipient.call("board_inbox", {"include_recent_open": True})
        assert [thread["id"] for thread in inbox["recent_open"]] == [post["thread_id"]]
        return {row["target"] for row in inbox["mentions"]}

    assert await targets(named) == {"person:ada", "agent:build"}
    assert await targets(first) == {"person:ada"}
    await named.call("board_post", {
        "thread_id": post["thread_id"], "text": "Review complete", "kind": "answer",
        "reply_to_post_id": post["post_id"],
    })
    assert not await targets(named)
    assert await targets(first) == {"person:ada"}
    assert await first.call("board_ack", {"thread_id": post["thread_id"], "up_to_post_id": post["post_id"]}) == {
        "acked": 1,
    }
    assert await second.call("board_ack", {"post_ids": [post["post_id"], post["post_id"]]}) == {"acked": 1}
    assert not await targets(first) and not await targets(second)

    ctx = app.state.ctx
    agent = next(agent for agent in ctx.workspace.agents if agent.key == "build")
    agent.owner = "ben"
    moved = await client("ben", agent="build")
    # Even the same reader key cannot receive a mention that belonged to the previous owner.
    assert not await targets(moved)
    await named.call("board_inbox", error=True)
    await sender.call("board_post", {
        "thread_id": post["thread_id"], "text": "New owner", "mentions": ["agent:build"],
    })
    assert await targets(moved) == {"agent:build"}


async def test_request_page_answer_records_a_board_decision(app, client):
    ctx = app.state.ctx
    origin = "https://illo.example.com"
    ctx.workspace.config.pages = PagesConfig(enabled=True, url=origin)
    ada, ben = await client(), await client("ben")
    post = await ada.call("board_post", {"title": "Page decision", **DECISION})
    token = "board-test-web-session"
    ask_id = post["ask_id"]
    async with ctx.pool.connection() as conn:
        await conn.execute("INSERT INTO web_sessions(token_hash,person_id) VALUES (%s,%s)",
                           (tokens.digest(token), ben.person_id))
        ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,))
    path = f"/web/a/{ask_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                cookies={signin.SESSION_COOKIE: token}) as web:
        assert (await web.get(path)).status_code == 200
        response = await web.post(path + "/answer", headers={"Origin": origin},
                                  data={"csrf": signin.csrf(token), "seen": ask_page.seen(ask),
                                        "option": "first"})
        assert response.status_code == 303
    result = await ada.call("board_read", {"thread_id": post["thread_id"]})
    assert result["open_decision_ids"] == []
    assert result["posts"][-1]["kind"] == "decision"
    assert result["posts"][-1]["reply_to_post_id"] == post["post_id"]


async def test_linked_task_visibility_applies_to_all_board_paths(app, client, task):
    ctx = app.state.ctx
    ada, ben = await client(), await client("ben")
    task_id = await task()
    args = {"task_id": task_id, "text": "Secret release", "source_ref": "release",
            "mentions": ["person:ada"]}
    post = await ada.call("board_post", args)
    thread_id, post_id = post["thread_id"], post["post_id"]
    # Set the linked task private without changing the board thread.
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET visibility='private',owner_person_id=%s WHERE id=%s",
                           (ben.person_id, task_id))
    checks = [
        ("board_read", "thread_id", {"thread_id": thread_id}),
        ("board_ack", "post_ids", {"post_ids": [post_id]}),
        ("board_ack", "thread_id", {"thread_id": thread_id, "up_to_post_id": post_id}),
        ("board_ack", "up_to_post_id", {"thread_id": thread_id, "up_to_post_id": post_id}),
        ("board_resolve", "thread_id", {"thread_id": thread_id}),
        ("board_post", "thread_id", {"thread_id": thread_id, "text": "More"}),
    ]
    for name, key, args_for_tool in checks:
        absent = [99999] if key == "post_ids" else 99999
        a = await ada.call(name, args_for_tool, error=True)
        b = await ada.call(name, {**args_for_tool, key: absent}, error=True)
        assert a == b == {"error": {"message": "The board item was not found."}}
    assert await ada.call("board_post", args, error=True) == a
    other = await ada.call("board_post", {"title": "Visible", "text": "Here"})
    reply = {"thread_id": other["thread_id"], "text": "Link", "kind": "answer"}
    assert await ada.call("board_post", {**reply, "reply_to_post_id": post_id}, error=True) == a
    assert await ada.call("board_post", {**reply, "reply_to_post_id": 99999}, error=True) == a
    assert await ada.call("board_read", {"query": "release"}) == {"threads": []}
    inbox = await ada.call("board_inbox", {"include_recent_open": True})
    assert inbox["mentions"] == []
    assert [row["id"] for row in inbox["recent_open"]] == [other["thread_id"]]
    assert "unread_board_mentions" not in await ada.call("lane_search")
    assert (await ben.call("board_read", {"thread_id": thread_id}))["posts"][0]["id"] == post_id


async def test_idempotency_and_hourly_limit_cover_committed_writes(app, client):
    ada, ben = await client(), await client("ben")
    request = {"title": "Same request", "text": "Only once", "source_ref": "retry"}
    first, retry = await asyncio.gather(ada.call("board_post", request),
                                        ada.call("board_post", request))
    assert first == retry
    mismatch = await ada.call("board_post", {**request, "text": "Changed"}, error=True)
    assert "different request" in mismatch["error"]["message"]

    async def ping(title):
        return await ben.call("board_post", {"title": title, "text": "Ping", "mentions": ["person:ada"]})

    mention = await ping("First mention")
    assert (await ada.call("board_ack", {"post_ids": [mention["post_id"]]}))["acked"] == 1
    await ada.call("board_resolve", {"thread_id": first["thread_id"]})
    for i in range(57):
        await ada.call("board_post", {"title": f"Topic {i}", "text": "New request"})

    assert await ada.call("board_post", request) == first
    assert (await ada.call("board_ack", {"post_ids": [mention["post_id"]]}))["acked"] == 0
    later = await ping("Second mention")
    refused = await ada.call("board_post", {"title": "Capped", "text": "Another request"}, error=True)
    assert "60 board changes" in refused["error"]["message"]
    assert (await ada.call("board_ack", {"post_ids": [later["post_id"]]}, error=True)) == refused
    assert (await ada.call("board_resolve", {"thread_id": later["thread_id"]}, error=True)) == refused
    inbox = await ada.call("board_inbox")
    assert [row["post_id"] for row in inbox["mentions"]] == [later["post_id"]]
    async with app.state.ctx.pool.connection() as conn:
        n = await one(conn, "SELECT count(*) AS n FROM audit WHERE tool='board_mutation' AND token_id=%s",
                      (ada.token_id,))
        assert n["n"] == 60


async def test_turn_cap_commits_on_the_twenty_first_post(app, client):
    ada = await client()
    initial = await ada.call("board_post", {"title": "Long exchange", "text": "Post 1"})
    thread_id = initial["thread_id"]
    for i in range(2, 21):
        await ada.call("board_post", {"thread_id": thread_id, "text": f"Post {i}"})
    before = await ada.call("board_read", {"thread_id": thread_id})
    assert before["thread"]["stalled_reason"] is None
    assert before["thread"]["turn_budget_remaining"] == 0

    for _ in range(2):
        refusal = await ada.call("board_post", {"thread_id": thread_id, "text": "Post 21"}, error=True)
        assert refusal["thread_id"] == thread_id
        assert refusal["error"]["code"] == refusal["stalled_reason"] == "turn_cap"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT stalled_reason,consecutive_agent_posts FROM board_threads WHERE id=%s",
                        (thread_id,))
        assert row == {"stalled_reason": "turn_cap", "consecutive_agent_posts": 20}
        assert (await one(conn, "SELECT count(*) AS n FROM board_posts"))["n"] == 20
        assert (await one(conn, "SELECT count(*) AS n FROM audit WHERE tool='board_mutation'"))["n"] == 21


@pytest.mark.parametrize("decision_first", [True, False])
async def test_resolve_and_decision_post_serialize(client, monkeypatch, decision_first):
    from illo.ledger import board

    ada, ben = await client(), await client("ben")
    thread_id = (await ada.call("board_post", {"title": "Decision race", "text": "Start"}))["thread_id"]
    pause, locked, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = board._thread

    async def thread(conn, params, *, lock=False):
        if params["person_id"] == ben.person_id:
            waiting.set()
            return await original(conn, params, lock=lock)
        row = await original(conn, params, lock=lock)
        assert lock
        locked.set()
        await pause.wait()
        return row

    monkeypatch.setattr(board, "_thread", thread)
    posting = {"thread_id": thread_id, **DECISION}
    closing = {"thread_id": thread_id}
    first = asyncio.create_task(ada.call(*(
        ("board_post", posting) if decision_first else ("board_resolve", closing)
    )))
    try:
        await asyncio.wait_for(locked.wait(), 3)
        pending = asyncio.create_task(ben.call(*(
            ("board_resolve", closing) if decision_first else ("board_post", posting)
        ), error=True))
        await asyncio.wait_for(waiting.wait(), 3)
        assert not pending.done()
    finally:
        pause.set()
    receipt, error = await asyncio.wait_for(asyncio.gather(first, pending), 3)
    result = await ada.call("board_read", {"thread_id": thread_id})
    if decision_first:
        assert "answer each open decision" in error["error"]["message"]
        assert result["thread"]["state"] == "open"
        assert result["open_decision_ids"] == [receipt["post_id"]]
    else:
        assert "closed" in error["error"]["message"]
        assert result["thread"]["state"] == "resolved"
        assert result["open_decision_ids"] == []


@pytest.mark.parametrize("blocks", [
    [{"kind": "image", "image": {"url": "https://example.com/picture.png"}}],
    [{"kind": "preview", "name": "Picture", "text": "", "image": {"url": "https://example.com/p.png"}}],
    [{"kind": "preview", "name": "Picture", "text": "",
      "image": {"repo": "example/project", "ref": "main", "path": "picture.png"}}],
    [{"kind": "preview", "name": "Large", "text": "x" * 6000}] * 6,
])
async def test_board_rejects_images_and_oversized_blocks(app, client, blocks):
    ada = await client()
    await ada.call("board_post", {"title": "Rejected", "text": "Check the blocks", "blocks": blocks},
                   error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM board_threads"))["n"] == 0


async def test_board_keeps_supported_text_blocks(client):
    ada = await client()
    blocks = [
        {"kind": "preview", "name": "Status", "text": "Done"},
        {"kind": "checks", "items": [{"claim": "The test passed", "state": "yes"}]},
        {"kind": "links", "items": [{"title": "Report", "url": "https://example.com/report"}]},
    ]
    post = await ada.call("board_post", {"title": "Proof", "text": "See the checks", "blocks": blocks})
    result = await ada.call("board_read", {"thread_id": post["thread_id"]})
    assert [block["kind"] for block in result["posts"][0]["blocks"]] == ["preview", "checks", "links"]
