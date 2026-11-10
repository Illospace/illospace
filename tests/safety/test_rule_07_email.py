"""Rule 7: forged and unknown senders get no reply or team authority."""

import pytest

from illo.channels.email.parser import parse_message
from illo.db import one
from tests.email.test_email import raw_mail


@pytest.mark.parametrize(
    ("sender", "auth"),
    [
        ("ada@example.com", "dmarc=fail header.from=example.com"),
        ("customer@example.net", "dmarc=pass header.from=example.net"),
    ],
)
async def test_rule_07_unknown_or_forged_sender_has_no_answer(
    app, fake_brain, slack_client, sender, auth
):
    event = parse_message(raw_mail(sender=sender, auth=auth), authserv_id="mx.example.com")
    assert (await app.state.ctx.gateway.receive(event))["decision"] == "claimed"
    assert not fake_brain.requests and not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM events WHERE channel='email'")
        assert row["principal"] == "outsider" and row["tainted"]
