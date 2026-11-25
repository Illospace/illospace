"""Rule 8: ask cards show the link host and the token's real owner."""

from illo.asks import service
from illo.asks.cards import links_text
from illo.board import coordinator, escalate
from tests.mcp.client import AgentClient


def test_rule_08_proof_link_uses_host_instead_of_agent_label():
    assert (
        links_text([{"url": "https://evil.example/login", "label": "Open the invoice"}])
        == "<https://evil.example/login|evil.example>"
    )


async def test_rule_08_token_ask_card_names_token_and_person(app, http, slack_client):
    app.state.ctx.workspace = app.state.ctx.workspace.model_copy(deep=True)
    app.state.ctx.workspace.config.board.coordinator.enabled = False
    agent = await AgentClient.create(app, http, person="ben", label="acme-laptop")
    await agent.call(
        "ask_create",
        {
            "owner": "ada",
            "question": "Can you review the invoice?",
            "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
            "proof": {
                "links": [{"url": "https://evil.example/login", "label": "Open the invoice"}]
            },
        },
    )
    await service.tick(app.state.ctx)
    card = next(c for c in slack_client.calls if c.get("method") == "chat_postMessage")
    text = str(card["blocks"])
    assert "Ben, through acme-laptop, needs a decision from you." in text
    assert "<https://evil.example/login|evil.example>" in text
    assert "Open the invoice" not in text


async def test_rule_08_board_coordinator_ask_card_names_token_and_person(app, http, slack_client):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.board.coordinator.quiet_hours = False
    agent = await AgentClient.create(app, http, person="ben", label="acme-laptop")
    await agent.call(
        "ask_create",
        {
            "owner": "ada",
            "question": "Can you review the invoice?",
            "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
            "proof": {
                "links": [{"url": "https://evil.example/login", "label": "Open the invoice"}]
            },
        },
    )
    # Trigger coordinator fallback delivery (verbatim card) and verify Rule 8 still holds.
    await coordinator.scan(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_episodes SET errors=2")
    await coordinator.run_coordinator(ctx)
    await escalate.send_due(ctx)
    card = next(c for c in slack_client.calls if c.get("method") == "chat_postMessage")
    text = str(card["blocks"])
    assert "Ben, through acme-laptop, needs a decision from you." in text
    assert "<https://evil.example/login|evil.example>" in text
    assert "Open the invoice" not in text
