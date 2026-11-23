"""Team memory saves at once. Buttons and corrections keep the same authority."""

import asyncio
import os
import subprocess
from datetime import UTC, datetime

import pytest

from illo.channels.slack.adapter import SlackAdapter
from illo.db import all_rows, one
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.ledger import people
from illo.memory import buttons, deliver_notices
from illo.notes import gitstore
from illo.workspace import load_workspace
from tests.memory.conftest import git

NOTE = {"title": "Research owner", "text": "Ben owns model research.", "about": "People"}


async def row_for(ctx, result):
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM memory_changes WHERE id=%s", (result["result"]["change_id"],))


async def announce(ctx, result, run, channel="C_ACME_TEAM", thread="100.1"):
    event = InboundEvent(channel="slack", kind="message", external_id="notice",
                         payload={"channel_id": channel, "thread_ts": thread}, reply_to=thread)
    await deliver_notices(ctx, event, run["event_id"])
    return await row_for(ctx, result)


async def click(ctx, row, choice, person="ada", **extra):
    async with ctx.pool.connection() as conn:
        acting = await people.get(conn, person)
    await ctx.slack_actions.dispatch(ctx, acting, {"action_id": f"memory:{row['id']}:{choice}",
        "channel": row["slack_channel"], "message_ts": row["slack_ts"], **extra})


async def correct(ctx, row, text, person="ada", **payload):
    async with ctx.pool.connection() as conn:
        acting = await people.get(conn, person)
    event = InboundEvent(channel="slack", kind="dm" if row["slack_channel"].startswith("D") else "message",
        external_id="correction", sender=acting["slack_id"], text=text,
        payload={"channel_id": row["slack_channel"], "thread_ts": row["thread_ts"], **payload})
    authority = Authority("person:" + person, "person_dm", acting["id"]) if event.kind == "dm" else Authority("team", "team_channel")
    prepared = await buttons.prepare(ctx, event, authority)
    return prepared.text if prepared else None


async def test_channel_note_writes_every_record_and_commit(memory_ctx, call):
    result, run = await call("note_save", NOTE)
    assert result["decision"] == "allow"
    assert result["result"]["line"] == "I will remember: Ben owns model research."
    row = await announce(memory_ctx, result, run)
    assert row["state"] == "saved" and row["before_text"] is None
    path = memory_ctx.workspace.root / "notes" / row["target"]
    assert "Ben owns model research." in path.read_text()
    async with memory_ctx.pool.connection() as conn:
        item = await one(conn, "SELECT * FROM items WHERE source_ref=%s", (result["result"]["ref"],))
        assert item["kind"] == "note" and item["visibility"] == "team"
        assert await one(conn, "SELECT id FROM audit WHERE run_id=%s AND tool='note_save'", (run["id"],))
    assert git(memory_ctx.workspace.root, "log", "-1", "--format=%s") == "memory: note Research owner"
    notice = memory_ctx.slack_client.calls[-1]
    assert notice["text"] == "Saved to memory: Research owner."
    assert notice["channel"] == "C_ACME_TEAM" and notice["thread_ts"] == "100.1"
    assert [b["text"]["text"] for b in notice["blocks"][1]["elements"]] == ["Correct", "Forget"]
    replaced, _ = await call("note_save", {**NOTE, "text": "Ada owns model research.", "replaces": result["result"]["ref"]})
    assert replaced["decision"] == "allow" and replaced["result"]["ref"] == result["result"]["ref"]
    assert "Ada owns model research." in path.read_text()


async def test_notice_follows_final_answer(memory_ctx, fake_brain):
    fake_brain.script = [{"name": "note_save", "arguments": NOTE}]
    fake_brain.final_text = "I will remember: Ben owns model research."
    event = InboundEvent(channel="slack", kind="message", external_id="memory-order",
        sender="U_ACME_ADA", text="Ben owns model research.", reply_to="200.1",
        payload={"channel_id": "C_ACME_TEAM", "mentioned": True, "channel_class": "members"})
    result = await memory_ctx.gateway.receive(event, SlackAdapter(memory_ctx))
    assert result["decision"] == "run" and "error" not in result
    calls = memory_ctx.slack_client.calls
    answer = next(i for i, c in enumerate(calls) if c["method"] == "chat_update" and c["text"].startswith("I will remember:"))
    notice = next(i for i, c in enumerate(calls) if c.get("text") == "Saved to memory: Research owner.")
    assert notice > answer
    assert calls[notice]["thread_ts"] == "200.1"


async def test_owner_creates_list_and_task(memory_ctx, call):
    result, run = await call("list_save", {"key": "social", "name": "Social posts",
        "instructions": "Ben writes the draft. Ada posts it.", "default_holder": "person:ben"})
    assert result["decision"] == "allow", result
    assert next(e for e in memory_ctx.workspace.lists if e.key == "social").holders == []
    lists, _ = await call("lists", {"key": "social"}, run=run)
    assert lists["result"]["lists"][0]["body"] == "Ben writes the draft. Ada posts it."
    task, _ = await call("task_create", {"list": "social", "title": "Prepare a post"}, run=run)
    assert task["decision"] == "allow", task
    assert task["result"]["holder"] == "person:ben"
    assert git(memory_ctx.workspace.root, "log", "-1", "--format=%s") == "memory: list social"


@pytest.mark.parametrize("field,value", [("holders", ["person:missing"]), ("done", ["custom:missing"]),
    ("default_holder", "person:missing"), ("reviewer", "person:missing"), ("done", [])])
async def test_invalid_list_changes_nothing(memory_ctx, call, field, value):
    before = {p.relative_to(memory_ctx.workspace.root): p.read_bytes() for p in memory_ctx.workspace.root.rglob("*.md")}
    head = git(memory_ctx.workspace.root, "rev-parse", "HEAD")
    result, _ = await call("list_save", {"key": "bugs", field: value})
    assert result["decision"] == "deny" and field in result["reason"], result
    assert before == {p.relative_to(memory_ctx.workspace.root): p.read_bytes() for p in memory_ctx.workspace.root.rglob("*.md")}
    assert git(memory_ctx.workspace.root, "rev-parse", "HEAD") == head


async def test_list_permissions_and_patch(memory_ctx, call):
    path = memory_ctx.workspace.root / "lists/bugs.md"
    before = path.read_text()
    result, _ = await call("list_save", {"key": "bugs", "add": "Keep proof links."}, person="ben")
    assert result["decision"] == "allow"
    entry = next(e for e in memory_ctx.workspace.lists if e.key == "bugs")
    assert entry.body.endswith("\n\nKeep proof links.")
    assert entry.holders == ["person:ada", "agent:build"]
    assert before.split("---")[1] in path.read_text() or "reviewer: person:ada" in path.read_text()
    for args in ({"key": "new", "name": "New"}, {"key": "bugs", "holders": ["person:ben"]},
                 {"key": "bugs", "name": "Faults"}, {"key": "bugs", "done": "person"}):
        result, _ = await call("list_save", args, person="ben")
        assert result["decision"] == "deny"
        assert result["reason"] == "Only an owner can change who holds this work. Ask Ada."
    config = memory_ctx.workspace.root / "illo.yaml"
    config.write_text(config.read_text() + "\nmemory:\n  list_changes: everyone\n")
    memory_ctx.workspace = load_workspace(memory_ctx.workspace.root)
    result, _ = await call("list_save", {"key": "new", "name": "New"}, person="ben")
    assert result["decision"] == "allow"


@pytest.mark.parametrize("profile,tainted", [("tainted", True), ("routine", False),
    ("agent_request", False), ("outsider", True), ("person_dm", True)])
async def test_denied_profiles(memory_ctx, call, profile, tainted):
    for name, args in [("note_save", NOTE), ("list_save", {"key": "new", "name": "New"})]:
        result, run = await call(name, args, profile=profile, tainted=tainted)
        assert result["decision"] == "deny"
        assert name not in {t["name"] for t in memory_ctx.registry.list_tools(run)}
    async with memory_ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM memory_changes")


async def test_unknown_actor_and_personal_read(memory_ctx, call):
    result, _ = await call("note_save", NOTE, person=None)
    assert result["decision"] == "deny"
    result, _ = await call("list_save", {"key": "bugs", "add": "New text."}, personal=True)
    assert result["decision"] == "deny" and "personal data" in result["reason"]
    result, _ = await call("note_save", NOTE, profile="person_dm", personal=True)
    assert result["decision"] == "draft", result
    assert result["result"]["state"] == "pending"
    async with memory_ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM note_proposals")
        assert not await one(conn, "SELECT id FROM memory_changes")
        assert not await one(conn, "SELECT id FROM drafts")
    card = next(c for c in memory_ctx.slack_client.calls if c.get("blocks"))
    assert [b["text"]["text"] for b in card["blocks"][1]["elements"]] == ["Keep", "Correct…", "Remove"]


async def test_forget_note_and_list_restore(memory_ctx, call):
    result, run = await call("note_save", NOTE)
    row = await announce(memory_ctx, result, run)
    await click(memory_ctx, row, "forget")
    assert not (memory_ctx.workspace.root / "notes" / row["target"]).exists()
    async with memory_ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM items WHERE source_ref=%s", (result["result"]["ref"],))
        assert await one(conn, "SELECT id FROM audit WHERE tool='memory_forget'")
    assert memory_ctx.slack_client.calls[-1]["text"] == "Forgotten."
    path = memory_ctx.workspace.root / "lists/bugs.md"
    before = path.read_text()
    result, run = await call("list_save", {"key": "bugs", "add": "Keep screenshots."})
    row = await announce(memory_ctx, result, run)
    await click(memory_ctx, row, "forget")
    assert path.read_text() == before
    assert memory_ctx.slack_client.calls[-1]["text"] == "Restored the earlier text."
    assert git(memory_ctx.workspace.root, "log", "-1", "--format=%s") == "memory: forget list bugs"


async def test_forget_new_list_open_tasks(memory_ctx, call):
    result, run = await call("list_save", {"key": "new", "name": "New"})
    row = await announce(memory_ctx, result, run)
    task, _ = await call("task_create", {"list": "new", "title": "An open task"}, run=run)
    assert task["decision"] == "allow"
    await click(memory_ctx, row, "forget")
    assert "open tasks" in memory_ctx.slack_client.calls[-1]["text"]
    assert (memory_ctx.workspace.root / "lists/new.md").exists()
    result, _ = await call("task_cancel", {"id": task["result"]["id"], "reason": "No longer needed."}, run=run)
    assert result["decision"] == "allow"
    await click(memory_ctx, row, "forget")
    assert not (memory_ctx.workspace.root / "lists/new.md").exists()
    assert not any(e.key == "new" for e in memory_ctx.workspace.lists)


@pytest.mark.parametrize("channel", ["C_ACME_TEAM", "D_EXAMPLE"])
@pytest.mark.parametrize("saver,editor", [("ben", "ben"), ("ben", "ada")])
async def test_correct_note_names_the_memory_for_the_model(memory_ctx, call, channel, saver, editor):
    result, run = await call("note_save", NOTE, person=saver)
    row = await announce(memory_ctx, result, run, channel=channel)
    await click(memory_ctx, row, "correct", person=editor)
    assert memory_ctx.slack_client.calls[-1]["text"] == buttons.CORRECT
    text = await correct(memory_ctx, row, "No, Ada owns it.", person=editor)
    # Code names the memory. The person's words stay whole, after the code line.
    assert f"`note:{row['target']}`" in text and "`note_save`" in text
    assert text.endswith("]\nNo, Ada owns it.")
    # The file changes only when the model saves. The correction waits no more.
    assert "Ben owns model research." in (memory_ctx.workspace.root / "notes" / row["target"]).read_text()
    assert (await row_for(memory_ctx, result))["correction_ts"] is None
    assert await correct(memory_ctx, row, "A later message.", person=editor) is None
    replaced, _ = await call("note_save", {**NOTE, "text": "Ada owns model research.",
                                           "replaces": result["result"]["ref"]}, person=editor)
    assert replaced["decision"] == "allow"
    assert "Ada owns model research." in (memory_ctx.workspace.root / "notes" / row["target"]).read_text()


async def test_correct_list_keeps_the_rest_and_refuses_a_bot(memory_ctx, call):
    before = next(e for e in memory_ctx.workspace.lists if e.key == "bugs").body
    result, run = await call("list_save", {"key": "bugs", "add": "Keep the proof."}, person="ben")
    row = await announce(memory_ctx, result, run)
    await click(memory_ctx, row, "forget", person="chloe")
    assert memory_ctx.slack_client.calls[-1]["text"] == "Only Ben or an owner can change this."
    await click(memory_ctx, row, "correct", person="ben")
    assert await correct(memory_ctx, row, "Bot.", person="ben", bot=True) is None
    assert await correct(memory_ctx, row, "Outside.", person="ben", contains_outside_text=True) is None
    text = await correct(memory_ctx, row, "Ben keeps proof links.", person="ben")
    assert "the list `bugs`" in text and "`list_save`" in text
    # A reply never replaces the list text by itself.
    entry = next(e for e in memory_ctx.workspace.lists if e.key == "bugs")
    assert entry.body.startswith(before.strip()) and entry.body.endswith("Keep the proof.")


async def test_note_forget_by_words(memory_ctx, call):
    result, _ = await call("note_save", NOTE, person="ben")
    ref = result["result"]["ref"]
    denied, _ = await call("note_forget", {"ref": ref}, person="chloe")
    assert denied["decision"] == "deny" and "author" in denied["reason"]
    denied, _ = await call("note_forget", {"ref": ref}, person="ben", personal=True)
    assert denied["decision"] == "deny"
    denied, _ = await call("note_forget", {"ref": ref}, person="ben", profile="agent_request")
    assert denied["decision"] == "deny"
    done, run = await call("note_forget", {"ref": ref}, person="ada")
    assert done["decision"] == "allow" and done["result"]["forgotten"]
    assert not (memory_ctx.workspace.root / "notes" / ref[5:]).exists()
    async with memory_ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM items WHERE source_ref=%s", (ref,))
        row = await one(conn, "SELECT * FROM memory_changes WHERE state='forgotten'")
        assert "Ben owns model research." in row["before_text"]
    # A forgotten note gets no notice with buttons.
    await announce(memory_ctx, done, run)
    assert (await row_for(memory_ctx, done))["slack_ts"] is None
    assert git(memory_ctx.workspace.root, "log", "-1", "--format=%s") == "memory: forget note Research owner"


async def test_button_expiry_and_later_edits(memory_ctx, call):
    result, run = await call("note_save", NOTE)
    row = await announce(memory_ctx, result, run)
    await click(memory_ctx, row, "forget", message_ts="wrong")
    assert (memory_ctx.workspace.root / "notes" / row["target"]).exists()
    async with memory_ctx.pool.connection() as conn:
        await conn.execute("UPDATE memory_changes SET created_at=now()-interval '8 days' WHERE id=%s", (row["id"],))
    await click(memory_ctx, row, "forget")
    assert "expired after 7 days" in memory_ctx.slack_client.calls[-1]["text"]
    result, run = await call("note_save", {**NOTE, "title": "Another fact"})
    row = await announce(memory_ctx, result, run)
    path = memory_ctx.workspace.root / "notes" / row["target"]
    path.write_text(path.read_text() + "A hand edit.\n")
    await click(memory_ctx, row, "forget")
    assert "changed after this save" in memory_ctx.slack_client.calls[-1]["text"]
    assert "A hand edit." in path.read_text()


async def test_hour_limit(memory_ctx, call):
    result, run = await call("note_save", NOTE)
    async with memory_ctx.pool.connection() as conn:
        await conn.execute("""INSERT INTO memory_changes(kind,target,title,after_text,person_id)
            SELECT 'note','unused.md','A fact','Text',person_id FROM memory_changes
            CROSS JOIN generate_series(1,19) WHERE id=%s""", (result["result"]["change_id"],))
    for name, args in [("note_save", NOTE), ("list_save", {"key": "new", "name": "New"})]:
        result, _ = await call(name, args)
        assert result["decision"] == "deny" and "20 memory saves" in result["reason"]
    result, _ = await call("note_save", NOTE, person="ben")
    assert result["decision"] == "allow"
    async with memory_ctx.pool.connection() as conn:
        await conn.execute("UPDATE memory_changes SET created_at=%s", (datetime(2020, 1, 1, tzinfo=UTC),))
    result, _ = await call("note_save", NOTE)
    assert result["decision"] == "allow"


async def test_concurrent_saves_share_the_hour_limit(memory_ctx, call):
    result, _ = await call("note_save", NOTE)
    async with memory_ctx.pool.connection() as conn:
        await conn.execute("""INSERT INTO memory_changes(kind,target,title,after_text,person_id)
            SELECT 'note','unused.md','A fact','Text',person_id FROM memory_changes
            CROSS JOIN generate_series(1,18) WHERE id=%s""", (result["result"]["change_id"],))
    first, second = await asyncio.gather(call("note_save", {**NOTE, "title": "First fact"}),
                                         call("note_save", {**NOTE, "title": "Second fact"}))
    assert sorted([first[0]["decision"], second[0]["decision"]]) == ["allow", "deny"]


async def test_whole_workspace_and_list_body_limits(memory_ctx, call):
    path = memory_ctx.workspace.root / "lists/bugs.md"
    before = path.read_text()
    result, _ = await call("list_save", {"key": "bugs", "add": "x" * 20000})
    assert result["decision"] == "deny" and "instructions" in result["reason"]
    assert path.read_text() == before
    broken = memory_ctx.workspace.root / "people.yaml"
    broken.write_text(broken.read_text().replace("role: owner", "role: unknown"))
    result, _ = await call("list_save", {"key": "bugs", "add": "Keep the proof."})
    assert result["decision"] == "deny" and "role" in result["reason"]
    assert path.read_text() == before


async def test_description_patch_name_required_and_list_owner(memory_ctx, call):
    result, _ = await call("list_save", {"key": "missing"})
    assert result["decision"] == "deny" and "name" in result["reason"]
    result, _ = await call("list_save", {"key": "bugs", "description": "Repair faults."}, person="ben")
    assert result["decision"] == "allow"
    entry = next(e for e in memory_ctx.workspace.lists if e.key == "bugs")
    assert entry.description == "Repair faults." and entry.default_holder == "agent:build"
    path = memory_ctx.workspace.root / "lists/private.md"
    path.write_text("---\nname: Private\nowner: ben\n---\nKeep proof.\n")
    memory_ctx.workspace = load_workspace(memory_ctx.workspace.root)
    result, _ = await call("list_save", {"key": "private", "holders": ["person:ben"]}, person="ben", profile="person_dm")
    assert result["decision"] == "allow"


async def test_search_and_note_grants(memory_ctx, call):
    result, _ = await call("note_save", NOTE)
    assert result["decision"] == "allow"
    for tool in ("notes_search", "search_all"):
        result, _ = await call(tool, {"query": "research"})
        assert result["decision"] == "allow" and "Research owner" in result["result"]
    result, run = await call("notes_propose", NOTE, profile="person_dm")
    assert result["decision"] == "deny"
    names = {t["name"] for t in memory_ctx.registry.list_tools(run)}
    assert {"note_save", "list_save"} <= names and "notes_propose" not in names


async def test_forget_button_after_a_correction_request(memory_ctx, call):
    result, run = await call("note_save", NOTE, person="ben")
    row = await announce(memory_ctx, result, run)
    await click(memory_ctx, row, "correct", person="ben")
    assert (await row_for(memory_ctx, result))["correction_ts"]
    await click(memory_ctx, row, "forget", person="ada")
    assert not (memory_ctx.workspace.root / "notes" / row["target"]).exists()
    assert await correct(memory_ctx, row, "Too late.", person="ben") is None


async def test_prompt_question_then_answer_creates_work(memory_ctx, fake_brain):
    adapter = SlackAdapter(memory_ctx)
    fake_brain.script = []
    fake_brain.final_text = "Who writes a social post, where does the draft go, and what counts as Done?"
    first = InboundEvent(channel="slack", kind="message", sender="U_ACME_ADA", external_id="ask-memory",
        text="Prepare a social post.", reply_to="300.1", payload={"channel_id": "C_ACME_TEAM", "mentioned": True})
    result = await memory_ctx.gateway.receive(first, adapter)
    assert result["text"].endswith("?")
    async with memory_ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM tasks")
    fake_brain.script = [
        {"name": "list_save", "arguments": {"key": "social", "name": "Social posts", "default_holder": "person:ben",
            "instructions": "Ben writes drafts in the social folder. Ada confirms the draft."}},
        {"name": "task_create", "arguments": {"list": "social", "title": "Prepare a social post"}},
    ]
    fake_brain.final_text = "I will remember: Ben writes social drafts, and Ada confirms them."
    result = await memory_ctx.gateway.receive(first.model_copy(update={"external_id": "answer-memory",
        "text": "Ben writes drafts in the social folder. Ada confirms the draft."}), adapter)
    assert "error" not in result
    async with memory_ctx.pool.connection() as conn:
        task = await one(conn, "SELECT * FROM tasks")
        assert task["list_key"] == "social" and task["holder"] == "person:ben"
        assert await one(conn, "SELECT id FROM memory_changes WHERE kind='list'")
async def test_no_git_keeps_file_and_sensor(memory_ctx, call, monkeypatch):
    monkeypatch.setenv("PATH", "/no-git-here")
    result, _ = await call("note_save", NOTE)
    assert result["decision"] == "allow" and not result["result"]["committed"]
    assert (memory_ctx.workspace.root / "notes" / result["result"]["ref"][5:]).exists()
    async with memory_ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM sensors WHERE key='workspace'"))["last_error_at"]


@pytest.mark.parametrize("home", [None, "/no-home-for-this-uid"])
async def test_git_no_home_safe_directory_and_child_environment(memory_ctx, monkeypatch, home):
    original = asyncio.create_subprocess_exec
    commands = []
    async def checked(*args, **kwargs):
        commands.append(args)
        assert args[1:3] == ("-c", f"safe.directory={memory_ctx.workspace.root}")
        assert os.path.isdir(kwargs["env"]["HOME"])
        assert "GIT_DIR" not in kwargs["env"] and "GIT_WORK_TREE" not in kwargs["env"]
        kwargs["env"]["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
        return await original(*args, **kwargs)
    monkeypatch.delenv("HOME", raising=False)
    if home:
        monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("GIT_DIR", "/wrong")
    monkeypatch.setenv("GIT_WORK_TREE", "/wrong")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked)
    untrusted = subprocess.run(["git", "-C", str(memory_ctx.workspace.root), "status", "--porcelain"],
        env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
             "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}, capture_output=True)
    assert untrusted.returncode != 0 and b"dubious ownership" in untrusted.stderr
    assert await gitstore._git(memory_ctx.workspace.root, "status", "--porcelain")
    assert commands
