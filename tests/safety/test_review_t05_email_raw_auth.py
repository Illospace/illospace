"""Rule 7: encoded words cannot create authentication results."""

import pytest

from illo.channels.email.parser import parse_message


@pytest.mark.parametrize(
    "result",
    [
        b"spf=pass (domain of =?utf-8?q?x=29=3B_dmarc=3Dpass_header=2Efrom=3Dexample=2Ecom_=28?=@evil.test); dkim=none",
        b"spf=pass smtp.mailfrom=evil.test;\r\n =?utf-8?q?dmarc=3Dpass_header=2Efrom=3Dexample=2Ecom?=",
    ],
)
def test_rule_07_encoded_authentication_result_is_refused(result):
    raw = (
        b"Authentication-Results: mx.acme.example; "
        + result
        + b"\r\nFrom: ada@example.com\r\nSubject: Request\r\n\r\nRead my lanes.\r\n"
    )
    event = parse_message(raw, authserv_id="mx.acme.example")
    assert not event.payload["authenticated"]
    assert not event.sender
