"""The two example lists use the same tools and ledger rules."""

import pytest

from illo.db import one
from illo.ledger import people, runs, tasks, tokens
from illo.tools.registry import ToolContext
from illo.workspace import TaskList
from tests.mcp.client import AgentClient


async def agent(app, http, key, owner="ada"):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, owner)
        raw = await tokens.create(conn, person["id"], "example-agent", scopes=["agent"],
                                  agent_key=key, workspace=ctx.workspace)
        token = await tokens.verify(conn, raw)
    return AgentClient(app, http, raw, token["id"], person["id"])


async def operator(app, name, **args):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"], profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn, run, ctx), name, args)
    assert result["decision"] == "allow", result
    return result["result"]


async def test_bugs_end_to_end(app, http):
    build = await agent(app, http, "build")
    row = await operator(app, "task_create", list="bugs", title="Repair a fault", brief="The preview fails.")
    assert row["holder"] == "agent:build"
    mine = await build.call("task_mine")
    assert mine["tasks"][0]["id"] == row["id"] and mine["tasks"][0]["new_for_you"]
    read = await build.call("task_get", {"id": row["id"]})
    assert read["brief"] == "The preview fails."
    row = await build.call("task_write", {"id": row["id"], "result": "The repair passes.",
        "result_version": read["result_version"], "message": "The checks pass. The link has the proof.",
        "links": [{"url": "https://example.com/fault/1", "kind": "ticket", "title": "Proof"}], "returned": True})
    assert row["state"] == "done" and row["done_by"] == "code"
    assert row["result"].startswith('<outside_data source="task-result">')
    assert row["links"][0]["title"].startswith('<outside_data')
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM audit WHERE token_id=%s AND decision='allow'", (build.token_id,)))["n"] == 3
        events = await tasks.events(conn, row["id"], viewer=tasks.Viewer("ada", "person:ada"))
        assert len([e for e in events if e["kind"] == "result"]) == 1
        assert len([e for e in events if e["kind"] == "message"]) == 1


async def test_launches_six_pieces_end_to_end(app, http):
    build = await agent(app, http, "build")
    studio = await agent(app, http, "studio", "ben")
    instructions = await operator(app, "lists", key="launches")
    assert "six pieces" in instructions["lists"][0]["body"]
    pieces = [("Script", "agent:studio"), ("Screen recording", "person:ben"),
              ("Edit", "agent:studio"), ("Title and description", "agent:studio"),
              ("Post", "person:ben"), ("Video on the page", "agent:build")]
    parent = await operator(app, "task_create", list="launches", title="Launch the new feature, Friday",
        brief="Show the feature and link its page.", pieces=[{"title": title, "holder": holder} for title, holder in pieces])
    assert len(parent["pieces"]) == 6
    # Ben gives his two pieces to his studio agent through the same hand-over tool.
    for piece in parent["pieces"]:
        if piece["holder"] == "person:ben":
            await operator(app, "task_hand", id=piece["id"], holder="agent:studio")
        caller = build if piece["holder"] == "agent:build" else studio
        read = await caller.call("task_get", {"id": piece["id"]})
        row = await caller.call("task_write", {"id": piece["id"], "result": "The piece is ready.",
            "result_version": read["result_version"], "message": "Please confirm this piece.", "returned": True})
        assert row["state"] == "review" and row["waits_for"] == "person:ada"
        await operator(app, "task_done", id=piece["id"])
    row = await operator(app, "task_get", id=parent["id"])
    assert row["state"] == "review" and row["waits_for"] == "person:ada"
    row = await operator(app, "task_done", id=parent["id"])
    assert row["state"] == "done"


async def test_agent_refusals_and_capture(app, http):
    build = await agent(app, http, "build")
    studio = await agent(app, http, "studio", "ben")
    row = await operator(app, "task_create", list="launches", title="Make a video", holder="agent:build")
    for name in ("task_done", "task_hand"):
        refused = await build.tool_response(name, {"id": row["id"]})
        assert refused["error"]["code"] == -32602
    await build.call("task_write", {"id": row["id"], "append": "No message"}, error=True)
    value = await studio.call("task_write", {"id": row["id"], "append": "No", "message": "No"}, error=True)
    assert "handed to someone else" in value["error"]["message"]
    captured = await build.call("task_create", {"list": "launches", "title": "A stated task"})
    assert captured["created_by"] == "agent:build" and captured["holder"] is None
    await build.call("task_create", {"list": "launches", "title": "No hand-over", "holder": "agent:studio"}, error=True)
    piece = await build.call("task_create", {"list": "launches", "title": "An edit", "parent": row["id"], "holder": "agent:studio"})
    assert piece["holder"] == "agent:studio"
    await operator(app, "task_hand", id=row["id"], holder="agent:studio")
    value = await build.call("task_post", {"id": row["id"], "text": "A question?"}, error=True)
    assert "end the run" in value["error"]["message"]


async def test_private_list_and_token_identity(app, http):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="private", name="Private", owner="ada"))
    build = await agent(app, http, "build")
    studio = await agent(app, http, "studio", "ben")
    row = await operator(app, "task_create", list="private", title="Private plan", holder="agent:build")
    assert row["visibility"] == "private"
    await studio.call("task_get", {"id": row["id"]}, error=True)
    lists = await studio.call("task_lists")
    assert "private" not in {entry["key"] for entry in lists["lists"]}
    await build.call("task_get", {"id": row["id"]})
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        for key in ("build", "missing"):
            with pytest.raises(ValueError, match="agent must exist"):
                await tokens.create(conn, ben["id"], "bad", scopes=["agent"], agent_key=key, workspace=ctx.workspace)
    ctx.workspace.agents[0].owner = "ben"
    await build.call("task_mine", error=True)


@pytest.mark.parametrize("profile,principal,tainted", [("tainted", "team", True), ("outsider", "outsider", False)])
async def test_outside_runs_have_no_task_tools(app, profile, principal, tainted):
    names = {tool["name"] for tool in app.state.ctx.registry.list_tools({"principal": principal,
        "profile": profile, "tainted": tainted, "person_id": None, "personal_read": False})}
    assert not any(name.startswith("task_") or name == "lists" for name in names)


async def test_mcp_task_cap_and_scope(app, http):
    build = await agent(app, http, "build")
    async with app.state.ctx.pool.connection() as conn:
        for _ in range(60):
            await conn.execute("INSERT INTO audit (token_id,principal,tool,args_digest,args,decision,reason) VALUES (%s,'person:ada','task_create','none','{}','allow','A call')", (build.token_id,))
    await build.call("task_create", {"list": "launches", "title": "Over the cap"}, error=True)
    assert not (await build.call("task_mine"))["tasks"]


async def test_named_and_unnamed_mine(app, http):
    build = await agent(app, http, "build")
    unnamed = await AgentClient.create(app, http)
    await operator(app, "task_create", list="launches", title="For the person", holder="person:ada")
    row = await operator(app, "task_create", list="launches", title="For the agent", holder="agent:build")
    assert len((await build.call("task_mine"))["tasks"]) == 1
    assert len((await unnamed.call("task_mine"))["tasks"]) == 2
    await build.call("task_write", {"id": row["id"], "append": "A result", "message": "A change."})
    assert not (await build.call("task_mine"))["tasks"][0]["new_for_you"]


async def test_piece_batch_rolls_back_on_bad_holder(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"], profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "task_create", {
            "list": "launches", "title": "The whole", "pieces": [{"title": "Good"}, {"title": "Bad", "holder": "agent:missing"}]})
        assert result["decision"] == "deny"
        assert (await one(conn, "SELECT count(*) AS n FROM tasks"))["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM audit"))["n"] == 1


async def test_agent_request_capture_keeps_agent_identity(app, http, fake_brain):
    build = await agent(app, http, "build")
    fake_brain.script = [{"name": "task_create", "arguments": {"list": "launches", "title": "Capture a stated launch"}}]
    receipt = await build.call("illo_submit", {"message": "Capture the launch that the person stated.", "idempotency_key": "task-capture"})
    await build.settle()
    assert receipt["status"] == "accepted"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM tasks")
        assert row and row["created_by"] == "agent:build" and row["holder"] is None


async def test_unnamed_token_writes_for_its_persons_holders(app, http):
    unnamed = await AgentClient.create(app, http)
    row = await operator(app, "task_create", list="launches", title="For Ada", holder="person:ada")
    changed = await unnamed.call("task_write", {"id": row["id"], "append": "A result", "message": "The result needs review.", "returned": True})
    assert changed["state"] == "review" and changed["done_by"] is None
    assert changed["events"][-2]["actor"].startswith("agent:unnamed:")


async def test_team_channel_task_tools_use_verified_sender(app, replay, fake_brain):
    fake_brain.script = [{"name": "task_create", "arguments": {"list": "launches", "title": "A team task"}}]
    result = await replay(kind="message", text="Make a team task.", payload={"channel_id": "C_ACME_TEAM", "mentioned": True})
    assert "error" not in result
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM tasks")
        assert row and row["created_by"] == "person:ada"


async def test_update_can_clear_due_and_omitted_fields_stay(app):
    row = await operator(app, "task_create", list="launches", title="A task", brief="Keep this brief.", due="2026-12-01T10:00:00Z")
    assert row["due_at"]
    row = await operator(app, "task_update", id=row["id"], due=None)
    assert row["due_at"] is None and row["brief"] == "Keep this brief."
    row = await operator(app, "task_update", id=row["id"], title="A changed title")
    assert row["brief"] == "Keep this brief."


async def test_team_channel_cannot_read_a_private_task(app, replay, fake_brain):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="private", name="Private", owner="ada"))
    row = await operator(app, "task_create", list="private", title="Private", brief="Private brief")
    fake_brain.script = [{"name": "task_get", "arguments": {"id": row["id"]}}]
    await replay(kind="message", text="Read a task.", payload={"channel_id": "C_ACME_TEAM", "mentioned": True})
    result = fake_brain.results[-1]
    assert result["isError"]
    assert "Private brief" not in result["content"][0]["text"]


@pytest.mark.parametrize("profile,expected", [
    ("routine", {"lists", "task_list", "task_get"}),
    ("agent_request", {"lists", "task_list", "task_get", "task_create", "task_post"}),
])
async def test_profile_task_grants(app, profile, expected):
    names = {tool["name"] for tool in app.state.ctx.registry.list_tools({"principal": "person:ada",
        "profile": profile, "tainted": False, "person_id": 1, "personal_read": False})}
    assert {name for name in names if name == "lists" or name.startswith("task_")} == expected


async def test_unnamed_actor_cannot_match_another_persons_named_agent(app, http):
    from illo.workspace import Agent

    unnamed = await AgentClient.create(app, http)
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    key = f"unnamed-{unnamed.token_id}"
    ctx.workspace.agents.append(Agent(key=key, name="Another agent", owner="ben"))
    entry = next(entry for entry in ctx.workspace.lists if entry.key == "launches")
    entry.holders.append("agent:" + key)
    row = await operator(app, "task_create", list="launches", title="Ben's task", holder="agent:" + key)
    result = await unnamed.call("task_write", {"id": row["id"], "append": "No", "message": "No"}, error=True)
    assert "handed to someone else" in result["error"]["message"]
