import pytest

from illo.db import one
from illo.ledger import people, tokens
from tests.mcp.client import AgentClient


@pytest.fixture
def client(app, http):
    async def create(person="ada", *, agent=None, scope="agent"):
        ctx = app.state.ctx
        async with ctx.pool.connection() as conn:
            owner = await people.get(conn, person)
            token = await tokens.create(conn, owner["id"], "Board test", scopes=[scope],
                                        agent_key=agent, workspace=ctx.workspace)
            row = await tokens.verify(conn, token)
        return AgentClient(app, http, token, row["id"], owner["id"])

    return create


@pytest.fixture
def task(app):
    async def create(title="Prepare the release", *, ticket=None, owner=None):
        async with app.state.ctx.pool.connection() as conn:
            row = await one(conn, """INSERT INTO tasks
                (list_key,title,brief,created_by,owner_person_id,visibility)
                VALUES ('bugs',%s,'','person:ada',%s,%s) RETURNING id""",
                (title, owner, "private" if owner else "team"))
            if ticket:
                await conn.execute("INSERT INTO task_tickets(url,task_id) VALUES (%s,%s)",
                                   (ticket, row["id"]))
            return row["id"]

    return create
