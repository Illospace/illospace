"""Rule 10: the brain sees only registered Illo tools, with no runtime escape tool."""

import pytest

from illo.ledger import people, runs
from illo.tools.checks import GRANT_PROFILES


@pytest.mark.parametrize("profile", GRANT_PROFILES)
async def test_actual_http_tool_list_has_no_runtime_escape(app, rpc, profile):
    async with app.state.ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=person["id"],
            profile=profile,
            tainted=profile == "tainted",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/list")
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names <= app.state.ctx.registry.tools.keys()
    assert names
    forbidden = ("shell", "exec", "bash", "file", "fetch", "browse", "web")
    assert all(
        name == "web_read_public_page" or not any(part in name for part in forbidden)
        for name in names
    )
