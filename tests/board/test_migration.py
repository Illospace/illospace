from pathlib import Path

import pytest
import yaml
from psycopg.errors import ForeignKeyViolation
from pydantic import ValidationError, create_model

from illo.db import all_rows, migrate, one
from illo.workspace import BoardConfig, Model, TeamConfig

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "illo/migrations"
BOARD_TABLES = {"board_threads", "board_posts", "board_mentions", "board_mention_acks", "board_wakes"}


async def test_board_migration_applies_to_an_existing_database_and_a_fresh_one(pool, tmp_path):
    before_board = tmp_path / "previous"
    before_board.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if path.name < "1900_board.sql":
            (before_board / path.name).symlink_to(path)

    async with pool.connection() as conn:
        fresh = await all_rows(conn, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
        assert BOARD_TABLES <= {r["tablename"] for r in fresh}
        try:
            await conn.execute("CREATE SCHEMA board_upgrade")
            await conn.execute("SET search_path TO board_upgrade")
            assert "1900_board.sql" not in await migrate(conn, before_board)
            await conn.execute("INSERT INTO people (key,name) VALUES ('ada','Ada')")
            await conn.commit()
            assert await migrate(conn) == [
                "1900_board.sql", "1901_board_wakes.sql",
                "1902_board_coordinator.sql", "1903_board_coordinator_validate.sql",
            ]
            assert await migrate(conn) == []
            assert await one(conn, "SELECT name FROM people WHERE key='ada'") == {"name": "Ada"}
            tables = await all_rows(conn, "SELECT tablename FROM pg_tables WHERE schemaname='board_upgrade'")
            assert BOARD_TABLES <= {r["tablename"] for r in tables}
        finally:
            await conn.rollback()
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA IF EXISTS board_upgrade CASCADE")
            await conn.commit()


def test_deploy_example_keeps_rollback_compatible_yaml():
    data = yaml.safe_load((ROOT / "workspace.example/illo.yaml").read_text())
    # The previous workspace schema forbids unknown keys and has no board field.
    old_config = create_model("OldConfig", __base__=Model, **{
        name: (info.annotation, info) for name, info in TeamConfig.model_fields.items() if name != "board"
    })
    assert old_config.model_validate(data).team_name == data["team_name"]
    with pytest.raises(ValidationError, match="Extra inputs"):
        old_config.model_validate({**data, "board": {}})
    assert TeamConfig.model_validate(data).board == BoardConfig(
        enabled=True, max_consecutive_agent_posts=20, wake_github=True, wake_webhooks=True,
    )


@pytest.mark.parametrize("cap", [0, 101])
def test_board_turn_cap_range(cap):
    with pytest.raises(ValidationError):
        BoardConfig(max_consecutive_agent_posts=cap)


async def test_same_thread_foreign_keys_and_append_only_links(app, client, task):
    ada = await client()
    task_id = await task()
    first = await ada.call("board_post", {"task_id": task_id, "text": "First"})
    other = await ada.call("board_post", {"title": "Second", "text": "Second"})
    wrong_pair = (first["thread_id"], other["post_id"])
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM board_wakes")
        for statement, params in [
            ("""INSERT INTO board_posts (thread_id,reply_to_post_id,actor,kind,text)
                VALUES (%s,%s,'agent:test','answer','Wrong')""", wrong_pair),
            ("""INSERT INTO board_mentions (thread_id,post_id,target,recipient_person_id)
                VALUES (%s,%s,'person:ada',%s)""", (*wrong_pair, ada.person_id)),
            ("""INSERT INTO board_wakes (thread_id,post_id,target_kind,target_ref)
                VALUES (%s,%s,'github_issue','example/project#1')""", wrong_pair),
            ("DELETE FROM tasks WHERE id=%s", (task_id,)),
            ("DELETE FROM board_threads WHERE id=%s", (first["thread_id"],)),
        ]:
            with pytest.raises(ForeignKeyViolation):
                async with conn.transaction():
                    await conn.execute(statement, params)
