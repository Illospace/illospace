"""Rule 8: the collector accepts the actual server response for a rule answer."""

from illo.ledger import asks, rules
from tests.mcp.client import AgentClient
from tests.safety.test_rule_08_09_collectors import collector_case  # noqa: F401


async def test_rule_08_real_rule_answer_reaches_t3(app, http, collector_case):  # noqa: F811
    collector, api = collector_case
    api.wait()
    collector.poll()
    args = next(args for name, args in api.calls if name == "ask_create")
    agent = await AgentClient.create(app, http)
    created = await agent.call("ask_create", args)
    async with app.state.ctx.pool.connection() as conn:
        rule = await rules.create(
            conn,
            owner_person_id=agent.person_id,
            text="Run tests first.",
            answer_option_label=args["options"][0]["label"],
        )
        assert await asks.answer_with_rule(conn, created["ask_id"], rule["id"])
    api.answers = (await agent.call("ask_pull"))["answers"]
    assert api.answers[0]["answer_text"] is None
    assert api.answers[0]["answered_via"] == "rule"
    assert api.answers[0]["rule_text"] == "Run tests first."
    collector.poll()
    message = api.sent[0]["arguments"]["answers"]["question-a"]
    assert "Answer by rule" in message and message.endswith("Rule: Run tests first.")
    assert ("ask_ack", {"ask_id": created["ask_id"]}) in api.calls
    await agent.call("ask_ack", {"ask_id": created["ask_id"]})
    assert (await agent.call("ask_pull"))["answers"] == []
