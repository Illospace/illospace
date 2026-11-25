"""Verify the split upgrade and the previous release's row contracts."""

from pathlib import Path

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation
from pydantic import ValidationError

from illo.asks import service
from illo.db import all_rows, migrate, one
from illo.ledger import runs
from illo.workspace import BrainConfig
from tests.board.test_escalate import escalation, queue

MIGRATIONS = Path(__file__).resolve().parents[2] / "illo/migrations"
ADD = "1902_board_coordinator.sql"
VALIDATE = "1903_board_coordinator_validate.sql"


async def test_upgrade_from_m2_and_validate_in_separate_transaction(pool, tmp_path):
    initial = tmp_path / "initial"
    initial.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if path.name < VALIDATE:
            (initial / path.name).symlink_to(path)
    add = initial / ADD
    add.unlink()

    async with pool.connection() as conn:
        try:
            await conn.execute("CREATE SCHEMA upgrade_board")
            await conn.execute("SET search_path TO upgrade_board")
            assert (await migrate(conn, initial))[-1] == "1901_board_wakes.sql"
            await conn.execute("""INSERT INTO board_threads(title,created_by,state,stalled_reason)
                VALUES ('Retained','agent:builder','resolved','turn_cap')""")
            await conn.commit()
            add.symlink_to(MIGRATIONS / ADD)
            assert await migrate(conn, initial) == [ADD]
            await conn.execute((MIGRATIONS / ADD).read_text())
            await conn.commit()
            constraint = "SELECT convalidated FROM pg_constraint WHERE conname=%s AND conrelid=%s::regclass"
            assert (await one(conn, constraint, ("runs_profile_check", "runs")))["convalidated"] is False
            assert (await one(conn, "SELECT closed_at FROM board_threads"))["closed_at"]
            new = await runs.create(conn, principal="team", person_id=None, profile="board_coordinator",
                                    provider="fake", model="fake")
            assert (await runs.get(conn, new["id"]))["profile"] == "board_coordinator"
            await conn.commit()
            assert (await migrate(conn))[:1] == [VALIDATE]
            assert (await one(conn, constraint, ("runs_profile_check", "runs")))["convalidated"] is True
        finally:
            await conn.rollback()
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA IF EXISTS upgrade_board CASCADE")
            await conn.commit()


async def test_escalation_constraints_and_active_episode_uniqueness(coordinator_ctx, client):
    ctx = coordinator_ctx
    q = await queue(ctx, client)
    another = await (await client()).call("board_post", {"title": "Elsewhere", "text": "Details"})
    row = await escalation(ctx)
    async with ctx.pool.connection() as conn:
        for statement, params, error in [
            ("UPDATE board_escalations SET slack_ts='1.2' WHERE id=%s", (row["id"],), CheckViolation),
            ("UPDATE board_escalations SET post_id=%s WHERE id=%s", (another["post_id"], row["id"]),
             ForeignKeyViolation),
            ("UPDATE board_escalations SET answer_post_id=%s WHERE id=%s", (another["post_id"], row["id"]),
             ForeignKeyViolation),
        ]:
            with pytest.raises(error):
                async with conn.transaction():
                    await conn.execute(statement, params)
        await conn.execute("UPDATE board_episodes SET state='headstart'")
        added = await all_rows(conn, """INSERT INTO board_episodes (thread_id,reason,post_id,episode_seq)
            VALUES (%s,'question',%s,2) ON CONFLICT DO NOTHING RETURNING id""", (q["thread_id"], q["post_id"]))
        assert added == []
        await conn.execute("UPDATE board_episodes SET state='stuck',closed_at=now()")
        added = await all_rows(conn, """INSERT INTO board_episodes (thread_id,reason,post_id,episode_seq)
            VALUES (%s,'question',%s,2) ON CONFLICT DO NOTHING RETURNING id""", (q["thread_id"], q["post_id"]))
        assert len(added) == 1


async def test_stock_ask_tick_cannot_deliver_card_before_committed_receipt(coordinator_ctx, slack, client):
    from illo.board import escalate

    ctx = coordinator_ctx
    await queue(ctx, client, "card", summary=True)
    await service.tick(ctx)
    assert not slack.calls
    await escalate._claim(ctx, [])
    await service.tick(ctx)
    assert not slack.calls
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM board_escalations"))["state"] == "sending"
        assert not await one(conn, "SELECT 1 FROM asks")


def test_coordinator_settings_are_optional_and_forward_compatible(coordinator_ctx):
    from typing import Literal

    from pydantic import create_model

    from illo.workspace import BoardConfig, BoardCoordinatorConfig, RouteSettings

    assert BoardConfig(future_key=12).model_extra == {"future_key": 12}
    cfg = BoardCoordinatorConfig(future_key=12)
    assert cfg.enabled and cfg.slack_summaries and cfg.model_extra == {"future_key": 12}
    previous = create_model("PreviousBrain", __base__=BrainConfig, routes=(
        dict[Literal["default", "person_dm", "team_channel", "tainted", "routine", "digest", "agent_request", "deep"],
             RouteSettings], ...))
    data = coordinator_ctx.workspace.config.brain.model_dump(exclude_unset=True)
    previous.model_validate(data)
    with pytest.raises(ValidationError):
        previous.model_validate(data | {"routes": data["routes"] | {"board_coordinator": {}}})


async def test_database_refuses_a_rollback_draft_answer_for_a_delivered_card(coordinator_ctx, slack, client):
    from illo.board import escalate

    ctx = coordinator_ctx
    await queue(ctx, client, "card", summary=True)
    await escalate.send_due(ctx)
    row = await escalation(ctx)
    async with ctx.pool.connection() as conn:
        with pytest.raises(CheckViolation) as exc:
            async with conn.transaction():
                await conn.execute("""UPDATE asks SET state='answered',answered_via='approved_draft'
                    WHERE id=%s""", (row["ask_id"],))
        assert exc.value.diag.constraint_name == "asks_board_answer_via_check"
