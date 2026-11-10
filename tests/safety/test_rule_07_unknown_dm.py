"""Rule 7: an unknown DM sender gets no authority and no answer."""

from illo.db import one


async def test_unknown_sender_cannot_claim_a_person(app, replay, channel, fake_brain):
    result = await replay(sender="U_UNKNOWN", payload={"principal": "person:ada"})
    assert result["decision"] == "ignore"
    assert not channel.working and not channel.final and not channel.errors
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 0
        assert (await one(conn, "SELECT principal FROM events"))["principal"] is None
