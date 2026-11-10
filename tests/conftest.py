"""One isolated database for each session. No real channel or model calls."""

import os
import uuid
from pathlib import Path

import httpx
import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from illo.app import create_app
from illo.brain.fake import FakeBrain
from illo.config import Config
from illo.db import create_pool, migrate
from illo.gateway import InboundEvent
from illo.workspace import load_workspace
from tests.fakes import FakeChannelAdapter, FakeGitHubClient, FakeSlackClient

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
async def database_pool():
    """Use the admin database only to create and drop this session's database."""
    url = os.getenv("ILLO_TEST_DATABASE_URL", "postgresql://illo:illo@127.0.0.1:55432/postgres")
    name = "illo_test_" + uuid.uuid4().hex
    admin = await AsyncConnection.connect(url, autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(url)
    params["dbname"] = name
    pool = create_pool(make_conninfo(**params), min_size=1, max_size=8)
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await migrate(conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        await admin.close()


@pytest.fixture
async def pool(database_pool):
    """Truncate only the random test database. Keep its migration record."""
    async with database_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'schema_migrations'"
            )
        ).fetchall()
        tables = sql.SQL(",").join(sql.Identifier(row["tablename"]) for row in rows)
        await conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(tables))
    yield database_pool


@pytest.fixture(scope="session")
def workspace():
    return load_workspace(ROOT / "workspace.example")


@pytest.fixture
async def conn(pool, workspace):
    async with pool.connection() as connection:
        await workspace.sync_people(connection)
        yield connection


@pytest.fixture
async def ada(conn):
    from illo.ledger.people import get

    return await get(conn, "ada")


@pytest.fixture
async def ben(conn):
    from illo.ledger.people import get

    return await get(conn, "ben")


@pytest.fixture
async def run(conn, ada):
    from illo.ledger.runs import create

    return await create(
        conn,
        principal="person:ada",
        person_id=ada["id"],
        profile="person_dm",
        runtime="fake",
        model="fake",
    )


@pytest.fixture
def slack_client():
    return FakeSlackClient()


@pytest.fixture
def github_client():
    return FakeGitHubClient()


@pytest.fixture
def fake_brain():
    return FakeBrain(final_text="The lane is working.")


@pytest.fixture
async def app(pool, fake_brain, slack_client, github_client):
    config = Config(
        workspace_dir=ROOT / "workspace.example",
        public_url="http://test",
        revision="test-revision",
        reload_token="test-reload",
    )
    application = create_app(config, pool=pool, brain_client=fake_brain)
    fake_brain.transport = httpx.ASGITransport(app=application)
    async with application.router.lifespan_context(application):
        application.state.ctx.slack_client = slack_client
        application.state.ctx.github_client = github_client
        yield application


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def channel():
    return FakeChannelAdapter()


@pytest.fixture
def replay(app, channel):
    async def replay_event(**changes):
        event = InboundEvent.model_validate(
            {
                "channel": "slack",
                "external_id": uuid.uuid4().hex,
                "kind": "dm",
                "sender": "U_ACME_ADA",
                "text": "Give me a lane update",
                **changes,
            }
        )
        return await app.state.ctx.gateway.receive(event, channel)

    return replay_event


@pytest.fixture
async def rpc_token(app):
    from illo.ledger import people, runs

    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    return run, token


@pytest.fixture
def rpc(http):
    async def call(token, method, params=None):
        return await http.post(
            "/brain/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )

    return call
