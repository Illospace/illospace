"""A list link shows only safe task facts for its own person."""

import shutil
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from illo.db import all_rows, one
from illo.ledger import people, runs, tasks
from illo.tasks import list_page
from illo.tools.registry import ToolContext
from illo.workspace import TaskList, load_workspace


@pytest.fixture
async def page_ctx(app, tmp_path):
    ctx = app.state.ctx
    root = tmp_path / "workspace"
    shutil.copytree(ctx.workspace.root, root)
    ctx.workspace = load_workspace(root)
    ctx.workspace.lists = [
        TaskList(key="tasks", name="Tasks"),
        TaskList(key="ada", name="Ada private", owner="ada"),
        TaskList(key="ben", name="Ben private", owner="ben"),
    ]
    return ctx


async def new_key(ctx, person="ada"):
    async with ctx.pool.connection() as conn:
        owner = await people.get(conn, person)
        return await list_page.issue(conn, owner["id"])


async def make(ctx, title, *, who="ada", list_key="tasks", **extra):
    async with ctx.pool.connection() as conn:
        return await tasks.create(
            conn,
            workspace=ctx.workspace,
            viewer=tasks.Viewer(who, "person:" + who, human=True),
            list_key=list_key,
            title=title,
            **extra,
        )


async def test_key_lifecycle_headers_and_inactive_person(page_ctx, http):
    ctx = page_ctx
    key, second = await new_key(ctx), await new_key(ctx)
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM list_links")
        assert len(rows) == 2 and key not in str(rows) and second not in str(rows)
        assert all(len(row["key_hash"]) == 64 for row in rows)
        assert (
            timedelta(hours=23) < rows[0]["expires_at"] - datetime.now(UTC) <= timedelta(hours=24)
        )
    for value in (key, second):
        response = await http.get("/list", params={"k": value})
        assert response.status_code == 200
        for name, expected in list_page.HEADERS.items():
            assert response.headers[name] == expected
        assert "set-cookie" not in response.headers
    for params in ({}, {"k": "wrong"}, {"view": "team"}):
        response = await http.get("/list", params=params)
        assert response.status_code == 404 and "invalid or expired" in response.text
        assert response.headers["cache-control"] == "no-store"
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE list_links SET expires_at=clock_timestamp()-interval '1 second' WHERE key_hash=%s",
            (list_page.digest(key),),
        )
    assert (await http.get("/list", params={"k": key})).status_code == 404
    async with ctx.pool.connection() as conn:
        owner = await people.get(conn, "ada")
        await list_page.revoke(conn, owner["id"])
    assert (await http.get("/list", params={"k": second})).status_code == 404
    third = await new_key(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    assert (await http.get("/list", params={"k": third})).status_code == 404


async def test_visibility_escaping_links_and_no_chat(page_ctx, http):
    ctx = page_ctx
    await make(
        ctx,
        "Team <script>alert(1)</script>",
        brief="Never show this brief",
        links=[
            {"url": "https://docs.example.com/report?a=1&b=2", "kind": "proof"},
            {"url": "http://localhost:8000/test", "kind": "local"},
            {"url": "http://unsafe.example.com/file", "kind": "unsafe"},
        ],
    )
    await make(ctx, "Ada private task", list_key="ada")
    await make(ctx, "Ben private task", list_key="ben", who="ben")
    key = await new_key(ctx)
    response = await http.get("/list", params={"k": key})
    assert "Ada private task" in response.text and "Ben private" not in response.text
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text and "<script>" not in response.text
    )
    assert "Never show this brief" not in response.text
    assert 'rel="noopener noreferrer nofollow"' in response.text
    assert (
        "docs.example.com/report?a=1&amp;b=2" in response.text
        and "http://localhost:8000/test" in response.text
    )
    assert "unsafe.example.com" not in response.text
    assert "prefers-color-scheme" in response.text and 'name="viewport"' in response.text
    response = await http.get("/list", params={"k": await new_key(ctx, "ben")})
    assert "Ben private task" in response.text and "Ada private" not in response.text
    response = await http.get("/list", params={"k": key, "list": "ben"})
    assert "Ben private" not in response.text


async def test_filters_states_time_and_pieces(page_ctx, http):
    ctx = page_ctx
    parent = await make(ctx, "Parent", holder="person:ben")
    await make(ctx, "First piece", holder="agent:build", parent_id=parent["id"])
    await make(ctx, "Second piece", holder="person:ben", parent_id=parent["id"])
    waiting = await make(ctx, "Waits for Ada", holder="person:ben")
    working = await make(
        ctx, "Work now", holder="agent:build", due_at=datetime(2026, 11, 15, 12, tzinfo=UTC)
    )
    review = await make(ctx, "Review now", holder="person:ada")
    done = await make(ctx, "Recent done")
    old = await make(ctx, "Old done")
    await make(ctx, "Only Ben", holder="person:ben")
    await make(ctx, "Ada private task", list_key="ada")
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET waits_for='person:ada' WHERE id=%s", (waiting["id"],))
        await conn.execute(
            "UPDATE tasks SET state='working',last_activity_at=now()-interval '3 hours' WHERE id=%s",
            (working["id"],),
        )
        await conn.execute("UPDATE tasks SET state='review' WHERE id=%s", (review["id"],))
        await conn.execute("UPDATE tasks SET state='done',done_at=now() WHERE id=%s", (done["id"],))
        await conn.execute(
            "UPDATE tasks SET state='done',done_at=now()-interval '8 days' WHERE id=%s",
            (old["id"],),
        )
    key = await new_key(ctx)
    response = await http.get("/list", params={"k": key})
    for state in ("to do", "in work", "in review", "done"):
        assert "State: " + state in response.text
    assert "2026-11-15 13:00 CET" in response.text
    assert "Silent hours: 3.0" in response.text
    assert "Old done" not in response.text
    assert (
        response.text.index("Work now")
        < response.text.index("Done in the last 7 days")
        < response.text.index("Recent done")
    )
    assert (
        response.text.index("Parent")
        < response.text.index("<ul><li><h3>#", response.text.index("Parent"))
        < response.text.index("First piece")
    )
    assert "Open pieces: 2" in response.text
    response = await http.get("/list", params={"k": key, "view": "mine"})
    assert "Parent" in response.text and "First piece" in response.text
    assert "Only Ben" not in response.text and "Second piece" not in response.text
    assert "Waits for Ada" in response.text and "Work now" in response.text
    response = await http.get("/list", params={"k": key, "list": "ada"})
    assert "Ada private task" in response.text and "Work now" not in response.text
    assert parent["id"]


async def test_more_than_one_ledger_page(page_ctx, http):
    ctx = page_ctx
    async with ctx.pool.connection() as conn:
        await conn.execute("""INSERT INTO tasks(list_key,title,brief,created_by)
            SELECT 'tasks','Task number '||n,'','person:ada' FROM generate_series(1,105) n""")
    response = await http.get("/list", params={"k": await new_key(ctx)})
    assert response.text.count("<li><h3>#") == 105


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("javascript:alert(1)", False),
        ("https://docs.example.com/x", True),
        ("http://127.0.0.1/x", True),
        ("http://127.0.0.1.evil.example/x", False),
        ("https://key@docs.example.com/x", False),
        ("//docs.example.com/x", False),
        ('https://docs.example.com/"onclick="bad', True),
    ],
)
def test_safe_link(url, allowed):
    assert list_page.safe_link(url) is allowed


async def test_list_tool_channel_sends_dm_and_revoke(page_ctx, replay, fake_brain):
    ctx = page_ctx
    fake_brain.script = [{"name": "list_link", "arguments": {}}]
    channel = ctx.workspace.config.slack.channels[0]
    await replay(
        kind="message",
        text="Give me my list link",
        payload={"channel_id": channel.id, "mentioned": True},
    )
    calls = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage"]
    message = next(c for c in calls if "/list?k=" in c["text"])
    assert message["channel"].startswith("D")
    assert all("/list?k=" not in c["text"] for c in calls if not c["channel"].startswith("D"))
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "list_link", {})
        url = result["result"]
        assert parse_qs(urlsplit(url).query)["k"]
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "list_link", {"revoke": True})
        assert result["result"] == "Your list links are revoked."
        assert not await one(conn, "SELECT * FROM list_links")
