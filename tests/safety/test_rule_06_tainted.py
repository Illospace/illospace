"""Rule 6: outside text gets a small grant and no personal data."""

from illo.ledger import lanes, people, runs
from illo.tools.registry import Tool


async def test_tainted_run_hides_personal_tools_and_private_lanes(app, rpc):
    async def personal(ctx, args):
        raise AssertionError("This handler must not run")

    app.state.ctx.registry.tools.pop("mail_search", None)
    app.state.ctx.registry.add(
        Tool("mail_search", "personal", "read", lambda a: "ledger", True, personal)
    )
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await lanes.upsert(
            conn,
            source="manual",
            external_id="private",
            title="Private lane",
            owner_person_id=ada["id"],
            visibility="private",
        )
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            tainted=True,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    names = {t["name"] for t in (await rpc(token, "tools/list")).json()["result"]["tools"]}
    assert "mail_search" not in names and "rules_remove_mine" not in names
    assert (await rpc(token, "tools/call", {"name": "mail_search"})).json()["result"]["isError"]
    result = (await rpc(token, "tools/call", {"name": "lanes_search"})).json()["result"][
        "structuredContent"
    ]
    assert result["decision"] == "deny"


async def test_outsider_has_no_team_ledger_authority(app, rpc):
    async with app.state.ctx.pool.connection() as conn:
        run = await runs.create(
            conn,
            principal="outsider",
            person_id=None,
            profile="tainted",
            tainted=True,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    tools = (await rpc(token, "tools/list")).json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["web_read_public_page"]
    result = (await rpc(token, "tools/call", {"name": "people_list"})).json()["result"]
    assert result["isError"]
