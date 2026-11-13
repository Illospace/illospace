"""The task connector owns all GitHub facts, actions, and proof checks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from illo.channels.mcp import lanes as lane_tools
from illo.connectors import github_tasks as connector
from illo.db import all_rows, one
from illo.ledger import people, tasks
from illo.sources.github import poll
from illo.tasks import release, wake
from tests.github.test_triage import alert, trust

ADA = tasks.Viewer("ada", "person:ada", human=True)
BUILD = tasks.Viewer("ada", "agent:build")
REPO = "acme-example/engine"


@pytest.fixture
async def ctx(app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    return ctx


async def make(ctx, **values):
    async with ctx.pool.connection() as conn:
        return await tasks.create(conn, workspace=ctx.workspace, viewer=ADA,
            list_key="bugs", title="Repair the export", brief="The export fails.", **values)


async def get(ctx, task_id):
    async with ctx.pool.connection() as conn:
        return await tasks.get(conn, task_id, viewer=ADA)


async def events(ctx, task_id):
    async with ctx.pool.connection() as conn:
        return await tasks.events(conn, task_id, viewer=ADA, limit=100)


async def test_first_wake_header_assignment_link_and_idempotency(ctx, github_client):
    row = await make(ctx)
    await wake.tick(ctx)
    current = await get(ctx, row["id"])
    assert len(github_client.issues) == 1
    issue = github_client.issues[0]
    assert issue["body"].count(ctx.workspace.config.github.issue_header) == 1
    assert "The export fails." in issue["body"] and f"Illo task: {row['id']}" in issue["body"]
    assert issue["assignees"] == [{"login": "acme-ada-example"}]
    assert current["links"][0]["url"] == issue["html_url"]
    async with ctx.pool.connection() as conn:
        await tasks.wake(conn, row["id"], workspace=ctx.workspace, viewer=ADA)
    await wake.tick(ctx)
    await wake.tick(ctx)
    assert len(github_client.issues) == 1
    async with ctx.pool.connection() as conn:
        writes = await all_rows(conn, "SELECT * FROM audit WHERE tool LIKE 'task_github_%%'")
        assert writes and all(w["target"] == f"task {row['id']}" for w in writes)
        assert not await all_rows(conn, "SELECT * FROM drafts")


async def test_marker_recovers_issue_after_missing_link(ctx, github_client):
    row = await make(ctx)
    github_client.issues.append({"number": 9, "title": "Existing", "state": "open", "body": f"Illo task: {row['id']}",
        "html_url": f"https://github.com/{REPO}/issues/9", "assignees": [], "comments_data": []})
    await wake.tick(ctx)
    assert len(github_client.issues) == 1
    assert (await get(ctx, row["id"]))["links"][0]["url"].endswith("/9")


async def test_existing_ticket_is_assigned_without_a_new_issue(ctx, github_client):
    issue = await github_client.create_issue(REPO, "Existing", "A body")
    row = await make(ctx, links=[{"kind": "ticket", "url": issue["html_url"]}])
    await wake.tick(ctx)
    assert len(github_client.issues) == 1
    assert any(c["method"] == "assign_issue" for c in github_client.calls)
    assert (await get(ctx, row["id"]))["state"] == "todo"


async def test_repo_choice_and_missing_repo_reason(ctx, github_client):
    config = ctx.workspace.agents[0].wake
    config.repos.append("acme/other")
    row = await make(ctx, links=[{"kind": "repo", "url": "https://github.com/acme/other"}])
    await wake.tick(ctx)
    assert github_client.issues[0]["repo"] == "acme/other"
    config.default_repo = None
    with pytest.raises(tasks.TaskError, match="need a repository"):
        await make(ctx)
    assert (await get(ctx, row["id"]))["links"][-1]["kind"] == "ticket"


async def test_restart_message_and_reopen(ctx, github_client):
    row = await make(ctx)
    await wake.tick(ctx)
    github_client.issues[0]["labels"] = ["agent:working"]
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which format?", question=True)
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=ADA, text="Use CSV.\nKeep the date.")
    await wake.tick(ctx)
    comment = [c for c in github_client.calls if c["method"] == "comment_issue"][-1]["body"]
    assert "> Use CSV.\n> Keep the date." in comment and f"From the Illo task {row['id']}." in comment
    async with ctx.pool.connection() as conn:
        await tasks.wake(conn, row["id"], workspace=ctx.workspace, viewer=ADA)
    await wake.tick(ctx)
    methods = [c["method"] for c in github_client.calls]
    assert "remove_label" in methods and "unassign_issue" in methods
    assert not github_client.issues[0]["labels"]
    assert any("Continue from the present state" in c.get("body", "") for c in github_client.calls)
    github_client.issues[0]["state"] = "closed"
    async with ctx.pool.connection() as conn:
        await tasks.reopen(conn, row["id"], workspace=ctx.workspace, viewer=ADA, reason="Check again.")
    await wake.tick(ctx)
    assert github_client.issues[0]["state"] == "open"


async def test_write_limit_fails_with_reason_and_resumes(ctx, github_client):
    ctx.workspace.config.tasks.github_writes_per_hour = 1
    row = await make(ctx)
    await wake.tick(ctx)
    assert (await events(ctx, row["id"]))[-1]["text"] == "The task limit for GitHub writes per hour is reached."
    assert len(github_client.issues) == 1
    ctx.workspace.config.tasks.github_writes_per_hour = 30
    await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=20))
    assert len(github_client.issues) == 1
    assert (await events(ctx, row["id"]))[-1]["kind"] == "woken"


async def test_worker_comment_liveness_question_and_answer(ctx, github_client):
    row = await make(ctx)
    await wake.tick(ctx)
    issue = github_client.issues[0]
    issue["comments_data"] = [{"id": 100, "body": "Which date should I use?", "html_url": issue["html_url"]+"#comment-100",
        "user": {"login": "acme-ada-example"}}]
    issue["labels"] = ["agent:needs-input"]
    await poll(ctx)
    current = await get(ctx, row["id"])
    assert current["alive_at"] and current["waits_for"] == "person:ada"
    record = await events(ctx, row["id"])
    assert record[-1]["kind"] == "question" and record[-1]["text"] == "Which date should I use?"
    assert record[-1]["data"]["url"].endswith("#comment-100")
    progress = [e for e in record if e["actor"] == "agent:build" and e["kind"] == "message"]
    assert len(progress) == 1
    issue["labels"] = []
    await poll(ctx)
    assert (await get(ctx, row["id"]))["waits_for"] == "agent:build"
    await poll(ctx)
    assert len([e for e in await events(ctx, row["id"]) if e["actor"] == "agent:build" and e["kind"] == "message"]) == 1


@pytest.mark.parametrize("reason,state", [("completed", "done"), ("not_planned", "todo")])
async def test_closed_signal_and_done_rule(ctx, github_client, reason, state):
    row = await make(ctx)
    await wake.tick(ctx)
    github_client.issues[0].update(state="closed", state_reason=reason)
    # The first poll also finds a linked issue that is already closed.
    await poll(ctx)
    current = await get(ctx, row["id"])
    assert current["state"] == state
    if reason == "completed":
        assert current["done_by"] == "code"
        assert any(e["kind"] == "returned" for e in await events(ctx, row["id"]))
    else:
        assert current["waits_for"] == "person:ada"
        assert (await events(ctx, row["id"]))[-1]["text"] == "The ticket was closed as not planned."


async def test_checks_and_cache_and_merged_change_link(ctx, github_client):
    row = await make(ctx)
    async with ctx.pool.connection() as conn:
        assert not (await connector.ticket_closed(ctx, conn, row))[0]
        assert not (await connector.change_merged(ctx, conn, row))[0]
    await wake.tick(ctx)
    github_client.pulls.append({"number": 2, "title": "A repair", "body": "Closes #1", "state": "closed", "merged": True,
        "pull_request": {}, "html_url": f"https://github.com/{REPO}/pull/2"})
    # A first-load poll reads open items only; the later poll supplies the merged change.
    await poll(ctx)
    await poll(ctx)
    current = await get(ctx, row["id"])
    assert any(link["kind"] == "change" for link in current["links"])
    async with ctx.pool.connection() as conn:
        assert (await connector.change_merged(ctx, conn, current))[0]
        before = len([c for c in github_client.calls if c["method"] == "get_pull"])
        assert (await connector.change_merged(ctx, conn, current))[0]
        assert len([c for c in github_client.calls if c["method"] == "get_pull"]) == before


async def test_trusted_alert_one_task_without_second_wake(app, ctx, fake_brain, github_client):
    trust(app)
    result = await alert(app, fake_brain)
    current = await get(ctx, result["task_id"])
    assert current["created_by"] == "code" and current["holder"] == "agent:build"
    assert current["brief"] == "Filed from slack in #alerts."
    await wake.tick(ctx)
    assert len([c for c in github_client.calls if c["method"] == "assign_issue"]) == 1
    async with ctx.pool.connection() as conn:
        assert await connector.capture(ctx, conn, REPO, result["issue"], "acme-ada-example") == current["id"]
        assert (await one(conn, "SELECT count(*) AS n FROM tasks"))["n"] == 1
        with pytest.raises(tasks.TaskError, match="already has a task"):
            await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="bugs", title="Duplicate",
                links=[{"kind": "ticket", "url": result["issue"]["html_url"]}])


async def test_owner_card_assignment_sets_holder(app, ctx, fake_brain, github_client):
    from illo.asks import drafts
    from illo.ledger import kv

    result = await alert(app, fake_brain)
    current = await get(ctx, result["task_id"])
    assert current["holder"] is None and current["waits_for"] == "person:ada"
    async with ctx.pool.connection() as conn:
        card = await one(conn, "SELECT * FROM drafts WHERE kind='github_assign_issue'")
        ada = await people.get(conn, "ada")
    await drafts.handle(ctx, ada, {"action_id": f"draft:{card['id']}:approve", "channel": card["slack_channel"], "message_ts": card["slack_ts"]})
    assert (await get(ctx, current["id"]))["holder"] == "agent:build"
    await wake.tick(ctx)
    assert len([c for c in github_client.calls if c["method"] == "assign_issue"]) == 1
    async with ctx.pool.connection() as conn:
        assert not await kv.get(conn, "unused")


@pytest.mark.parametrize("plain_url", [False, True])
async def test_working_lane_alive_stopped_lane_releases_now(ctx, github_client, plain_url):
    row = await make(ctx)
    await wake.tick(ctx)
    issue = github_client.issues[0]
    issue["labels"] = ["agent:working"]
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        token = {"person_key": "ada", "person_id": ada["id"], "id": 1, "label": "build"}
        caller = SimpleNamespace(app=ctx, conn=conn, token=token)
        values = {"source": "codex", "external_id": "run-1", "state": "working", "links": [issue["html_url"] if plain_url else {"url": issue["html_url"]}]}
        await lane_tools.report_one(caller, values)
    assert (await get(ctx, row["id"]))["alive_at"]
    async with ctx.pool.connection() as conn:
        caller.conn = conn
        await lane_tools.report_one(caller, {**values, "state": "unknown"})
    assert (await get(ctx, row["id"]))["stopped_at"]
    await release.tick(ctx)
    assert not [e for e in await events(ctx, row["id"]) if e["kind"] == "released"]  # the grace period
    await release.tick(ctx, datetime.now(UTC) + timedelta(minutes=31))
    await wake.tick(ctx)
    assert any(c["method"] == "remove_label" for c in github_client.calls)
    assert any(c["method"] == "unassign_issue" for c in github_client.calls)
    assert len([e for e in await events(ctx, row["id"]) if e["kind"] == "released"]) == 1
    async with ctx.pool.connection() as conn:
        caller.conn = conn
        await lane_tools.report_one(caller, {**values, "state": "unknown"})
    assert (await get(ctx, row["id"]))["stopped_at"] is None


async def test_explicit_task_lane_and_other_owner_cannot_change_it(ctx):
    row = await make(ctx)
    async with ctx.pool.connection() as conn:
        lane = {"id": 1, "source": "codex", "state": "working", "links": []}
        await connector.lane_signal(ctx, conn, lane, task_id=row["id"], token={"person_key": "ben"})
        assert not (await get_without_pool(conn, row["id"]))["alive_at"]
        await connector.lane_signal(ctx, conn, lane, task_id=row["id"], token={"person_key": "ada"})
        assert (await get_without_pool(conn, row["id"]))["alive_at"]


async def get_without_pool(conn, task_id):
    return await tasks.get(conn, task_id, viewer=ADA)


async def test_own_app_comments_are_not_activity_but_other_bots_are(ctx, github_client):
    row = await make(ctx)
    await wake.tick(ctx)
    issue = github_client.issues[0]
    ctx.config.github_app_id = "123"
    issue["comments_data"] = [{"id": 100, "body": "An own comment", "user": {"login": "example-app[bot]"},
                                "performed_via_github_app": {"id": 123}}]
    await poll(ctx)
    assert not (await get(ctx, row["id"]))["alive_at"]
    issue["comments_data"].append({"id": 101, "body": "A worker report <!-- worker -->", "user": {"login": "builder[bot]"},
                                   "performed_via_github_app": None})
    await poll(ctx)
    assert (await get(ctx, row["id"]))["alive_at"]
    assert any(e["kind"] == "message" and e["actor"] == "agent:build" for e in await events(ctx, row["id"]))


async def test_closed_review_signal_does_not_repeat_record(ctx, github_client):
    row = await make(ctx)
    next(entry for entry in ctx.workspace.lists if entry.key == "bugs").done = "person"
    await wake.tick(ctx)
    github_client.issues[0].update(state="closed", state_reason="completed")
    await poll(ctx)
    count = len(await events(ctx, row["id"]))
    await poll(ctx)
    assert len(await events(ctx, row["id"])) == count


async def test_partial_restart_retry_keeps_completed_steps(ctx, github_client, monkeypatch):
    row = await make(ctx)
    await wake.tick(ctx)
    github_client.issues[0]["labels"] = ["agent:working"]
    real = github_client.assign_issue
    failed = False
    async def uncertain(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("An uncertain assignment.")
        return await real(*args, **kwargs)
    monkeypatch.setattr(github_client, "assign_issue", uncertain)
    async with ctx.pool.connection() as conn:
        await tasks.wake(conn, row["id"], workspace=ctx.workspace, viewer=ADA)
    await wake.tick(ctx)
    await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=20))
    assert len([c for c in github_client.calls if c["method"] == "unassign_issue"]) == 1
    assert len([c for c in github_client.calls if c["method"] == "remove_label"]) == 1
    assert (await events(ctx, row["id"]))[-1]["kind"] == "woken"


async def test_existing_merged_lane_supplies_change_link(ctx, github_client):
    from illo.ledger import github

    row = await make(ctx)
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await github.sync_item(conn, REPO, {"number": 2, "title": "A repair", "body": "Fixes #1", "state": "closed", "merged": True,
            "html_url": f"https://github.com/{REPO}/pull/2"}, {})
    await poll(ctx)
    assert any(link["kind"] == "change" for link in (await get(ctx, row["id"]))["links"])


async def test_run_title_that_names_the_ticket_links_the_run(ctx, github_client):
    row = await make(ctx)
    await wake.tick(ctx)
    number = github_client.issues[0]["number"]
    lane = {"id": 7, "source": "t3", "state": "working", "links": ["t3-thread://x"], "title": f"engine #{number}: Repair"}
    other = {**lane, "id": 8, "title": f"engine #{number}0: Another ticket"}
    async with ctx.pool.connection() as conn:
        await connector.lane_signal(ctx, conn, other, token={"person_key": "ada"})
        assert not (await get_without_pool(conn, row["id"]))["alive_at"]
        await connector.lane_signal(ctx, conn, lane, token={"person_key": "ada"})
        assert (await get_without_pool(conn, row["id"]))["alive_at"]
        # A second run stops. The first run still works, so the task is not stopped.
        helper = {**lane, "id": 9, "state": "done", "title": f"Review acme-example/engine#{number}"}
        await connector.lane_signal(ctx, conn, helper, token={"person_key": "ada"})
        assert not (await get_without_pool(conn, row["id"]))["stopped_at"]
        await connector.lane_signal(ctx, conn, {**lane, "state": "done"}, token={"person_key": "ada"})
        assert (await get_without_pool(conn, row["id"]))["stopped_at"]


async def test_hand_over_adds_no_comment(ctx, github_client):
    await make(ctx)
    await wake.tick(ctx)
    assert not any(c["method"] == "comment_issue" for c in github_client.calls)


@pytest.mark.parametrize("keyword,kind", [("Fixes", "closes"), ("Refs", "refs"), ("Ref", "refs"), ("Related to", "refs")])
async def test_pull_link_matches_ticket_once_without_closing(ctx, github_client, keyword, kind):
    from illo.ledger import github

    row = await make(ctx)
    await wake.tick(ctx)
    issue = github_client.issues[0]
    change = f"https://github.com/{REPO}/pull/22"
    async with ctx.pool.connection() as conn:
        pull = await github.sync_item(conn, REPO, {"number": 22, "title": "Repair the export",
            "body": f"{keyword} #{issue['number']}", "state": "open", "pull_request": {}, "html_url": change}, {})
        assert any(link["kind"] == kind for link in pull["links"])
        lane = {"id": 70, "source": "t3", "state": "working", "links": [change]}
        for _ in range(2):
            await connector.lane_signal(ctx, conn, lane, token={"person_key": "ada"})
        current = await get_without_pool(conn, row["id"])
        assert current["alive_at"] and current["state"] == "todo"
        assert len([link for link in current["links"] if link["kind"] == "change"]) == 1
        assert len([e for e in await tasks.events(conn, row["id"], viewer=ADA) if e["kind"] == "link"]) == 2
        assert github_client.issues[0]["state"] == "open"


async def test_unrelated_pull_or_another_repo_cannot_match_task(ctx):
    from illo.ledger import github

    ticket = f"https://github.com/{REPO}/issues/12"
    row = await make(ctx, links=[{"kind": "ticket", "url": ticket}])
    async with ctx.pool.connection() as conn:
        for repo, body in [(REPO, "Refs #13"), ("acme/other", "Refs #12")]:
            change = f"https://github.com/{repo}/pull/22"
            await github.sync_item(conn, repo, {"number": 22, "title": "Another change", "state": "open",
                "pull_request": {}, "body": body, "html_url": change}, {})
            await connector.lane_signal(ctx, conn, {"id": 80, "source": "t3", "state": "working", "links": [change]},
                                        token={"person_key": "ada"})
        current = await get_without_pool(conn, row["id"])
        assert not current["alive_at"] and len(current["links"]) == 1


async def test_open_pull_signal_adds_reference_change_to_review_task_once(ctx):
    from illo.ledger import github

    row = await make(ctx, links=[{"kind": "ticket", "url": f"https://github.com/{REPO}/issues/12"}])
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET state='review',waits_for='person:ada' WHERE id=%s", (row["id"],))
        item = {"number": 22, "title": "Repair the export", "state": "open", "pull_request": {},
                "body": "Refs #12", "html_url": f"https://github.com/{REPO}/pull/22"}
        await github.sync_item(conn, REPO, item, {})
        for _ in range(2):
            await connector.signal(ctx, conn, REPO, item)
        current = await get_without_pool(conn, row["id"])
        assert current["state"] == "review"
        assert len([link for link in current["links"] if link["kind"] == "change"]) == 1


async def test_github_network_call_has_no_task_graph_lock(ctx, github_client, monkeypatch):
    row = await make(ctx)
    original = github_client.assign_issue
    locks = []

    async def assign(*args, **kwargs):
        async with ctx.pool.connection() as conn:
            lock = await one(conn, "SELECT pg_try_advisory_xact_lock(12000001) AS ok")
            locks.append(lock["ok"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(github_client, "assign_issue", assign)
    await wake.tick(ctx)
    assert locks == [True]
    assert (await events(ctx, row["id"]))[-1]["kind"] == "woken"


async def test_failed_network_write_keeps_attempt_budget_and_audit(ctx, github_client, monkeypatch):
    from illo.ledger import kv

    await make(ctx)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("A network error")

    monkeypatch.setattr(github_client, "assign_issue", unavailable)
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert len(await kv.get(conn, "tasks:github-writes")) == 2
        attempts = await all_rows(conn, "SELECT * FROM audit WHERE tool='task_github_assign_issue'")
        assert len(attempts) == 1
