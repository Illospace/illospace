"""Refuse an impossible GitHub hand-over and end configuration failures once."""

from datetime import UTC, datetime, timedelta

import pytest

from illo.channels.slack import tasks as surface
from illo.connectors import github_tasks
from illo.db import all_rows, one
from illo.ledger import tasks
from illo.tasks import wake
from illo.tasks.connectors import WakeConfig

ADA = tasks.Viewer("ada", "person:ada", human=True)


@pytest.fixture
async def ctx(app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    agent = ctx.workspace.agents[0]
    agent.name = "Server agents"
    agent.wake.repos = ["Acme/app", "Acme/site"]
    agent.wake.default_repo = None
    return ctx


async def make(ctx, **kwargs):
    async with ctx.pool.connection() as conn:
        return await tasks.create(conn, workspace=ctx.workspace, viewer=ADA,
            list_key="bugs", title="Repair the export", **kwargs)


async def test_create_refuses_missing_and_unsupported_repository(ctx):
    for links, text in [([], "Server agents need a repository. Add a link of kind repo in the same call. They work in: Acme/app, Acme/site."),
        ([{"kind": "repo", "url": "https://github.com/Acme/other"}],
         "Server agents cannot work in Acme/other. They work in: Acme/app, Acme/site. Add a link of kind repo to one of these in the same call, or choose another holder.")]:
        with pytest.raises(tasks.TaskError) as error:
            await make(ctx, links=links)
        assert str(error.value) == text
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM tasks")
        assert not await all_rows(conn, "SELECT * FROM task_wakes")


@pytest.mark.parametrize("kind, url", [("repo", "https://github.com/acme/APP"),
    ("ticket", "https://github.com/Acme/site/issues/5")])
async def test_handover_carries_links_in_the_same_call(ctx, kind, url):
    row = await make(ctx, holder="person:ada")
    async with ctx.pool.connection() as conn:
        with pytest.raises(tasks.TaskError, match="need a repository"):
            await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="agent:build")
        assert (await tasks.get(conn, row["id"], viewer=ADA))["holder"] == "person:ada"
        updated = await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA,
            holder="agent:build", links=[{"kind": kind, "url": url}])
        assert updated["holder"] == "agent:build"
        assert updated["links"] == [{"kind": kind, "url": url}]


async def test_default_repository_and_other_holders(ctx):
    ctx.workspace.agents[0].wake.default_repo = "Acme/site"
    assert (await make(ctx))["holder"] == "agent:build"
    ctx.workspace.agents[0].wake = WakeConfig(type="poll")
    assert (await make(ctx))["holder"] == "agent:build"
    ctx.workspace.agents[0].wake = WakeConfig(type="none")
    assert (await make(ctx))["holder"] == "agent:build"
    assert (await make(ctx, holder="person:ada"))["holder"] == "person:ada"


@pytest.mark.parametrize("cause, reason", [("missing", "The task has no repository link."),
    ("unsupported", "Server agents cannot work in Acme/other."),
    ("github", "GitHub is not configured.")])
async def test_configuration_failure_stops_once_and_slack_has_next_step(ctx, cause, reason):
    # A task can predate the new validation or a later agent config change.
    ctx.workspace.agents[0].wake.default_repo = "Acme/app"
    links = [{"kind": "repo", "url": "https://github.com/Acme/other"}] if cause == "unsupported" else []
    row = await make(ctx, links=links)
    ctx.workspace.agents[0].wake.default_repo = None
    if cause == "github":
        ctx.github_client = None
    for seconds in (0, 20, 40, 60):
        await wake.tick(ctx, datetime.now(UTC) + timedelta(seconds=seconds))
    async with ctx.pool.connection() as conn:
        pending = await one(conn, "SELECT * FROM task_wakes WHERE task_id=%s", (row["id"],))
        assert pending["attempts"] == 1 and pending["finished_at"]
        current = await tasks.get(conn, row["id"], viewer=ADA)
        assert current["waits_for"] == "person:ada"
        failures = await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s AND kind='wake_failed'", (row["id"],))
        assert len(failures) == 1 and failures[0]["data"]["final"]
        assert failures[0]["text"] == reason
    await surface.tick(ctx)
    texts = [call["text"] for call in ctx.slack_client.calls if call["method"] == "chat_postMessage"]
    notices = [text for text in texts if f"I could not start Server agents on task #{row['id']}." in text]
    assert notices and all("link of kind repo" not in text for text in notices)
    if cause == "unsupported":
        assert any("They do not work in Acme/other. Tell me another holder, or add that repository to the agent in `agents.yaml`." in text for text in notices)


async def test_github_transient_failure_keeps_three_attempts(ctx):
    ctx.workspace.agents[0].wake.default_repo = "Acme/app"
    row = await make(ctx)
    calls = 0

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("A private transport detail")

    ctx.github_client.search_issues = fail
    for seconds in (0, 20, 40, 60):
        await wake.tick(ctx, datetime.now(UTC) + timedelta(seconds=seconds))
    assert calls == 3
    async with ctx.pool.connection() as conn:
        pending = await one(conn, "SELECT * FROM task_wakes WHERE task_id=%s", (row["id"],))
        assert pending["attempts"] == 3 and pending["finished_at"]
        assert (await tasks.get(conn, row["id"], viewer=ADA))["waits_for"] == "person:ada"


def test_repository_choice_prefers_allowed_ticket_and_rejects_other_hosts():
    from types import SimpleNamespace

    agent = SimpleNamespace(wake=github_tasks.IssueWake(assignee="acme-bot", repos=["Acme/app"]))
    links = [{"kind": "repo", "url": "https://github.com/Acme/app"},
             {"kind": "ticket", "url": "https://github.com/Acme/app/issues/7"}]
    assert github_tasks.repository_choice(agent, links) == (links[1], "Acme/app", None)
    assert github_tasks.repository_choice(agent, [{"kind": "repo", "url": "https://elsewhere.example/Acme/app"}]) == (None, None, None)
