"""Rule 3: an ask without a lane stays private to its participants."""

import pytest

from illo.db import one
from illo.ledger import ask_workflow, asks, items, people, runs


@pytest.mark.parametrize(
    "principal,visible",
    [
        ("person:ada", True),
        ("person:ben", True),
        ("person:chloe", False),
        ("team", False),
        ("outsider", False),
    ],
)
async def test_rule_03_laneless_ask_is_visible_only_to_participants(
    conn, ada, ben, principal, visible
):
    ask = await asks.create(
        conn,
        owner_person_id=ben["id"],
        requester_person_id=ada["id"],
        question="Can you cover my medical leave?",
        proof={"text": "Surgery on Monday"},
    )
    assert bool(await asks.get(conn, ask["id"], principal=principal)) == visible
    assert bool(await asks.list_open(conn, principal=principal)) == visible


async def test_rule_03_laneless_ask_is_invisible_to_tainted_run(app, rpc):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        ask = await asks.create(
            conn,
            owner_person_id=ben["id"],
            requester_person_id=ada["id"],
            question="Can you cover my medical leave?",
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
    for tool, args in [("asks_get", {"id": ask["id"]}), ("asks_list", {})]:
        response = await rpc(token, "tools/call", {"name": tool, "arguments": args})
        result = response.json()["result"]["structuredContent"]
        assert result["decision"] == "deny" or not result["result"]


@pytest.mark.parametrize("requester", [True, False])
async def test_rule_03_laneless_decision_is_private_to_requester_or_owner(
    conn, ada, ben, requester
):
    ask = await asks.create(
        conn,
        owner_person_id=ben["id"],
        requester_person_id=ada["id"] if requester else None,
        question="Can you cover my medical leave?",
    )
    answer = await asks.answer(conn, ask["id"], person_id=ben["id"], text="Yes")
    item = await ask_workflow.decision(conn, answer)
    stored = await one(conn, "SELECT * FROM items WHERE id=%s", (item["id"],))
    assert stored["visibility"] == "private"
    assert stored["owner_person_id"] == (ada["id"] if requester else ben["id"])
    principal = "person:ada" if requester else "person:ben"
    assert await items.get(conn, item["source_ref"], principal=principal)
    for other in ["person:chloe", "team", "outsider"]:
        assert await items.get(conn, item["source_ref"], principal=other) is None
        assert await items.search(conn, "medical", principal=other) == []
