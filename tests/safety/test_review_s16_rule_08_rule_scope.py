"""Rule 8: Always keeps the source, repository, and requester fixed."""

import pytest

from illo.asks import actions, service
from illo.db import one
from illo.ledger import asks, lanes, people, rules, tokens


@pytest.mark.parametrize(
    "context,expected",
    [
        ("same", True),
        ("lane", False),
        ("repository", False),
        ("other-requester", False),
        ("no-requester", False),
    ],
)
async def test_rule_08_laneless_always_rule_has_explicit_scope(
    app, slack_client, context, expected
):
    ctx = app.state.ctx
    options = [{"id": "yes", "label": "Yes", "detail": "Use the proposed plan"}]
    async with ctx.pool.connection() as conn:
        ada, ben = await people.get(conn, "ada"), await people.get(conn, "ben")
        raw = await tokens.create(conn, ada["id"], "acme-agent")
        token = await tokens.verify(conn, raw)
        original = await asks.create(
            conn,
            owner_person_id=ada["id"],
            requester_token_id=token["id"],
            question="Proceed?",
            options=options,
            recommended_option="yes",
        )
    await service.tick(ctx)
    async with ctx.pool.connection() as conn:
        card = await one(conn, "SELECT * FROM asks WHERE id=%s", (original["id"],))
    await actions.handle(
        ctx,
        ada,
        {
            "action_id": f"ask:{card['id']}:always:yes",
            "channel": card["slack_channel"],
            "message_ts": card["slack_ts"],
        },
    )
    async with ctx.pool.connection() as conn:
        saved = (await rules.list_rules(conn, principal="person:ada"))[0]
        assert saved["match"]["lane_source"] is None
        assert saved["match"]["repository"] is None
        assert saved["match"]["requester_person_id"] == ada["id"]
        lane = None
        if context in {"lane", "repository"}:
            lane = await lanes.upsert(
                conn,
                source="codex",
                external_id="different",
                title="Acme work",
                owner_person_id=ada["id"],
                repo="acme-example/billing" if context == "repository" else None,
            )
        requester_token = token["id"]
        if context == "other-requester":
            raw = await tokens.create(conn, ben["id"], "acme-agent")
            requester_token = (await tokens.verify(conn, raw))["id"]
        elif context == "no-requester":
            requester_token = None
        ask = await asks.create(
            conn,
            owner_person_id=ada["id"],
            requester_token_id=requester_token,
            lane_id=lane["id"] if lane else None,
            question="Proceed?",
            options=options,
        )
        assert bool(await rules.match_ask(conn, ask)) == expected
        assert bool(await asks.answer_with_rule(conn, ask["id"], saved["id"])) == expected
    notice = next(
        c["text"] for c in slack_client.calls if c.get("text", "").startswith("Rule saved.")
    )
    assert "source: none" in notice and "repository: none" in notice and "requester: Ada" in notice
