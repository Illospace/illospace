"""Rule 3: a private lane cannot appear on another person's ask card."""

import pytest

from illo.db import one
from tests.mcp.client import AgentClient


@pytest.mark.parametrize("reported", [False, True])
async def test_rule_03_private_lane_refuses_another_ask_owner(app, http, reported):
    agent = await AgentClient.create(app, http)
    if reported:
        await agent.call(
            "lane_report",
            {
                "source": "t3",
                "external_id": "private-work",
                "title": "Private draft",
                "visibility": "private",
            },
        )
    args = {
        "owner": "ben",
        "lane": {"source": "t3", "external_id": "private-work"},
        "question": "Can you review a document?",
        "source_ref": "private-ask",
        "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
    }
    await agent.call("ask_create", args, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM asks") is None
    if reported:
        args["owner"] = "ada"
        assert (await agent.call("ask_create", args))["ask_id"]
