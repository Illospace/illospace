"""Decode the brain's single content channel in HTTP tests."""

import json


def read_result(envelope):
    value = json.loads(envelope["content"][0]["text"])
    metadata = envelope["structuredContent"]
    assert {key: metadata[key] for key in ("decision", "reason")} == {
        key: value[key] for key in ("decision", "reason")
    }
    assert set(metadata) <= {"decision", "reason", "run_flags"}
    if "run_flags" in metadata:
        assert set(metadata["run_flags"]) == {"customer_read", "personal_read"}
        assert all(type(v) is bool for v in metadata["run_flags"].values())
    return value


def snapshot_data(text):
    """Read the JSON inside a snapshot block and check both markers."""
    opening, body = text.split("\n", 1)
    assert opening in {'<outside_data source="snapshot">', '<outside_data source="team-snapshot">'}
    assert body.endswith("\n</outside_data>")
    return json.loads(body.removesuffix("\n</outside_data>"))
