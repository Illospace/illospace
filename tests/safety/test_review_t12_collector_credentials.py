"""Rule 9: collectors remove common credentials before a report leaves the machine."""

import pytest

from collectors.common import clean


@pytest.mark.parametrize(
    "token",
    [
        "xapp-1-" + "a" * 40,
        "xoxe.xoxp-1-" + "b" * 40,
        "xoxe-" + "c" * 40,
        "xoxa-" + "d" * 40,
        "sk_live_" + "e" * 24,
        "rk_test_" + "f" * 24,
        "glpat-" + "g" * 20,
        "npm_" + "h" * 36,
    ],
)
def test_rule_09_common_token_is_fully_redacted(token):
    assert clean("Before " + token + " after") == "Before [redacted] after"


@pytest.mark.parametrize(
    "assignment",
    [
        "aws_secret_access_key={key}",
        "AWS_SECRET_ACCESS_KEY = '{key}'",
        '"aws_secret_access_key": "{key}"',
    ],
)
def test_rule_09_aws_secret_assignment_is_redacted(assignment):
    secret = "aB0/" * 9 + "cD0+"
    assert len(secret) == 40
    assert secret not in clean(assignment.format(key=secret))
