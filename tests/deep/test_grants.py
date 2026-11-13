"""Deep grants preserve reads and refuse writes in the required order."""

import pytest

from illo.tools.checks import scope
from illo.tools.registry import Registry


@pytest.fixture
def registry():
    registry = Registry()
    registry.discover()
    return registry


def run(**changes):
    return {
        "principal": "person:ada",
        "person_id": 1,
        "profile": "person_dm",
        "tainted": False,
        "personal_read": False,
        "web_read": False,
        "deep_kind": "request",
        **changes,
    }


def allowed(registry, name, **changes):
    return scope(run(**changes), registry.tools[name])


@pytest.mark.parametrize(
    "name",
    [
        "task_create",
        "task_post",
        "task_hand",
        "task_update",
        "task_done",
        "task_reopen",
        "task_cancel",
        "task_wake",
    ],
)
def test_task_writes_before_and_after_web(registry, name):
    assert allowed(registry, name).outcome == "allow"
    decision = allowed(registry, name, web_read=True)
    assert decision.outcome == "deny"
    assert (
        decision.reason
        == "This run read the web. Code refuses task writes after that. Put the proposal in your answer."
    )
    for kind in ("scout", "brief"):
        assert (
            allowed(registry, name, deep_kind=kind).reason
            == "A deep run only reads. Put the proposal in your answer."
        )


@pytest.mark.parametrize(
    "name",
    [
        "github_create_issue",
        "github_comment_issue",
        "github_assign_issue",
        "slack_send_dm",
        "slack_post_channel",
        "email_draft",
        "note_save",
        "list_save",
        "note_forget",
        "connect_start",
        "asks_create",
    ],
)
def test_deep_refuses_external_and_memory_writes(registry, name):
    assert (
        allowed(registry, name).reason == "A deep run only reads. Put the proposal in your answer."
    )


@pytest.mark.parametrize("name", ["shell", "web_fetch"])
def test_only_deep_gets_the_tools(registry, name):
    assert allowed(registry, name).outcome == "allow"
    assert allowed(registry, name, deep_kind=None).outcome == "deny"
    assert allowed(registry, name, tainted=True).outcome == "deny"


def test_no_nested_deep_and_no_web_after_private(registry):
    assert allowed(registry, "deep_start").outcome == "deny"
    assert allowed(registry, "deep_start", deep_kind=None).outcome == "allow"
    assert (
        allowed(registry, "web_fetch", personal_read=True).reason
        == "This run read private data. Code refuses the web after that."
    )
    assert allowed(registry, "web_fetch", deep_kind="brief").outcome == "deny"
    assert allowed(registry, "web_read_public_page").outcome == "deny"
    assert allowed(registry, "findings_list", deep_kind="scout").outcome == "allow"
    assert allowed(registry, "finding_add", deep_kind="scout").outcome == "allow"
    assert allowed(registry, "finding_add").outcome == "deny"
    for name in ("task_list", "task_get", "lists", "github_get_issue", "search_all", "shell"):
        assert allowed(registry, name, deep_kind="brief").outcome == "allow"
