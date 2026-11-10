import pytest

from illo.db import one
from illo.ledger import asks, rules
from tests.mcp.client import AgentClient

ASK = {
    "lane": {"source": "t3", "external_id": "acme-1"},
    "question": "Which test target should I use?",
    "options": [{"id": "preview", "label": "Preview"}, {"id": "local", "label": "Local"}],
    "recommended": "preview",
    "source_ref": "acme-1:question",
}


async def test_ask_creation_idempotency_and_cancel(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    again = await agent.call("ask_create", ASK)
    assert created["ask_id"] == again["ask_id"] and again["state"] == "open"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks")
        lane = await one(conn, "SELECT * FROM lanes")
        assert row["requester_token_id"] == agent.token_id
        assert row["requester_person_id"] == row["owner_person_id"] == agent.person_id
        assert lane["external_id"] == "acme-1" and lane["machine"] == "acme-laptop"
    result = await agent.call("ask_cancel", {"source_ref": ASK["source_ref"]})
    assert result["state"] == "cancelled"
    result = await agent.call("ask_cancel", {"ask_id": created["ask_id"]})
    assert result["state"] == "cancelled"


@pytest.mark.parametrize(
    "updates",
    [
        {"options": [{"id": "a", "label": "A"}]},
        {"options": [{"id": str(i), "label": str(i)} for i in range(6)]},
        {"recommended": "unknown"},
        {"question": "First question? Second question?"},
        {"question": ""},
        {"question": "x" * 241},
        {"options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
        {"options": [{"id": "a"}, {"id": "b", "label": "B"}]},
        {"due_at": "2026-11-10"},
        {"expires_at": "bad-date"},
    ],
)
async def test_ask_validation(app, http, updates):
    agent = await AgentClient.create(app, http)
    await agent.call("ask_create", {**ASK, **updates}, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM asks"))["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM lanes"))["n"] == 0


async def test_pull_redelivery_person_scope_and_ack(app, http):
    agent = await AgentClient.create(app, http)
    other_machine = await AgentClient.create(app, http, label="acme-desktop")
    same_machine = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    ask_id = created["ask_id"]
    async with app.state.ctx.pool.connection() as conn:
        await asks.answer(
            conn, ask_id, person_id=agent.person_id, option="preview", text="Use the preview."
        )
    assert (await other_machine.call("ask_pull"))["answers"] == []
    pulled = await same_machine.call("ask_pull")
    answer = pulled["answers"][0]
    assert (
        answer["answer_option"]["id"] == "preview" and answer["answer_option"]["label"] == "Preview"
    )
    assert answer["answered_by"] == "ada" and answer["answered_via"] == "person"
    assert answer["lane"] == ASK["lane"] and answer["source_ref"] == ASK["source_ref"]
    assert (await agent.call("ask_pull"))["answers"] == pulled["answers"]
    assert (await other_machine.call("ask_pull", {"scope": "person"}))["answers"] == pulled[
        "answers"
    ]
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT delivery_attempts FROM asks"))["delivery_attempts"] == 3
    ack = await other_machine.call("ask_ack", {"ask_id": ask_id})
    assert ack == {"ask_id": ask_id, "delivery_state": "acked"}
    assert await other_machine.call("ask_ack", {"ask_id": ask_id}) == ack
    assert (await agent.call("ask_pull"))["answers"] == []
    assert (await agent.call("ask_cancel", {"ask_id": ask_id}))["state"] == "answered"
    replay = await agent.call("ask_create", ASK)
    assert replay["state"] == "answered" and replay["answer_option"]["id"] == "preview"


async def test_rule_answer_has_its_rule_text(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    async with app.state.ctx.pool.connection() as conn:
        rule = await rules.create(
            conn,
            owner_person_id=agent.person_id,
            text="Use the preview for test checks.",
            answer_option_label="Preview",
            match={"lane_source": "t3"},
        )
        await asks.answer_with_rule(conn, created["ask_id"], rule["id"])
    result = await agent.call("ask_pull")
    assert result["answers"][0]["rule_text"] == rule["text"]
    assert result["answers"][0]["answered_via"] == "rule"


async def test_expired_answers_do_not_redeliver(app, http):
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", ASK)
    async with app.state.ctx.pool.connection() as conn:
        await asks.answer(conn, created["ask_id"], person_id=agent.person_id, option="preview")
        await conn.execute("UPDATE asks SET expires_at=now()-interval '1 minute'")
    assert (await agent.call("ask_pull"))["answers"] == []
