"""Rules 3 and 6: inbound search adds no channel read to a tainted run."""

import pytest

from illo.ledger import runs
from illo.sources import search
from tests.search.test_slack_events import event


@pytest.mark.parametrize("profile,principal", [("tainted", "team"), ("person_dm", "person:ada")])
async def test_rule_03_06_tainted_runs_cannot_search_or_read_other_channel_events(
    app, rpc, profile, principal, ada
):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        row = await event(conn, channel_id="C_ACME_TEAM", text="A team-only Nebula decision.")
        run = await runs.create(conn, principal=principal, person_id=ada["id"] if principal.startswith("person:") else None, profile=profile,
                                tainted=True, runtime="fake", model="fake")
        token = await runs.mint_token(conn, run["id"], 60)
    tools = (await rpc(token, "tools/list")).json()["result"]["tools"]
    assert not {"search_all", "search_get"} & {t["name"] for t in tools}
    for name, args in [("search_all", {"query": "Nebula"}),
                       ("search_get", {"source_ref": f"slack-event:{row['id']}"})]:
        result = (await rpc(token, "tools/call", {"name": name, "arguments": args})).json()["result"]
        assert result["isError"] and "team-only" not in str(result)
    assert not await search.search(ctx, principal, "Nebula", tainted=True)
