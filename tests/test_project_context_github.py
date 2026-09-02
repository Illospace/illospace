from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from brain.contracts.github import GitHubConnectorError
from brain.systems.cortex.project_context.github import (
    _async_issue_timeline_or_none,
    _issue_payload,
    async_get_issue,
)
from brain.systems.knowledge.connectors.github import _resource_repositories


def _assigned_issue() -> dict[str, object]:
    return {
        "id": 1,
        "number": 42,
        "title": "Investigate assignment",
        "state": "open",
        "user": {"login": "illo-bot[bot]"},
        "assignees": [{"login": "owner"}],
        "created_at": "2026-01-01T00:00:00Z",
    }


def _assigned_event(actor: str, created_at: str) -> dict[str, object]:
    return {
        "event": "assigned",
        "actor": {"login": actor},
        "assignee": {"login": "owner"},
        "created_at": created_at,
    }


def test_resource_repositories_ignores_uploaded_file_uri():
    context = {"resources": [{"uri": "/static/uploads/x.pdf"}]}

    assert _resource_repositories(context) == []


def test_resource_repositories_keeps_github_urls_in_generic_resource_fields():
    context = {
        "resources": [
            {"uri": "https://github.com/owner/first"},
            {"url": "https://github.com/owner/second/tree/main"},
        ]
    }

    assert _resource_repositories(context) == ["owner/first", "owner/second"]


@pytest.mark.parametrize(
    "event_created_at",
    ["2026-01-01T00:00:03Z", "2026-01-01T00:00:21Z"],
)
def test_issue_payload_classifies_creator_assignment_at_filing_as_automation(event_created_at):
    payload = _issue_payload(
        _assigned_issue(),
        assignment_timeline=[_assigned_event("ILLO-BOT[bot]", event_created_at)],
    )

    assert payload["assignment_provenance"] == "automation_at_filing"


def test_issue_payload_classifies_immediate_assignment_by_different_actor_as_human():
    payload = _issue_payload(
        _assigned_issue(),
        assignment_timeline=[_assigned_event("human", "2026-01-01T00:00:01Z")],
    )

    assert payload["assignment_provenance"] == "human"


def test_issue_payload_classifies_late_creator_assignment_as_human():
    payload = _issue_payload(
        _assigned_issue(),
        assignment_timeline=[_assigned_event("illo-bot[bot]", "2026-01-01T00:10:00Z")],
    )

    assert payload["assignment_provenance"] == "human"


def test_issue_payload_classifies_mixed_automation_and_human_assignments_as_human():
    payload = _issue_payload(
        _assigned_issue(),
        assignment_timeline=[
            _assigned_event("illo-bot[bot]", "2026-01-01T00:00:03Z"),
            _assigned_event("human", "2026-01-01T00:05:00Z"),
        ],
    )

    assert payload["assignment_provenance"] == "human"


@pytest.mark.parametrize(
    "timeline",
    [[], [{"event": "labeled"}]],
    ids=["empty_timeline", "timeline_without_assignment"],
)
def test_issue_payload_classifies_assignees_without_an_assignment_event_as_unknown(timeline):
    # Absence of an assignment event is absence of evidence, not evidence of
    # automation. A truncated timeline page reaching this branch must not hide a
    # human-assigned ticket from consumers that filter on this field.
    payload = _issue_payload(_assigned_issue(), assignment_timeline=timeline)

    assert payload["assignment_provenance"] == "unknown"


def test_issue_payload_classifies_issue_without_assignees_as_none():
    issue = _assigned_issue()
    issue["assignees"] = []

    assert _issue_payload(issue)["assignment_provenance"] == "none"


@pytest.mark.asyncio
async def test_get_issue_leaves_assignment_timeline_fetch_disabled_by_default():
    with patch(
        "brain.systems.cortex.project_context.github._async_request",
        new=AsyncMock(return_value=_assigned_issue()),
    ) as request:
        result = await async_get_issue("acme/widgets", 42)

    assert result["issue"]["assignment_provenance"] == "unknown"
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_issue_fetches_timeline_when_assignment_provenance_is_enabled():
    request = AsyncMock(
        side_effect=[
            _assigned_issue(),
            [_assigned_event("illo-bot[bot]", "2026-01-01T00:00:03Z")],
        ]
    )
    with patch("brain.systems.cortex.project_context.github._async_request", new=request):
        result = await async_get_issue(
            "acme/widgets",
            42,
            include_assignment_provenance=True,
        )

    assert result["issue"]["assignment_provenance"] == "automation_at_filing"
    assert request.await_args_list[1].args[2] == "/repos/acme/widgets/issues/42/timeline"


@pytest.mark.asyncio
async def test_get_issue_timeline_failure_keeps_payload_and_reports_unknown():
    request = AsyncMock(
        side_effect=[
            _assigned_issue(),
            GitHubConnectorError(status_code=502, message="timeline unavailable"),
        ]
    )
    with patch("brain.systems.cortex.project_context.github._async_request", new=request):
        result = await async_get_issue(
            "acme/widgets",
            42,
            include_assignment_provenance=True,
        )

    assert result["issue"]["number"] == 42
    assert result["issue"]["assignment_provenance"] == "unknown"


@pytest.mark.asyncio
async def test_issue_timeline_or_none_only_swallows_github_connector_errors():
    connector_error = GitHubConnectorError(status_code=502, message="timeline unavailable")
    with patch(
        "brain.systems.cortex.project_context.github._async_issue_timeline",
        new=AsyncMock(side_effect=connector_error),
    ):
        result = await _async_issue_timeline_or_none(
            AsyncMock(),
            "acme/widgets",
            _assigned_issue(),
            token=None,
        )

    assert result is None

    unexpected_error = RuntimeError("unexpected defect")
    with patch(
        "brain.systems.cortex.project_context.github._async_issue_timeline",
        new=AsyncMock(side_effect=unexpected_error),
    ):
        with pytest.raises(RuntimeError, match="unexpected defect"):
            await _async_issue_timeline_or_none(
                AsyncMock(),
                "acme/widgets",
                _assigned_issue(),
                token=None,
            )
