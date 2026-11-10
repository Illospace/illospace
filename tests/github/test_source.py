"""GitHub snapshots use source facts and durable cursors."""

import pytest

from illo.db import all_rows, one
from illo.ledger import github as ledger
from illo.ledger import kv
from illo.sources.github import poll

REPO = "acme-example/engine"


def issue(number=1, **changes):
    return {
        "number": number,
        "repo": REPO,
        "title": "Example problem",
        "body": "First line\nSecond line",
        "state": "open",
        "labels": [],
        "assignees": [{"login": "acme-ada-example"}],
        "user": {"login": "acme-ben-example"},
        "comments_data": [{"body": "Earlier\nLast line"}],
        **changes,
    }


async def test_first_poll_and_incremental_state_change(app, github_client):
    ctx = app.state.ctx
    github_client.issues = [issue(), issue(2, state="closed")]
    await poll(ctx)
    async with ctx.pool.connection() as conn:
        lane = await one(conn, "SELECT * FROM lanes")
        assert lane["external_id"] == f"{REPO}#1"
        assert lane["state"] == "unknown"
        assert lane["summary"] == "First line\nSecond line\nLast line"
        assert (
            lane["owner_person_id"]
            == (await one(conn, "SELECT id FROM people WHERE key='ada'"))["id"]
        )
        assert await kv.get(conn, f"github:cursor:{REPO}")
    github_client.issues[0]["labels"] = [{"name": "agent:working"}]
    await poll(ctx)
    await poll(ctx)
    async with ctx.pool.connection() as conn:
        lane = await one(conn, "SELECT * FROM lanes WHERE external_id=%s", (f"{REPO}#1",))
        assert lane["state"] == "working"
        events = await all_rows(
            conn, "SELECT * FROM lane_events WHERE lane_id=%s ORDER BY id", (lane["id"],)
        )
        assert [e["data"]["to"] for e in events] == ["unknown", "working"]
    polls = [c for c in github_client.calls if c["method"] == "list_changed"]
    assert polls[0]["since"] is None and polls[1]["since"] is not None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"labels": ["agent:working"]}, "working"),
        ({"labels": ["agent:needs-input"]}, "waiting_person"),
        ({"labels": ["agent:hold"]}, "blocked"),
        ({"requested_reviewers": [{"login": "reviewer"}]}, "review"),
        ({"state": "closed", "state_reason": "completed"}, "done"),
        ({"state": "closed", "state_reason": "not_planned"}, "failed"),
    ],
)
def test_state_from_facts(changes, expected):
    from illo.workspace import GitHubConfig

    assert ledger.item_state(issue(**changes), GitHubConfig().label_map) == expected


async def test_custom_label_owner_fallback_and_closed_at(conn):
    lane = await ledger.sync_item(
        conn, REPO, issue(assignees=[{"login": "outsider"}], labels=["doing"]), {"doing": "working"}
    )
    assert lane["state"] == "working"
    assert (
        lane["owner_person_id"] == (await one(conn, "SELECT id FROM people WHERE key='ben'"))["id"]
    )
    closed = issue(state="closed", closed_at="2026-01-02T03:04:05Z")
    lane = await ledger.sync_item(conn, REPO, closed, {})
    first_closed = lane["closed_at"]
    assert first_closed.isoformat() == "2026-01-02T03:04:05+00:00"
    lane = await ledger.sync_item(conn, REPO, closed, {})
    assert lane["closed_at"] == first_closed
    lane = await ledger.sync_item(conn, REPO, issue(), {})
    assert lane["closed_at"] is None


async def test_pull_links_issue_even_if_pull_arrives_first(app, github_client):
    github_client.pulls = [issue(2, pull_request={}, body="Closes #1", merged=False)]
    github_client.issues = [issue()]
    ctx = app.state.ctx
    # Test the reverse arrival order independently from GitHub's sort order.
    async with ctx.pool.connection() as conn:
        await ledger.sync_item(conn, REPO, github_client.pulls[0], {})
    await poll(ctx)
    async with ctx.pool.connection() as conn:
        pull = await one(conn, "SELECT * FROM lanes WHERE external_id=%s", (f"{REPO}#2",))
        linked = await one(conn, "SELECT * FROM lanes WHERE external_id=%s", (f"{REPO}#1",))
        assert next(v for v in pull["links"] if v["kind"] == "closes")["lane_id"] == linked["id"]
    assert any(c["method"] == "get_pull" for c in github_client.calls)


async def test_failed_poll_keeps_cursor(app, github_client, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(github_client, "get_issue", fail)
    github_client.issues = [issue()]
    with pytest.raises(RuntimeError):
        await poll(app.state.ctx)
    async with app.state.ctx.pool.connection() as conn:
        assert await kv.get(conn, f"github:cursor:{REPO}") is None
        assert not await one(conn, "SELECT id FROM lanes")
        assert (await one(conn, "SELECT * FROM sensors WHERE key='github'"))["last_error_at"]
