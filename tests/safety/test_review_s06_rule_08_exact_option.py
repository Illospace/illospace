"""Rule 8: match the approved option, never an id that resembles its label."""

import pytest

from illo.asks import actions, service
from illo.db import one
from illo.ledger import asks, people, rules


@pytest.mark.parametrize(
    "options,expected",
    [
        ([{"id": "Preview", "label": "Production"}, {"id": "x", "label": "Preview"}], "x"),
        ([{"id": "Preview", "label": "Production"}], None),
        ([{"id": "a", "label": "Preview"}, {"id": "b", "label": "Preview"}], None),
    ],
)
async def test_rule_08_legacy_label_must_be_exact_and_unique(conn, ada, options, expected):
    rule = await rules.create(
        conn, owner_person_id=ada["id"], text="Use preview", answer_option_label="Preview"
    )
    ask = await asks.create(
        conn, owner_person_id=ada["id"], question="Which target?", options=options
    )
    matched = await rules.match_ask(conn, ask)
    answered = await asks.answer_with_rule(conn, ask["id"], rule["id"])
    assert bool(matched) == (expected is not None)
    assert (answered["answer_option"] if answered else None) == expected


@pytest.mark.parametrize(
    "change", [{"id": "other"}, {"label": "Production"}, {"detail": "Drop the old tables"}, {}]
)
async def test_rule_08_card_rule_binds_id_label_and_detail(app, change):
    ctx = app.state.ctx
    option = {"id": "preview", "label": "Preview", "detail": "Use the preview data"}
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ask = await asks.create(
            conn,
            owner_person_id=ada["id"],
            question="Which target?",
            options=[option],
            recommended_option="preview",
        )
    await service.tick(ctx)
    async with ctx.pool.connection() as conn:
        card = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask["id"],))
    await actions.handle(
        ctx,
        ada,
        {
            "action_id": f"ask:{ask['id']}:always:preview",
            "channel": card["slack_channel"],
            "message_ts": card["slack_ts"],
        },
    )
    async with ctx.pool.connection() as conn:
        rule = (await rules.list_rules(conn, principal="person:ada"))[0]
        assert rule.get("answer_option_id") == "preview"
        assert rule.get("answer_option_detail") == option["detail"]
        next_ask = await asks.create(
            conn,
            owner_person_id=ada["id"],
            question="Which target?",
            options=[{**option, **change}],
        )
        assert bool(await rules.match_ask(conn, next_ask)) == (not change)
        assert bool(await asks.answer_with_rule(conn, next_ask["id"], rule["id"])) == (not change)
