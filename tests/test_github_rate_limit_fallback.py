"""Rate limits must stop token fallback across GitHub tool boundaries."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.contracts.github import GitHubConnectorError
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import github


_H = "brain.systems.runs.tool_catalog.handlers.github"
_C = "brain.systems.cortex.project_context.github"
_CANDIDATES = [
    {"token": "first", "source": "first", "key_name": "FIRST"},
    {"token": "second", "source": "second", "key_name": "SECOND"},
    {"token": "third", "source": "third", "key_name": "THIRD"},
]
_RELATION = {
    "parent_repo": "acme/widgets", "parent_issue_number": 1,
    "child_repo": "acme/widgets", "child_issue_number": 2,
}


@pytest.fixture(params=[403, 429])
def rate_limit(request):
    return GitHubConnectorError(
        request.param, "API rate limit exceeded", rate_limited=True,
        reset_at="2026-10-26T14:00:00+00:00", retry_after_seconds=60,
    )


@pytest.fixture
def candidates():
    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}._github_token_candidates", new=AsyncMock(return_value=_CANDIDATES),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,operation,kwargs", [
    (github._handle_read_github_source, "_read_with_token", {"repo": "acme/widgets"}),
    (github._handle_create_github_issue, "async_create_repo_issue", {"repo": "acme/widgets", "title": "Fix"}),
    (github._handle_create_github_pull_request, "async_reconcile_promotion_pull_request", {
        "repo": "uwear-ai/uwear-backend", "base": "main", "head": "staging",
        "title": "Staging → main promotion", "body": "Promote",
    }),
    (github._handle_add_github_issue_comment, "async_add_repo_issue_comment", {
        "repo": "acme/widgets", "issue_number": 1, "body": "Review",
    }),
    (github._handle_update_github_issue, "async_update_repo_issue", {
        "repo": "acme/widgets", "issue_number": 1, "state": "closed",
    }),
    (github._handle_add_github_sub_issue, "async_add_repo_sub_issue", _RELATION),
    (github._handle_remove_github_sub_issue, "async_remove_repo_sub_issue", _RELATION),
    (github._handle_list_github_sub_issues, "async_list_repo_sub_issues", {
        "repo": "acme/widgets", "issue_number": 1,
    }),
    (github._handle_list_github_sub_issues, "async_get_repo_issue_parent", {
        "repo": "acme/widgets", "issue_number": 1, "action": "get_parent",
    }),
    (github._handle_check_fix_deploy_state, "async_get_pull_request_deploy_info", {
        "repo": "acme/widgets", "pr_number": 1,
    }),
])
async def test_rate_limit_stops_tool_token_fallback(candidates, rate_limit, handler, operation, kwargs):
    with patch(f"{_H}.{operation}", new=AsyncMock(side_effect=rate_limit)) as call:
        payload = json.loads(await handler(**kwargs))
    call.assert_awaited_once()
    assert call.await_args.kwargs["token"] == "first"
    assert {key: payload[key] for key in rate_limit.as_payload()} == rate_limit.as_payload()
    assert not payload.get("no_write_token")
    assert not payload.get("missing_scope")
    assert "required_permission" not in payload
    assert not github._github_read_state().rejected


@pytest.mark.asyncio
async def test_rate_limit_after_unverified_parent_is_not_hidden_or_retried(candidates, rate_limit):
    with patch(f"{_H}.async_get_repo_issue_parent", new=AsyncMock(side_effect=[
        {"verified": False}, rate_limit,
    ])) as call:
        payload = json.loads(await github._handle_list_github_sub_issues(
            repo="acme/widgets", issue_number=1, action="get_parent",
        ))
    assert call.await_count == 2
    assert payload == {**rate_limit.as_payload(), "attempted_token_sources": ["first", "second"]}


@pytest.mark.asyncio
async def test_backend_issue_closure_stops_on_rate_limit(candidates, rate_limit):
    with patch(f"{_H}.async_get_issue_closure_info", new=AsyncMock(side_effect=rate_limit)) as call:
        with pytest.raises(GitHubConnectorError) as raised:
            await github.github_issue_closure_for_backend(repo_slug="acme/widgets", issue_number=1, org_id="o")
    call.assert_awaited_once()
    assert raised.value is rate_limit
    assert not github._github_read_state().rejected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [False, True])
async def test_deploy_compare_rate_limit_never_switches_tokens(candidates, rate_limit, backend):
    with patch("brain.systems.deploy_state_github.async_compare_commits", new=AsyncMock(side_effect=rate_limit)) as call:
        if backend:
            result = await github.github_deploy_states_for_backend({"fix": ("acme/widgets", "abc")}, org_id="o")
            failures = result.observations_by_key["fix"].failure_payloads()
            assert result["fix"] is None
            assert all(failure["rate_limited"] for failure in failures)
            assert all(failure["retry_after_seconds"] == 60 for failure in failures)
        else:
            payload = json.loads(await github._handle_check_fix_deploy_state(repo="acme/widgets", sha="abc"))
            assert payload["indeterminate"] is True
            assert {key: payload[key] for key in rate_limit.as_payload()} == rate_limit.as_payload()
    # The two branch comparisons share the first identity.
    assert call.await_count == 2
    assert {entry.kwargs["token"] for entry in call.await_args_list} == {"first"}


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"state": "closed"}, {"labels_remove": ["one", "two"]},
])
async def test_update_field_rate_limit_preserves_error_without_token_fallback(candidates, rate_limit, updates):
    with patch(f"{_C}._async_request", new=AsyncMock(side_effect=rate_limit)) as call:
        payload = json.loads(await github._handle_update_github_issue(repo="acme/widgets", issue_number=1, **updates))
    assert {entry.kwargs["token"] for entry in call.await_args_list} == {"first"}
    assert payload["status"] == "failed"
    assert not payload.get("no_write_token")
    for failure in payload["failed"].values():
        assert {key: failure[key] for key in rate_limit.as_payload()} == rate_limit.as_payload()


@pytest.mark.asyncio
async def test_label_rate_limit_after_auth_error_still_stops_fallback(candidates, rate_limit):
    with patch(f"{_C}._async_request", new=AsyncMock(side_effect=[
        GitHubConnectorError(403, "Forbidden"), rate_limit, {"number": 1, "labels": []},
    ])) as call:
        payload = json.loads(await github._handle_update_github_issue(
            repo="acme/widgets", issue_number=1, labels_remove=["one", "two"],
        ))
    assert call.await_count == 3
    assert {entry.kwargs["token"] for entry in call.await_args_list} == {"first"}
    failure = payload["failed"]["labels_remove"]
    assert failure["rate_limited"] is True
    assert failure["retry_after_seconds"] == 60
    assert failure["errors"] == [
        {"label": "one", "error": "Forbidden", "status_code": 403},
        {"label": "two", **rate_limit.as_payload()},
    ]


@pytest.mark.asyncio
async def test_provider_alert_rate_limit_keeps_claim_pending_without_fallback(candidates, rate_limit):
    with patch(f"{_H}.create_provider_alert_issue", new=AsyncMock(side_effect=rate_limit)) as call:
        payload = json.loads(await github._handle_create_github_issue(
            repo="acme/widgets", title="Provider failure",
            provider_alert={"service": "api", "subsystem": "image", "tracked_signature": "outage"},
        ))
    call.assert_awaited_once()
    assert payload["filing_pending"] is True
    assert payload["retryable"] is True
    assert {key: payload[key] for key in rate_limit.as_payload()} == rate_limit.as_payload()
