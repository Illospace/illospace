"""Rule 10: the brain sees only registered Illo tools, with no runtime escape tool."""

import pytest

from illo.db import all_rows
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
            provider="fake", tool_limit=12,
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/list")
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names <= app.state.ctx.registry.tools.keys()
    if profile == "board_coordinator":
        expected = {
            "people_list", "people_get", "lanes_search", "lanes_get", "lanes_mine",
            "asks_list", "asks_get", "rules_list", "search_all", "search_get", "notes_search",
            "github_search_issues", "github_get_issue", "github_get_pull", "github_get_commit",
            "slack_read_thread", "lists", "task_list", "task_get",
            "need_answer", "need_route", "need_ask_person", "need_wait",
        }
        if app.state.ctx.workspace.config.pages.enabled:
            expected.add("page_read")
        assert names == expected
        for name in ("board_read", "asks_create", "web_read_public_page", "slack_send_dm",
                     "github_comment_issue", "mail_search", "calendar_list", "connectors_call", "shell"):
            call = await rpc(token, "tools/call", {"name": name, "arguments": {"command": "Private test text"}})
            result = call.json()["result"]
            assert result["isError"]
        malformed = await rpc(token, "tools/call", {"name": "shell", "arguments": "Private test text"})
        assert malformed.json()["error"]["code"] == -32602
        async with app.state.ctx.pool.connection() as conn:
            denied = await all_rows(conn, "SELECT args FROM audit WHERE run_id=%s AND decision='deny'", (run["id"],))
        assert len(denied) == 10
        assert denied[-1] == {"args": {}}
    else:
        assert names
    forbidden = ("shell", "exec", "bash", "file", "fetch", "browse", "web")
    assert all(
        name == "web_read_public_page" or not any(part in name for part in forbidden)
        for name in names
    )
