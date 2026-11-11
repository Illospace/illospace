"""Task changes keep authority, proof, and the next move in one transaction."""

import pytest

from illo.db import all_rows, one
from illo.ledger import tasks
from illo.tasks import checks
from illo.workspace import TaskList

ADA = tasks.Viewer("ada", "person:ada", human=True)
BEN = tasks.Viewer("ben", "person:ben", human=True)
BUILD = tasks.Viewer("ada", "agent:build")
STUDIO = tasks.Viewer("ben", "agent:studio")


@pytest.fixture
def ws(workspace):
    workspace = workspace.model_copy(deep=True)
    workspace.lists.append(TaskList(key="tasks", name="Tasks"))
    return workspace


async def make(conn, ws, **values):
    return await tasks.create(conn, workspace=ws, viewer=ADA, list_key="tasks", title="A task", **values)


async def test_waits_for_result_versions_and_record(conn, ws):
    row = await make(conn, ws)
    assert row["waits_for"] is None
    row = await tasks.hand(conn, row["id"], workspace=ws, viewer=ADA, holder="agent:build")
    assert row["state"] == "todo" and row["waits_for"] == "agent:build"
    row = await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD,
                                  result="First result", result_version=0, message="The first check passes.")
    assert row["state"] == "working" and row["result_version"] == 1
    assert row["brief"] == ""
    with pytest.raises(tasks.TaskError, match="result changed"):
        await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD,
                                 result="Stale", result_version=0, message="A change.")
    row = await tasks.post(conn, row["id"], workspace=ws, viewer=BUILD, text="Which format?", question=True, to="person:ben")
    assert row["waits_for"] == "person:ben"
    row = await tasks.post(conn, row["id"], workspace=ws, viewer=ADA, text="A comment.")
    assert row["waits_for"] == "person:ben"
    row = await tasks.post(conn, row["id"], workspace=ws, viewer=BEN, text="Use a document.")
    assert row["waits_for"] == "agent:build"
    row = await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD,
                                  append="Final result", message="Please confirm the result.", returned=True)
    assert row["state"] == "review" and row["waits_for"] == "person:ada"
    assert row["result"] == "First result\nFinal result"
    row = await tasks.done(conn, row["id"], workspace=ws, viewer=ADA)
    assert row["state"] == "done" and row["waits_for"] is None and row["done_by"] == "person:ada"
    row = await tasks.reopen(conn, row["id"], workspace=ws, viewer=ADA, reason="Check another format.")
    assert row["state"] == "todo" and row["done_at"] is None
    row = await tasks.cancel(conn, row["id"], workspace=ws, viewer=ADA, reason="The work is no longer needed.")
    assert row["waits_for"] is None
    record = await tasks.events(conn, row["id"], viewer=ADA, limit=100)
    assert {"created", "handed", "result", "message", "question", "returned", "done", "reopened", "cancelled"} <= {e["kind"] for e in record}
    assert len([e for e in record if e["kind"] == "result"]) == 2


async def test_visibility_filters_every_read(conn, ws, ada):
    ws.lists.append(TaskList(key="private", name="Private", owner="ada"))
    row = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="private", title="Private words", brief="Secret")
    public = await make(conn, ws)
    for viewer in (ADA, BUILD):
        assert await tasks.get(conn, row["id"], viewer=viewer)
    for viewer in (BEN, STUDIO, tasks.Viewer(None, "code", team=True)):
        assert await tasks.get(conn, row["id"], viewer=viewer) is None
        assert len(await tasks.search(conn, viewer=viewer)) == 1
        assert await tasks.search(conn, viewer=viewer, query="Secret") == []
        with pytest.raises(tasks.TaskError):
            await tasks.events(conn, row["id"], viewer=viewer)
    for viewer in (tasks.Viewer(None, "outsider"), tasks.Viewer("ada", "person:ada", tainted=True)):
        with pytest.raises(tasks.TaskError):
            await tasks.get(conn, public["id"], viewer=viewer)
        with pytest.raises(tasks.TaskError):
            await tasks.search(conn, viewer=viewer)
    await conn.execute("UPDATE people SET active=false WHERE id=%s", (ada["id"],))
    with pytest.raises(tasks.TaskError):
        await tasks.get(conn, public["id"], viewer=BUILD)


async def test_holder_and_brief_walls(conn, ws):
    row = await make(conn, ws, holder="agent:build", brief="The person's brief.")
    for viewer in (STUDIO, BEN):
        with pytest.raises(tasks.TaskError, match="handed to someone else"):
            await tasks.write_result(conn, row["id"], workspace=ws, viewer=viewer,
                                     append="No", message="No")
    with pytest.raises(tasks.TaskError, match="creator"):
        await tasks.update(conn, row["id"], workspace=ws, viewer=BUILD, brief="Replace the brief")
    with pytest.raises(tasks.TaskError, match="person must"):
        await tasks.done(conn, row["id"], workspace=ws, viewer=BUILD)
    with pytest.raises(tasks.TaskError, match="message"):
        await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD, append="No", message=" ")
    await tasks.hand(conn, row["id"], workspace=ws, viewer=ADA, holder="person:ben")
    with pytest.raises(tasks.TaskError, match="handed to someone else"):
        await tasks.post(conn, row["id"], workspace=ws, viewer=BUILD, text="No", question=True)
    row = await tasks.get(conn, row["id"], viewer=ADA)
    assert row["result_version"] == 0 and row["brief"] == "The person's brief."


async def test_check_rules_and_force(conn, ws):
    row = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="bugs", title="Repair a fault")
    row = await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD,
                                  append="The repair passes.", message="Please check the evidence.", returned=True)
    assert row["state"] == "review"
    with pytest.raises(tasks.TaskError, match="checks do not pass"):
        await tasks.done(conn, row["id"], workspace=ws, viewer=ADA)
    row = await tasks.add_link(conn, row["id"], workspace=ws, viewer=ADA,
                               links=[{"url": "https://example.com/1", "kind": "ticket", "title": "Proof"}])
    assert row["state"] == "done" and row["done_by"] == "code"
    await tasks.reopen(conn, row["id"], workspace=ws, viewer=ADA, reason="Check again.")
    await conn.execute("UPDATE tasks SET links='[]' WHERE id=%s", (row["id"],))
    row = await tasks.done(conn, row["id"], workspace=ws, viewer=ADA, force=True, reason="The person confirms another proof.")
    assert row["done_by"] == "person:ada"
    event = (await tasks.events(conn, row["id"], viewer=ADA))[-1]
    assert event["data"]["force"] is True


async def test_fake_registered_check_and_parent_review(conn, ws, monkeypatch):
    async def fake(ctx, connection, task):
        assert connection is conn
        return True, "The fake proof passes."
    monkeypatch.setattr(checks, "_checks", {})
    checks.register("fake:proof", fake)
    assert checks.known("fake:proof")
    ws.lists.append(TaskList(key="checked", name="Checked", done=["fake:proof", "pieces"]))
    parent = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="checked", title="The whole")
    child = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="checked", title="A piece", parent_id=parent["id"])
    await tasks.done(conn, child["id"], workspace=ws, viewer=ADA)
    row = await tasks.get(conn, parent["id"], viewer=ADA)
    assert row["state"] == "review" and row["waits_for"] == "person:ada"
    await tasks.done(conn, row["id"], workspace=ws, viewer=ADA)
    await tasks.reopen(conn, child["id"], workspace=ws, viewer=ADA, reason="A correction.")
    row = await tasks.get(conn, parent["id"], viewer=ADA)
    assert row["state"] == "todo" and row["done_at"] is None


async def test_create_failure_rolls_back_event(conn, ws):
    with pytest.raises(tasks.TaskError):
        await tasks.create(conn, workspace=ws, viewer=ADA, list_key="bugs", title="No holder", holder="person:ben")
    assert (await one(conn, "SELECT count(*) AS n FROM tasks"))["n"] == 0
    assert await all_rows(conn, "SELECT * FROM task_events") == []


async def test_check_failure_does_not_lose_returned_work(conn, ws, monkeypatch):
    async def unavailable(ctx, connection, task):
        raise RuntimeError("A private provider error")
    monkeypatch.setattr(checks, "_checks", {"fake:unavailable": unavailable})
    ws.lists.append(TaskList(key="checked", name="Checked", done=["fake:unavailable"]))
    row = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="checked", title="A task", holder="agent:build")
    row = await tasks.write_result(conn, row["id"], workspace=ws, viewer=BUILD, append="The work", message="Please review it.", returned=True)
    assert row["state"] == "review" and row["result"] == "The work"
    proof = await tasks.proof(conn, ws, row)
    assert not proof[0]["ok"] and "private" not in proof[0]["text"]


async def test_changed_link_checks_active_work(conn, ws):
    row = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="bugs", title="A fault")
    row = await tasks.add_link(conn, row["id"], workspace=ws, viewer=ADA,
                               links=[{"url": "https://example.com/proof", "kind": "ticket"}])
    assert row["state"] == "done" and row["done_by"] == "code"


async def test_search_filters_and_private_holder_validation(conn, ws):
    row = await make(conn, ws, holder="person:ben", brief="An export format")
    for filters in ({"holder": "person:ben"}, {"waits_for": "person:ben"}, {"state": "todo"},
                    {"list_key": "tasks"}, {"query": "export"}):
        assert (await tasks.search(conn, viewer=ADA, **filters))[0]["id"] == row["id"]
    assert not await tasks.search(conn, viewer=ADA, holder="person:ada")
    await tasks.update(conn, row["id"], workspace=ws, viewer=ADA, title="A new title", brief="A new brief")
    assert (await tasks.get(conn, row["id"], viewer=ADA))["brief"] == "A new brief"
    ws.lists.append(TaskList(key="private", name="Private", owner="ada"))
    with pytest.raises(tasks.TaskError, match="holder of its owner"):
        await tasks.create(conn, workspace=ws, viewer=ADA, list_key="private", title="Private", holder="person:ben")
    await tasks.done(conn, row["id"], workspace=ws, viewer=ADA)
    assert not await tasks.search(conn, viewer=ADA, open=True)


async def test_parent_with_missing_proof_waits_for_a_person(conn, ws):
    ws.lists.append(TaskList(key="proof", name="Proof", done=["link:video"]))
    parent = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="proof", title="A launch")
    piece = await tasks.create(conn, workspace=ws, viewer=ADA, list_key="proof", title="A piece", parent_id=parent["id"])
    await tasks.done(conn, piece["id"], workspace=ws, viewer=ADA, force=True, reason="The person checks the piece.")
    parent = await tasks.get(conn, parent["id"], viewer=ADA)
    assert parent["state"] == "review" and parent["waits_for"] == "person:ada"
    proof = await tasks.proof(conn, ws, parent)
    assert not proof[0]["ok"] and proof[1]["ok"]
    with pytest.raises(tasks.TaskError):
        await tasks.done(conn, parent["id"], workspace=ws, viewer=ADA)
    parent = await tasks.add_link(conn, parent["id"], workspace=ws, viewer=ADA, links=[{"url": "https://example.com/video", "kind": "video"}])
    assert parent["state"] == "review"
    await tasks.done(conn, parent["id"], workspace=ws, viewer=ADA)
