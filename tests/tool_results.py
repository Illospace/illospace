"""Decode the brain's single content channel in HTTP tests."""

import json


def read_result(envelope):
    value = json.loads(envelope["content"][0]["text"])
    assert envelope["structuredContent"] == {key: value[key] for key in ("decision", "reason")}
    return value
