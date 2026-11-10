"""Rules 3 and 6: personal facts need a draft. Team and tainted runs cannot propose notes."""

import pytest

from illo.db import all_rows, one
from illo.ledger import people, runs
from tests.tool_results import read_result


async def test_rule_03_note_after_personal_read_is_draft(app, rpc, rpc_token, slack_client):
    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "notes_propose",
            "arguments": {"title": "Private fact", "text": "A personal fact", "about": "Private"},
        },
    )
    result = read_result(response.json()["result"])
    assert result["decision"] == "draft"
    assert not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM note_proposals")
        draft = await one(conn, "SELECT * FROM drafts WHERE id=%s", (result["result"]["draft_id"],))
        assert draft["approver_person_id"] == run["person_id"]
        assert draft["destination"] == "ledger" and draft["kind"] == "notes_propose"


@pytest.mark.parametrize(
    ("principal", "profile", "tainted"),
    [
        ("team", "team_channel", False),
        ("team", "tainted", True),
        ("person:ada", "person_dm", True),
        ("outsider", "tainted", True),
        ("team", "routine", False),
    ],
)
async def test_rule_06_notes_propose_denied(app, rpc, principal, profile, tainted):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal=principal,
            person_id=ada["id"] if principal.startswith("person:") else None,
            profile=profile,
            runtime="fake",
            model="fake",
            tainted=tainted,
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/list")
    assert "notes_propose" not in [t["name"] for t in response.json()["result"]["tools"]]
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "notes_propose",
            "arguments": {"title": "Unsafe", "text": "Do not save", "about": "Test"},
        },
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"
    async with app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM note_proposals")
