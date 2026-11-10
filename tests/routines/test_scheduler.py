"""Persist due times and claims across repeated ticks and restarts."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from illo.cli import execute, parser
from illo.db import all_rows, one
from illo.gateway.principal import resolve
from illo.routines.scheduler import list_routines, next_due, tick_routines
from illo.workspace import Routine


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("2026-01-01T00:00:00+00:00", "2026-01-01T14:00:00+00:00"),
        ("2026-03-07T15:00:00+00:00", "2026-03-08T13:00:00+00:00"),
        ("2026-10-31T15:00:00+00:00", "2026-11-01T14:00:00+00:00"),
    ],
)
def test_timezone_and_dst(after, expected):
    assert next_due(
        "0 9 * * *", datetime.fromisoformat(after), "America/New_York"
    ) == datetime.fromisoformat(expected)


def configure(ctx, *, principal="team", destination="none", enabled=True):
    # Each app owns this copy. The shared fixture remains unchanged.
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.routines = [
        Routine(
            name="test-routine",
            schedule="* * * * *",
            principal=principal,
            destination=destination,
            enabled=enabled,
            body="Read the active lanes.",
        )
    ]


async def test_due_claim_once_across_restart_and_concurrent_ticks(app, fake_brain):
    ctx = app.state.ctx
    configure(ctx)
    now = datetime(2026, 1, 1, 9, tzinfo=UTC)
    await asyncio.gather(tick_routines(ctx, now), tick_routines(ctx, now))
    async with ctx.pool.connection() as conn:
        state = await one(conn, "SELECT * FROM routine_state WHERE key='test-routine'")
        assert state["last_status"] == "completed"
        assert state["next_run_at"] == now + timedelta(minutes=1)
        await conn.execute(
            "UPDATE routine_state SET next_run_at=%s WHERE key='test-routine'", (now,)
        )
    before = len(fake_brain.requests)
    await tick_routines(ctx, now)
    assert len(fake_brain.requests) == before == 1
    async with ctx.pool.connection() as conn:
        assert (
            len(
                await all_rows(
                    conn,
                    "SELECT * FROM events WHERE channel='routine' AND external_id LIKE 'routine:test-routine:%'",
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    ("hours", "runs", "status"), [(2, 1, "completed"), (6, 0, "skipped"), (8, 0, "skipped")]
)
async def test_late_policy(app, fake_brain, hours, runs, status):
    ctx = app.state.ctx
    configure(ctx)
    now = datetime(2026, 1, 1, 9, tzinfo=UTC)
    await tick_routines(ctx, now - timedelta(minutes=1))
    fake_brain.requests.clear()
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE routine_state SET next_run_at=%s WHERE key='test-routine'",
            (now - timedelta(hours=hours),),
        )
    await tick_routines(ctx, now)
    assert len(fake_brain.requests) == runs
    async with ctx.pool.connection() as conn:
        state = await one(conn, "SELECT * FROM routine_state WHERE key='test-routine'")
        assert state["last_status"] == status
        assert state["next_run_at"] > now


@pytest.mark.parametrize(
    ("destination", "target"),
    [("dm:ada", "D_EXAMPLE"), ("channel:alerts", "C_ACME_ALERTS"), ("none", None)],
)
async def test_destination_delivery(app, slack_client, destination, target):
    ctx = app.state.ctx
    configure(ctx, destination=destination)
    await tick_routines(ctx, datetime(2026, 1, 1, 9, tzinfo=UTC))
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert [c["channel"] for c in posts] == ([target] if target else [])


async def test_disabled_and_changed_file(app, fake_brain):
    ctx = app.state.ctx
    configure(ctx, enabled=False)
    now = datetime(2026, 1, 1, 9, tzinfo=UTC)
    await tick_routines(ctx, now)
    assert not fake_brain.requests
    ctx.workspace.routines[0].enabled = True
    ctx.workspace.routines[0].body = "The new prompt."
    await tick_routines(ctx, now)
    assert fake_brain.requests[0].user_message == "The new prompt."
    async with ctx.pool.connection() as conn:
        assert (await list_routines(conn, ctx.workspace))[0]["last_status"] == "completed"


async def test_person_routine_uses_owner_grant(app, fake_brain):
    ctx = app.state.ctx
    configure(ctx, principal="person:ada")
    await tick_routines(ctx, datetime(2026, 1, 1, 9, tzinfo=UTC))
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs WHERE id=%s", (fake_brain.requests[0].run_id,))
        assert row["principal"] == "person:ada" and row["profile"] == "person_dm"


async def test_routine_list_cli(conn, workspace, capsys, monkeypatch):
    from illo import cli

    class ConnectionContext:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            pass

    async def connect(*args, **kwargs):
        return ConnectionContext()

    monkeypatch.setattr(cli.AsyncConnection, "connect", connect)
    assert (
        await execute(
            parser().parse_args(["routines", "list"]), cli.Config(workspace_dir=workspace.root)
        )
        == 0
    )
    assert "daily-digest" in capsys.readouterr().out


async def test_unknown_routine_cannot_choose_a_principal(conn, workspace):
    from illo.gateway import InboundEvent

    event = InboundEvent(
        channel="routine",
        external_id="forged",
        kind="scheduled",
        payload={"routine": "missing", "principal": "person:ada"},
    )
    assert await resolve(conn, event, workspace) is None


def test_dst_missing_and_repeated_hour():
    # The spring gap runs at the next real local time. Both fall instants are distinct.
    assert next_due(
        "30 2 * * *", datetime.fromisoformat("2026-03-07T15:00:00+00:00"), "America/New_York"
    ) == datetime.fromisoformat("2026-03-08T07:00:00+00:00")
    assert next_due(
        "30 1 * * *", datetime.fromisoformat("2026-11-01T05:45:00+00:00"), "America/New_York"
    ) == datetime.fromisoformat("2026-11-01T06:30:00+00:00")


async def test_file_reload_applies_routine_fields(app, tmp_path):
    import shutil

    from illo.workspace import load_workspace

    root = tmp_path / "workspace"
    shutil.copytree(app.state.ctx.workspace.root, root)
    path = root / "routines/added.md"
    path.write_text(
        "---\nschedule: '* * * * *'\nprincipal: team\ndestination: none\nenabled: false\nskills: []\n---\nFirst prompt."
    )
    ctx = app.state.ctx
    ctx.config.workspace_dir = root
    await ctx.reload()
    routine = next(r for r in ctx.workspace.routines if r.name == "added")
    assert not routine.enabled and routine.body == "First prompt."
    path.write_text(path.read_text().replace("false", "true").replace("First", "Next"))
    await ctx.reload()
    routine = next(r for r in ctx.workspace.routines if r.name == "added")
    assert routine.enabled and routine.body == "Next prompt."
    assert load_workspace(root).config.email.reply == "both"
