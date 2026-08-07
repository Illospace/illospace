"""Tests for the scheduler-launched external heartbeat emitter."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.jobs.pipelines import illo_heartbeat


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.requests = []
        self.responses = list(responses)

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class FakeContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        return False


def _public_payload() -> illo_heartbeat.HeartbeatPayload:
    return illo_heartbeat.HeartbeatPayload(
        ts="2026-07-27T12:00:00Z",
        last_run_id=2718,
        last_surface="slack",
    )


def _fake_client(monkeypatch, responses) -> FakeClient:
    client = FakeClient(responses)
    monkeypatch.setattr(
        illo_heartbeat,
        "async_http_client",
        lambda **_kwargs: FakeContext(client),
    )
    return client


def test_public_payload_has_only_timestamp_run_id_and_coarse_surface():
    run = SimpleNamespace(
        id=2718,
        metadata_={
            "originating_surface": "slack",
            "customer": "must-not-leak",
            "error_body": "must-not-leak",
        },
        target_ref={"thread_contents": "must-not-leak"},
    )

    payload = illo_heartbeat.build_heartbeat_payload(run, now=NOW)

    assert payload.as_public_dict() == {
        "ts": "2026-07-27T12:00:00Z",
        "last_run_id": 2718,
        "last_surface": "slack",
    }


def test_public_payload_rejects_unknown_surface_text():
    run = SimpleNamespace(
        id=2719,
        metadata_={"originating_surface": "customer-42-secret-project"},
        target_ref={},
    )

    payload = illo_heartbeat.build_heartbeat_payload(run, now=NOW)

    assert payload.last_surface == "unknown"


@pytest.mark.asyncio
async def test_publish_boundary_rejects_untyped_payload():
    with pytest.raises(TypeError, match="HeartbeatPayload"):
        await illo_heartbeat.publish_heartbeat(  # type: ignore[arg-type]
            {
                "ts": "2026-07-27T12:00:00Z",
                "last_run_id": 2718,
                "last_surface": "slack",
                "secret": "must-not-cross-write-boundary",
            },
            token="installation-token",
        )


@pytest.mark.asyncio
async def test_missing_project_binding_is_an_explicit_configuration_skip(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=None))
    monkeypatch.setattr(illo_heartbeat, "publish_heartbeat", publish)

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result["ok"] is False
    assert result["outcome"] == "skipped"
    assert result["skip_kind"] == "configuration"
    assert result["reason"] == (
        "No GitHub App project binding is configured for illospace/illospace"
    )
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_project_token_is_an_explicit_configuration_skip(monkeypatch):
    actor = illo_heartbeat.HeartbeatActor(user_id="user-1", org_id="org-1")
    publish = AsyncMock()
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_token", AsyncMock(return_value=None))
    monkeypatch.setattr(illo_heartbeat, "publish_heartbeat", publish)

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result["ok"] is False
    assert result["outcome"] == "skipped"
    assert result["skip_kind"] == "configuration"
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_configuration_skip_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        illo_heartbeat,
        "run_heartbeat",
        AsyncMock(
            return_value={
                "job": "illo_external_heartbeat",
                "ok": False,
                "outcome": "skipped",
                "skip_kind": "configuration",
                "reason": "binding missing",
            }
        ),
    )

    assert await illo_heartbeat.async_main() == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "binding missing"


@pytest.mark.asyncio
async def test_emitter_requests_existing_app_token_with_contents_write(monkeypatch):
    resolver = AsyncMock(return_value={"GITHUB_TOKEN": "installation-token"})
    monkeypatch.setattr(
        illo_heartbeat,
        "async_resolve_project_bound_env_tokens",
        resolver,
    )
    actor = illo_heartbeat.HeartbeatActor(user_id="user-1", org_id="org-1")

    token = await illo_heartbeat._heartbeat_token(actor)

    assert token == "installation-token"
    resolver.assert_awaited_once_with(
        actor_user_id="user-1",
        org_id="org-1",
        project_slug="illospace/illospace",
        github_app_only=True,
        github_app_permissions={"contents": "write"},
    )


@pytest.mark.asyncio
async def test_published_result_exposes_only_the_minimal_payload(monkeypatch):
    actor = illo_heartbeat.HeartbeatActor(user_id="user-1", org_id="org-1")
    latest_run = SimpleNamespace(
        id=2718,
        metadata_={"originating_surface": "slack", "secret": "do-not-publish"},
        target_ref={},
    )
    publish = AsyncMock(
        return_value=illo_heartbeat.HeartbeatPublishResult(
            illo_heartbeat.HeartbeatPublishOutcome.PUBLISHED,
            attempts=1,
        )
    )
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        illo_heartbeat,
        "_heartbeat_token",
        AsyncMock(return_value="installation-token"),
    )
    monkeypatch.setattr(illo_heartbeat, "_latest_run", AsyncMock(return_value=latest_run))
    monkeypatch.setattr(illo_heartbeat, "publish_heartbeat", publish)

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result == {
        "job": "illo_external_heartbeat",
        "ok": True,
        "outcome": "published",
        "payload": {
            "ts": "2026-07-27T12:00:00Z",
            "last_run_id": 2718,
            "last_surface": "slack",
        },
    }
    publish.assert_awaited_once_with(
        _public_payload(),
        token="installation-token",
    )


@pytest.mark.asyncio
async def test_existing_orphan_branch_heartbeat_update_is_minimal(monkeypatch):
    client = _fake_client(
        monkeypatch,
        [
            FakeResponse(200, {"object": {"sha": "head-sha"}}),
            FakeResponse(200, {"sha": "old-heartbeat-sha"}),
            FakeResponse(200, {"commit": {"sha": "new-commit-sha"}}),
        ],
    )
    payload = _public_payload()

    result = await illo_heartbeat.publish_heartbeat(
        payload,
        token="installation-token",
    )

    assert result == illo_heartbeat.HeartbeatPublishResult(
        illo_heartbeat.HeartbeatPublishOutcome.PUBLISHED,
        attempts=1,
    )
    method, url, kwargs = client.requests[-1]
    assert method == "PUT"
    assert url.endswith("/repos/Illospace/illospace/contents/heartbeat.json")
    assert kwargs["json"]["branch"] == "ops/heartbeat"
    assert kwargs["json"]["sha"] == "old-heartbeat-sha"
    decoded = base64.b64decode(kwargs["json"]["content"])
    assert json.loads(decoded) == payload.as_public_dict()


@pytest.mark.asyncio
async def test_initial_publish_creates_orphan_branch(monkeypatch):
    client = _fake_client(
        monkeypatch,
        [
            FakeResponse(404, {}),
            FakeResponse(201, {"sha": "blob-sha"}),
            FakeResponse(201, {"sha": "tree-sha"}),
            FakeResponse(201, {"sha": "commit-sha"}),
            FakeResponse(201, {"ref": "refs/heads/ops/heartbeat"}),
        ],
    )

    result = await illo_heartbeat.publish_heartbeat(
        _public_payload(),
        token="installation-token",
    )

    assert result == illo_heartbeat.HeartbeatPublishResult(
        illo_heartbeat.HeartbeatPublishOutcome.PUBLISHED,
        attempts=1,
    )
    assert [request[0] for request in client.requests] == [
        "GET",
        "POST",
        "POST",
        "POST",
        "POST",
    ]
    assert client.requests[-1][1].endswith("/repos/Illospace/illospace/git/refs")
    assert client.requests[-1][2]["json"] == {
        "ref": "refs/heads/ops/heartbeat",
        "sha": "commit-sha",
    }
    blob_content = base64.b64decode(client.requests[1][2]["json"]["content"])
    assert json.loads(blob_content) == _public_payload().as_public_dict()


@pytest.mark.asyncio
async def test_branch_creation_422_race_retries_as_existing_branch(monkeypatch):
    client = _fake_client(
        monkeypatch,
        [
            FakeResponse(404, {}),
            FakeResponse(201, {"sha": "blob-sha"}),
            FakeResponse(201, {"sha": "tree-sha"}),
            FakeResponse(201, {"sha": "commit-sha"}),
            FakeResponse(422, {}),
            FakeResponse(200, {"object": {"sha": "racing-head-sha"}}),
            FakeResponse(200, {"sha": "racing-heartbeat-sha"}),
            FakeResponse(200, {"commit": {"sha": "updated-commit-sha"}}),
        ],
    )

    result = await illo_heartbeat.publish_heartbeat(
        _public_payload(),
        token="installation-token",
    )

    assert result == illo_heartbeat.HeartbeatPublishResult(
        illo_heartbeat.HeartbeatPublishOutcome.PUBLISHED,
        attempts=2,
    )
    assert client.requests[-1][0] == "PUT"
    assert client.requests[-1][2]["json"]["sha"] == "racing-heartbeat-sha"


@pytest.mark.asyncio
async def test_recoverable_409_reloads_sha_and_publishes(monkeypatch):
    client = _fake_client(
        monkeypatch,
        [
            FakeResponse(200, {"object": {"sha": "head-1"}}),
            FakeResponse(200, {"sha": "heartbeat-1"}),
            FakeResponse(409, {}),
            FakeResponse(200, {"object": {"sha": "head-2"}}),
            FakeResponse(200, {"sha": "heartbeat-2"}),
            FakeResponse(200, {"commit": {"sha": "commit-2"}}),
        ],
    )

    result = await illo_heartbeat.publish_heartbeat(
        _public_payload(),
        token="installation-token",
    )

    assert result == illo_heartbeat.HeartbeatPublishResult(
        illo_heartbeat.HeartbeatPublishOutcome.PUBLISHED,
        attempts=2,
    )
    put_bodies = [
        request[2]["json"] for request in client.requests if request[0] == "PUT"
    ]
    assert [body["sha"] for body in put_bodies] == ["heartbeat-1", "heartbeat-2"]


@pytest.mark.asyncio
async def test_exhausted_409_retries_return_typed_conflict_skip(monkeypatch):
    responses = []
    for attempt in range(3):
        responses.extend(
            [
                FakeResponse(200, {"object": {"sha": f"head-{attempt}"}}),
                FakeResponse(200, {"sha": f"heartbeat-{attempt}"}),
                FakeResponse(409, {}),
            ]
        )
    client = _fake_client(monkeypatch, responses)

    result = await illo_heartbeat.publish_heartbeat(
        _public_payload(),
        token="installation-token",
    )

    assert result == illo_heartbeat.HeartbeatPublishResult(
        illo_heartbeat.HeartbeatPublishOutcome.CONFLICT_SKIPPED,
        attempts=3,
        conflict_status=409,
    )
    assert not client.responses


@pytest.mark.asyncio
async def test_conflict_skip_is_a_successful_job_outcome(monkeypatch):
    actor = illo_heartbeat.HeartbeatActor(user_id="user-1", org_id="org-1")
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        illo_heartbeat,
        "_heartbeat_token",
        AsyncMock(return_value="installation-token"),
    )
    monkeypatch.setattr(illo_heartbeat, "_latest_run", AsyncMock(return_value=None))
    monkeypatch.setattr(
        illo_heartbeat,
        "publish_heartbeat",
        AsyncMock(
            return_value=illo_heartbeat.HeartbeatPublishResult(
                illo_heartbeat.HeartbeatPublishOutcome.CONFLICT_SKIPPED,
                attempts=3,
                conflict_status=409,
            )
        ),
    )

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result == {
        "job": "illo_external_heartbeat",
        "ok": True,
        "outcome": "skipped",
        "skip_kind": "transient",
        "reason": "GitHub compare-and-swap conflicts exhausted",
        "attempts": 3,
        "conflict_status": 409,
    }
