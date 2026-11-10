"""Rule 7: malformed and non-ASCII sender identities give no authority."""

import pytest

from illo.channels.email.parser import mailbox, parse_message


@pytest.mark.parametrize(
    "sender,domain",
    [
        ("ada@Kxample.com", "Kxample.com"),
        ("ada@Kxample.com", "kxample.com"),
        ("Kate@example.com", "example.com"),
        ("ada@example.com <eve@evil.test>", "example.com"),
    ],
)
def test_rule_07_invalid_from_refuses_authority(sender, domain):
    raw = (
        f"Authentication-Results: mx.example.com; dmarc=pass header.from={domain}\r\n"
        f"From: {sender}\r\nSubject: Request\r\n\r\nRead my lanes.\r\n"
    ).encode()
    event = parse_message(raw, authserv_id="mx.example.com")
    assert not event.sender
    assert not event.payload["authenticated"]


def test_rule_07_mailbox_refuses_unicode_before_lowercase():
    with pytest.raises(ValueError):
        mailbox("ada@Kxample.com")
