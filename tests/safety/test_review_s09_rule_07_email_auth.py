"""Rule 7: sender authority needs one aligned result from the receiving server."""

import pytest

from illo.channels.email.parser import auth_results, parse_message


def parse(headers, authserv_id="mx.acme.example"):
    raw = (
        headers + "From: ada@example.com\r\nTo: illo@acme.example\r\n"
        "Subject: Do this\r\nMessage-ID: <forged@evil.test>\r\n\r\nDo this.\r\n"
    )
    return parse_message(raw.encode(), authserv_id=authserv_id)


def test_rule_07_semicolon_injection_is_not_authenticated():
    event = parse(
        "Authentication-Results: mx.acme.example;\r\n"
        ' spf=pass (mx.acme.example: domain of "x;dmarc=pass header.from=example.com"@evil.test'
        " designates 203.0.113.9 as permitted sender)"
        ' smtp.mailfrom="x;dmarc=pass header.from=example.com"@evil.test;\r\n'
        " dkim=none; dmarc=fail (p=REJECT) header.from=example.com\r\n"
    )
    assert not event.payload["authenticated"]
    assert not event.sender


@pytest.mark.parametrize(
    "result",
    [
        "spf=pass (outer (nested;dmarc=pass header.from=example.com)) smtp.mailfrom=evil.test; dmarc=fail header.from=example.com",
        'spf=pass smtp.mailfrom="x;dmarc=pass header.from=example.com"@evil.test; dmarc=fail header.from=example.com',
        "dmarc=pass header.from=example.com; dmarc=fail header.from=example.com",
        "dmarc=fail header.from=example.com; dmarc=pass header.from=example.com",
        "dmarc=pass header.from=example.com; dmarc=pass header.from=example.com",
        "spf=pass smtp.mailfrom=example.com; dkim=pass header.d=example.com",
        "dmarc=pass header.from=evil.test",
        "dmarc=pass",
        'dkim=pass reason=";dmarc=pass header.from=example.com"',
        "dmarc=pass header.from=example.com (unclosed",
        'dmarc=pass header.from=example.com reason="unclosed',
    ],
)
def test_rule_07_ambiguous_or_unaligned_results_are_not_authenticated(result):
    assert not parse(f"Authentication-Results: mx.acme.example; {result}\r\n").sender


@pytest.mark.parametrize(
    ("headers", "authserv_id", "trusted"),
    [
        ("", "mx.acme.example", False),
        (
            "Authentication-Results: other.example; dmarc=pass header.from=example.com\r\n",
            "mx.acme.example",
            False,
        ),
        (
            "Authentication-Results: mx.acme.example; dmarc=pass header.from=example.com\r\n",
            None,
            True,
        ),
        (
            "Authentication-Results: mx.acme.example; dmarc=pass header.from=example.com\r\n",
            "",
            True,
        ),
        (
            "Authentication-Results: MX.ACME.EXAMPLE; DMARC=PASS HEADER.FROM=EXAMPLE.COM\r\n",
            "mx.acme.example",
            True,
        ),
        (
            'Authentication-Results: mx.acme.example (outer (nested; text));\r\n\t dmarc=pass (ok) header.from="example.com"\r\n',
            "mx.acme.example",
            True,
        ),
        (
            "Authentication-Results: mx.acme.example; dmarc=fail header.from=example.com\r\nAuthentication-Results: mx.acme.example; dmarc=pass header.from=example.com\r\n",
            "mx.acme.example",
            False,
        ),
        (
            "Authentication-Results: receiver.example; dmarc=fail header.from=example.com\r\nAuthentication-Results: evil.test; dmarc=pass header.from=example.com\r\n",
            None,
            False,
        ),
        (
            "Authentication-Results: other.example; dmarc=fail header.from=example.com\r\nAuthentication-Results: mx.acme.example; dmarc=pass header.from=example.com\r\n",
            "mx.acme.example",
            True,
        ),
    ],
)
def test_rule_07_only_the_selected_receiving_header_is_trusted(headers, authserv_id, trusted):
    event = parse(headers, authserv_id)
    assert event.payload["authenticated"] is trusted
    assert bool(event.sender) is trusted


@pytest.mark.parametrize(
    "comment",
    ["(one; two)", "(outer (inner; text))", r"(escaped \) text)", '(a "quote; text")'],
)
def test_rule_07_pure_parser_removes_comments(comment):
    assert auth_results(f"mx.example {comment}; dmarc=pass header.from=example.com") == [
        ["mx.example"],
        ["dmarc", "=", "pass", "header.from", "=", "example.com"],
    ]


@pytest.mark.parametrize("quoted", ['"one; two"', r'"escaped \"; text"', '"(comment; text)"'])
def test_rule_07_pure_parser_keeps_quoted_tokens(quoted):
    assert auth_results(f"mx.example; spf=fail reason={quoted}") == [
        ["mx.example"],
        ["spf", "=", "fail", "reason", "=", quoted],
    ]


@pytest.mark.parametrize("header", ["mx.example; (open", 'mx.example; "open', "mx.example; )"])
def test_rule_07_pure_parser_rejects_incomplete_syntax(header):
    with pytest.raises(ValueError):
        auth_results(header)
