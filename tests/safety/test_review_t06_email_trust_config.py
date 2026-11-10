"""Rule 7: email authority needs an explicit trust setting."""

import pytest
from pydantic import SecretStr

from illo.channels.email import setup
from illo.channels.email.parser import parse_message
from illo.db import one

RAW = (
    b"Authentication-Results: mx.example.com; dmarc=pass header.from=example.com\r\n"
    b"From: ada@example.com\r\nSubject: Request\r\n\r\nRead my lanes.\r\n"
)


@pytest.mark.parametrize("setting", [None, "", "  "])
def test_rule_07_blank_trust_refuses_sender(setting):
    assert not parse_message(RAW, authserv_id=setting).sender


def test_rule_07_explicit_topmost_supports_m365():
    raw = RAW.replace(b"mx.example.com; dmarc", b"spf=pass; dmarc")
    assert parse_message(raw, authserv_id="topmost").sender == "ada@example.com"
    assert not parse_message(raw, authserv_id="mx.example.com").sender


async def test_rule_07_blank_trust_stops_mailbox_timer(app, caplog):
    ctx = app.state.ctx
    ctx.config.email_host = "imap.example.com"
    ctx.config.email_user = "illo@example.com"
    ctx.config.email_password = SecretStr("unused")
    ctx.config.email_authserv_id = ""
    timers = []
    ctx.every = lambda seconds, fn: timers.append(fn)
    await setup(ctx)
    assert not timers
    assert "ILLO_EMAIL_AUTHSERV_ID" in caplog.text
    async with ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='email:auth'")
    assert sensor and sensor["last_error_at"]
