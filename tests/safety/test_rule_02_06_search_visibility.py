"""Rules 2 and 6: search never exposes another person's private ledger text."""

import pytest

from illo.db import one
from illo.ledger import items, lanes, people, runs
from illo.sources import search


@pytest.mark.parametrize(
    ("principal", "tainted", "expected"),
    [
        ("person:ada", False, {"saved:team", "saved:ada"}),
        ("person:ben", False, {"saved:team", "saved:ben"}),
        ("team", False, {"saved:team"}),
        ("person:ada", True, {"saved:team"}),
    ],
)
async def test_rule_02_06_item_visibility(app, principal, tainted, expected):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        for key in ["ada", "ben"]:
            person = await people.get(conn, key)
            await items.upsert(
                conn,
                source_ref=f"saved:{key}",
                title="orchard",
                body=f"{key} secret",
                visibility="private",
                owner_person_id=person["id"],
            )
        await items.upsert(conn, source_ref="saved:team", title="orchard")
    hits = await search.search(ctx, principal, "orchard", tainted=tainted)
    assert {hit.source_ref for hit in hits} == expected
    assert all("ben secret" not in hit.snippet for hit in hits if principal != "person:ben")
    assert await search.get(ctx, "team", "saved:ada") is None
    assert await search.get(ctx, "person:ben", "saved:ada") is None


async def test_rule_02_06_lane_events_visibility(app, rpc):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        lane = await lanes.upsert(
            conn,
            source="manual",
            external_id="private",
            title="Private task",
            owner_person_id=ada["id"],
            visibility="private",
        )
        await lanes.add_event(conn, lane["id"], "update", "Secret orchard")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
            tainted=True,
        )
        token = await runs.mint_token(conn, run["id"], 60)
    assert not await search.search(ctx, "team", "orchard")
    assert not await search.search(ctx, "person:ben", "orchard")
    assert await search.get(ctx, "person:ben", f"lane:{lane['id']}") is None
    response = await rpc(
        token,
        "tools/call",
        {"name": "search_get", "arguments": {"source_ref": f"lane:{lane['id']}"}},
    )
    assert "Secret orchard" not in str(response.json())
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"


async def test_rule_03_private_get_sets_personal_read(app, rpc, rpc_token):
    ctx = app.state.ctx
    run, token = rpc_token
    async with ctx.pool.connection() as conn:
        await items.upsert(
            conn,
            source_ref="saved:mine",
            title="orchard",
            visibility="private",
            owner_person_id=run["person_id"],
        )
    await rpc(
        token, "tools/call", {"name": "search_get", "arguments": {"source_ref": "saved:mine"}}
    )
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT personal_read FROM runs WHERE id=%s", (run["id"],)))[
            "personal_read"
        ]
