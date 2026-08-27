from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_update_repo_issue,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.definitions.github import GITHUB_TOOLS
from brain.systems.runs.tool_catalog.handlers.github import _handle_update_github_issue


_C = "brain.systems.cortex.project_context.github"
_H = "brain.systems.runs.tool_catalog.handlers.github"


def _updated_issue(
    *,
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict:
    return {
        "id": 987,
        "node_id": "I_kwDOExample",
        "number": 369,
        "title": "Updated title",
        "body": "Updated body",
        "state": "closed",
        "html_url": "https://github.com/Illospace/illospace/issues/369",
        "assignees": [{"login": login, "id": index} for index, login in enumerate(assignees or [])],
        "labels": [
            {"name": label, "color": "123456"}
            for label in (["chantier"] if labels is None else labels)
        ],
    }


def test_update_github_issue_is_registered_with_all_supported_fields():
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    definition = next(tool for tool in GITHUB_TOOLS if tool["name"] == "update_github_issue")
    properties = definition["input_schema"]["properties"]

    assert "update_github_issue" in {tool["name"] for tool in COORDINATOR_TOOLS}
    assert "update_github_issue" in {tool["name"] for tool in WORKER_TOOLS}
    assert "update_github_issue" in _get_tool_handlers()
    assert definition["input_schema"]["required"] == ["repo", "issue_number"]
    assert {
        "assignees_add",
        "assignees_remove",
        "labels_add",
        "labels_remove",
        "labels_set",
        "state",
        "title",
        "body",
    } <= properties.keys()
    assert properties["state"]["enum"] == ["open", "closed"]

    registration = get_tool_registration("update_github_issue")
    assert registration is not None
    assert registration.permission == "write_workspace"
    assert registration.risk_class == "high"
    assert registration.reversibility == "reversible"
    assert registration.action_manifest is True


@pytest.mark.asyncio
async def test_connector_applies_each_field_separately_and_reads_back_issue():
    read_back = _updated_issue(assignees=["new-owner"])

    async def request(_client, method, path, **kwargs):
        if method == "GET":
            return read_back
        return {}

    with patch(f"{_C}._async_request", new=AsyncMock(side_effect=request)) as github_request:
        result = await async_update_repo_issue(
            "Illospace/illospace",
            369,
            assignees_add=["new-owner"],
            assignees_remove=["old-owner"],
            labels_add=["chantier"],
            labels_remove=["untriaged"],
            state="closed",
            title="Updated title",
            body="Updated body",
            token="installation-token",
        )

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["failed"] == {}
    assert result["applied"] == {
        "assignees_add": ["new-owner"],
        "assignees_remove": ["old-owner"],
        "labels_add": ["chantier"],
        "labels_remove": ["untriaged"],
        "state": "closed",
        "title": "Updated title",
        "body": "Updated body",
    }
    assert [user["login"] for user in result["issue"]["assignees"]] == ["new-owner"]

    calls = github_request.await_args_list
    assert [(call.args[1], call.args[2]) for call in calls] == [
        ("POST", "/repos/Illospace/illospace/issues/369/assignees"),
        ("DELETE", "/repos/Illospace/illospace/issues/369/assignees"),
        ("POST", "/repos/Illospace/illospace/issues/369/labels"),
        ("DELETE", "/repos/Illospace/illospace/issues/369/labels/untriaged"),
        ("PATCH", "/repos/Illospace/illospace/issues/369"),
        ("PATCH", "/repos/Illospace/illospace/issues/369"),
        ("PATCH", "/repos/Illospace/illospace/issues/369"),
        ("GET", "/repos/Illospace/illospace/issues/369"),
    ]
    assert all(call.kwargs["token"] == "installation-token" for call in calls)
    assert calls[0].kwargs["json"] == {"assignees": ["new-owner"]}
    assert calls[1].kwargs["json"] == {"assignees": ["old-owner"]}
    assert calls[2].kwargs["json"] == {"labels": ["chantier"]}
    assert calls[4].kwargs["json"] == {"state": "closed"}
    assert calls[5].kwargs["json"] == {"title": "Updated title"}
    assert calls[6].kwargs["json"] == {"body": "Updated body"}


@pytest.mark.asyncio
async def test_connector_reports_partial_failure_per_field_without_hiding_applied_assignee():
    read_back = _updated_issue(assignees=["new-owner"])

    async def request(_client, method, path, **kwargs):
        if path.endswith("/labels"):
            raise GitHubConnectorError(status_code=422, message="Label does not exist")
        if method == "GET":
            return read_back
        return {}

    with patch(f"{_C}._async_request", new=AsyncMock(side_effect=request)):
        result = await async_update_repo_issue(
            "Illospace/illospace",
            369,
            assignees_add=["new-owner"],
            labels_add=["not-a-real-label"],
            token="installation-token",
        )

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["status"] == "partial"
    assert result["applied"] == {"assignees_add": ["new-owner"]}
    assert result["fields"]["assignees_add"]["status"] == "applied"
    assert result["fields"]["labels_add"] == {
        "status": "failed",
        "requested": ["not-a-real-label"],
        "applied": [],
        "failed": ["not-a-real-label"],
        "status_code": 422,
        "error": "Label does not exist",
    }
    assert result["failed"]["labels_add"]["status_code"] == 422
    assert [user["login"] for user in result["issue"]["assignees"]] == ["new-owner"]


@pytest.mark.asyncio
async def test_connector_can_replace_all_labels_including_clearing_them():
    with patch(
        f"{_C}._async_request",
        new=AsyncMock(side_effect=[[], _updated_issue(labels=[])]),
    ) as github_request:
        result = await async_update_repo_issue(
            "Illospace/illospace",
            369,
            labels_set=[],
            token="installation-token",
        )

    assert result["applied"] == {"labels_set": []}
    assert github_request.await_args_list[0].args[1:3] == (
        "PUT",
        "/repos/Illospace/illospace/issues/369/labels",
    )
    assert github_request.await_args_list[0].kwargs["json"] == {"labels": []}


@pytest.mark.asyncio
async def test_connector_does_not_claim_applied_when_read_back_disagrees():
    with patch(
        f"{_C}._async_request",
        new=AsyncMock(side_effect=[{}, _updated_issue(assignees=[])]),
    ):
        result = await async_update_repo_issue(
            "Illospace/illospace",
            369,
            assignees_add=["new-owner"],
            token="installation-token",
        )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["applied"] == {}
    assert result["fields"]["assignees_add"]["verified"] is False
    assert result["failed"]["assignees_add"]["status_code"] == 502
    assert "read-back did not confirm" in result["failed"]["assignees_add"]["error"]


@pytest.mark.asyncio
async def test_handler_reuses_create_issue_vault_app_write_lane_and_surfaces_read_back():
    connector_result = {
        "repo": "Illospace/illospace",
        "issue_number": 369,
        "ok": True,
        "partial": False,
        "status": "applied",
        "fields": {
            "assignees_add": {
                "status": "applied",
                "requested": ["new-owner"],
                "applied": ["new-owner"],
            }
        },
        "applied": {"assignees_add": ["new-owner"]},
        "failed": {},
        "issue": {"number": 369, "assignees": [{"login": "new-owner"}]},
    }
    resolve = AsyncMock(return_value={"GITHUB_TOKEN": "minted-installation-token"})
    with bind_agent_context({"user_id": "u", "org_id": "o"}), patch(
        f"{_H}.async_resolve_project_bound_env_tokens",
        new=resolve,
    ), patch(
        f"{_H}.async_update_repo_issue",
        new=AsyncMock(return_value=connector_result),
    ) as update:
        payload = json.loads(await _handle_update_github_issue(
            repo="Illospace/illospace",
            issue_number=369,
            assignees_add=["new-owner"],
        ))

    assert payload["status"] == "applied"
    assert payload["issue"]["assignees"] == [{"login": "new-owner"}]
    assert payload["mutated_target_refs"] == [
        {"kind": "github_issue", "id": "Illospace/illospace#369"}
    ]
    assert payload["token_source"] == "project_binding:GITHUB_TOKEN"
    resolve.assert_awaited_once()
    assert resolve.await_args.kwargs == {
        "actor_user_id": "u",
        "org_id": "o",
        "project_slug": "Illospace/illospace",
        "project_slugs": None,
        "github_app_only": True,
    }
    update.assert_awaited_once()
    assert update.await_args.kwargs["token"] == "minted-installation-token"


@pytest.mark.asyncio
async def test_handler_preserves_honest_partial_result_from_github_client():
    async def request(_client, method, path, **kwargs):
        if path.endswith("/labels"):
            raise GitHubConnectorError(status_code=422, message="Label does not exist")
        if method == "GET":
            return _updated_issue(assignees=["new-owner"])
        return {}

    candidates = [{
        "key_name": None,
        "token": "minted-installation-token",
        "source": "project_binding:GITHUB_TOKEN",
    }]
    with patch(
        f"{_H}._github_token_candidates",
        new=AsyncMock(return_value=candidates),
    ), patch(
        f"{_C}._async_request",
        new=AsyncMock(side_effect=request),
    ):
        payload = json.loads(await _handle_update_github_issue(
            repo="Illospace/illospace",
            issue_number=369,
            assignees_add=["new-owner"],
            labels_add=["not-a-real-label"],
        ))

    assert payload["ok"] is False
    assert payload["partial"] is True
    assert payload["status"] == "partial"
    assert payload["applied"] == {"assignees_add": ["new-owner"]}
    assert payload["failed"]["labels_add"]["status_code"] == 422
    assert payload.get("no_write_token") is not True


@pytest.mark.asyncio
async def test_handler_rejects_ambiguous_label_modes_before_resolving_a_token():
    with patch(f"{_H}._github_token_candidates", new=AsyncMock()) as candidates:
        payload = json.loads(await _handle_update_github_issue(
            repo="Illospace/illospace",
            issue_number=369,
            labels_add=["bug"],
            labels_set=["triaged"],
        ))

    assert payload["status_code"] == 422
    assert "labels_set" in payload["error"]
    candidates.assert_not_awaited()
