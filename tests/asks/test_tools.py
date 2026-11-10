"""Exercise the ask tools through the real authenticated HTTP endpoint."""

import pytest

from illo.ledger import asks, people, rules, runs


async def call(rpc, token, name, arguments=None):
    response = await rpc(token, "tools/call", {"name": name, "arguments": arguments or {}})
    return response.json()["result"]["structuredContent"]


async def test_create_answer_cancel_and_digest(app, rpc, rpc_token):
    ctx = app.state.ctx
    _, token = rpc_token
    result = await call(
        rpc,
        token,
        "asks_create",
        {
            "owner": "ada",
            "question": "Review Acme plan?",
            "options": [{"id": "yes", "label": "Proceed"}],
        },
    )
    assert result["decision"] == "allow"
    ask_id = result["result"]["ask_id"]
    listed = await call(rpc, token, "asks_list", {"owner": "ada"})
    assert listed["result"][0]["id"] == ask_id
    got = await call(rpc, token, "asks_get", {"id": ask_id})
    assert got["result"]["question"] == "Review Acme plan?"
    digest = await call(rpc, token, "asks_digest")
    assert "1 need you" in str(digest["result"])
    result = await call(rpc, token, "asks_answer", {"id": ask_id, "text": "Use the reviewed plan."})
    assert result["decision"] == "draft"
    result = await call(rpc, token, "asks_answer", {"id": ask_id, "option": "yes"})
    assert result["decision"] == "draft"
    created = await call(
        rpc, token, "asks_create", {"owner": "ada", "question": "Another Acme ask?"}
    )
    cancelled = await call(rpc, token, "asks_cancel_mine", {"id": created["result"]["ask_id"]})
    assert cancelled["result"]["cancelled"]
    assert ctx


@pytest.mark.parametrize("profile", ["team_channel", "agent_request", "tainted"])
async def test_digest_and_rule_removal_are_dm_only(app, rpc, profile):
    async with app.state.ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada" if profile != "team_channel" else "team",
            person_id=person["id"] if profile != "team_channel" else None,
            profile=profile,
            tainted=profile == "tainted",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    result = await call(rpc, token, "asks_digest")
    assert result["decision"] == "deny"
    result = await call(rpc, token, "rules_remove_mine", {"id": 1})
    assert result["decision"] == "deny"


async def test_answer_and_cancel_cannot_change_another_person_ask(app, rpc, rpc_token):
    ctx = app.state.ctx
    _, token = rpc_token
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        row = await asks.create(
            conn,
            owner_person_id=ben["id"],
            question="Ben decides",
            options=[{"id": "yes", "label": "Proceed"}],
        )
    answered = await call(rpc, token, "asks_answer", {"id": row["id"], "option": "yes"})
    cancelled = await call(rpc, token, "asks_cancel_mine", {"id": row["id"]})
    assert answered["decision"] == "deny" and not cancelled["result"]["cancelled"]


async def test_owner_can_list_and_remove_rule_in_dm(app, rpc, rpc_token):
    ctx = app.state.ctx
    _, token = rpc_token
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        rule = await rules.create(
            conn,
            owner_person_id=ada["id"],
            text="Use the reviewed plan",
            answer_option_label="Proceed",
        )
    listed = await call(rpc, token, "rules_list")
    assert listed["result"][0]["id"] == rule["id"]
    removed = await call(rpc, token, "rules_remove_mine", {"id": rule["id"]})
    assert removed["result"]["removed"]


async def test_rule_tool_applies_only_matching_rule(app, rpc, rpc_token):
    ctx = app.state.ctx
    _, token = rpc_token
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        row = await asks.create(
            conn,
            owner_person_id=ada["id"],
            question="Continue?",
            options=[{"id": "yes", "label": "Proceed"}],
        )
        rule = await rules.create(
            conn,
            owner_person_id=ada["id"],
            text="Use the reviewed plan",
            answer_option_label="Proceed",
        )
    result = await call(
        rpc, token, "asks_answer_with_rule", {"id": row["id"], "rule_id": rule["id"]}
    )
    assert result["result"]["answered"]
    assert (
        'Answered by the rule "Use the reviewed plan"' in result["result"]["answer"]["answer_text"]
    )


async def test_create_rejects_unknown_owner_and_invalid_recommendation(rpc, rpc_token):
    _, token = rpc_token
    result = await call(rpc, token, "asks_create", {"owner": "missing", "question": "Continue?"})
    assert result["decision"] == "deny"
    result = await call(
        rpc,
        token,
        "asks_create",
        {"owner": "ada", "question": "Continue?", "recommended_option": "missing"},
    )
    assert result["decision"] == "deny"
