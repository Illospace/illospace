"""Seed committed ask rows for channel tests."""

import pytest

from illo.ledger import asks, drafts, lanes, people, runs, tokens


@pytest.fixture
async def seed(app):
    ctx = app.state.ctx

    async def create(*, owner="ada", lane=None, draft=False, **changes):
        async with ctx.pool.connection() as conn:
            person = await people.get(conn, owner) if owner else None
            ada = await people.get(conn, "ada")
            token = await tokens.create(conn, ada["id"], "Acme agent")
            token_row = await tokens.verify(conn, token)
            if draft:
                run = await runs.create(
                    conn,
                    principal="person:ada",
                    person_id=ada["id"],
                    profile="person_dm",
                    runtime="fake",
                    model="fake",
                )
                return await drafts.create(
                    conn,
                    run_id=run["id"],
                    approver_person_id=person["id"],
                    kind=changes.pop("kind", "example"),
                    destination=changes.pop("destination", "outside:contact@example.com"),
                    content=changes.pop("content", {"text": "Hello, Acme"}),
                )
            lane_row = await lanes.upsert(
                conn,
                source="t3",
                external_id="lane-example",
                title="Acme example",
                owner_person_id=ada["id"],
                machine="acme-laptop",
                **(lane or {}),
            )
            values = {
                "owner_person_id": person["id"] if person else None,
                "lane_id": lane_row["id"],
                "requester_token_id": token_row["id"],
                "question": "Can the agent continue?",
                "options": [
                    {"id": "yes", "label": "Continue", "detail": "Use the proposed plan"},
                    {"id": "no", "label": "Wait"},
                ],
                "recommended_option": "yes",
                **changes,
            }
            return await asks.create(conn, **values)

    return create
