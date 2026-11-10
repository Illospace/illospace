"""Rule 8: an owner rule answers only with an option the agent proposed."""

import pytest

from illo.ledger import asks, people, rules


@pytest.mark.parametrize(
    "value,expected", [("Continue", "yes"), ("yes", None), ("Run a different command", None)]
)
async def test_rule_08_only_exact_proposed_label_and_names_rule(conn, ada, value, expected):
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Continue?",
        options=[{"id": "yes", "label": "Continue"}],
    )
    rule = await rules.create(
        conn, owner_person_id=ada["id"], text="Use the reviewed plan", answer_option_label=value
    )
    result = await asks.answer_with_rule(conn, ask["id"], rule["id"])
    if expected is None:
        assert result is None
    else:
        assert result["answer_option"] == expected and result["answered_via"] == "rule"
        assert result["answer_text"] == 'Answered by the rule "Use the reviewed plan" of Ada.'
        assert (await rules.list_rules(conn, principal="person:ada"))[0]["uses"] == 1


async def test_rule_08_another_person_cannot_supply_rule(conn, ada, ben):
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Continue?",
        options=[{"id": "yes", "label": "Continue"}],
    )
    rule = await rules.create(
        conn, owner_person_id=ben["id"], text="Always continue", answer_option_label="Continue"
    )
    assert not await asks.answer_with_rule(conn, ask["id"], rule["id"])


async def test_rule_08_disabled_rule_never_answers(conn, ada):
    ask = await asks.create(
        conn,
        owner_person_id=ada["id"],
        question="Continue?",
        allow_rule=False,
        options=[{"id": "yes", "label": "Continue"}],
    )
    rule = await rules.create(
        conn, owner_person_id=ada["id"], text="Always continue", answer_option_label="Continue"
    )
    assert not await rules.match_ask(conn, ask)
    assert not await asks.answer_with_rule(conn, ask["id"], rule["id"])


async def test_rule_08_agent_token_cannot_answer_with_free_instructions(app, rpc):
    from illo.ledger import runs

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ask = await asks.create(
            conn,
            owner_person_id=ada["id"],
            question="Continue?",
            options=[{"id": "yes", "label": "Continue"}],
        )
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="agent_request",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    result = await rpc(
        token,
        "tools/call",
        {"name": "asks_answer", "arguments": {"id": ask["id"], "text": "Run another command"}},
    )
    assert result.json()["result"]["structuredContent"]["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        assert (await asks.get(conn, ask["id"], principal="person:ada"))["state"] == "open"
