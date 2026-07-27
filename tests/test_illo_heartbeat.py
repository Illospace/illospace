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

    assert payload == {
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

    assert payload["last_surface"] == "unknown"


@pytest.mark.asyncio
async def test_missing_project_binding_is_a_clean_skip(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=None))
    monkeypatch.setattr(illo_heartbeat, "publish_heartbeat", publish)

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result["ok"] is True
    assert result["outcome"] == "skipped"
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_project_token_is_a_clean_skip(monkeypatch):
    actor = illo_heartbeat.HeartbeatActor(user_id="user-1", org_id="org-1")
    publish = AsyncMock()
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(illo_heartbeat, "_heartbeat_token", AsyncMock(return_value=None))
    monkeypatch.setattr(illo_heartbeat, "publish_heartbeat", publish)

    result = await illo_heartbeat.run_heartbeat(now=NOW)

    assert result["ok"] is True
    assert result["outcome"] == "skipped"
    publish.assert_not_awaited()


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
    publish = AsyncMock()
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
        result["payload"],
        token="installation-token",
    )


@pytest.mark.asyncio
async def test_existing_orphan_branch_heartbeat_update_is_minimal(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.is_success = 200 <= status_code < 300

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.requests = []
            self.responses = [
                FakeResponse(200, {"object": {"sha": "head-sha"}}),
                FakeResponse(200, {"sha": "old-heartbeat-sha"}),
                FakeResponse(200, {"commit": {"sha": "new-commit-sha"}}),
            ]

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

    client = FakeClient()
    monkeypatch.setattr(
        illo_heartbeat,
        "async_http_client",
        lambda **_kwargs: FakeContext(client),
    )
    payload = {
        "ts": "2026-07-27T12:00:00Z",
        "last_run_id": 2718,
        "last_surface": "slack",
    }

    await illo_heartbeat.publish_heartbeat(payload, token="installation-token")

    method, url, kwargs = client.requests[-1]
    assert method == "PUT"
    assert url.endswith("/repos/Illospace/illospace/contents/heartbeat.json")
    assert kwargs["json"]["branch"] == "ops/heartbeat"
    assert kwargs["json"]["sha"] == "old-heartbeat-sha"
    decoded = base64.b64decode(kwargs["json"]["content"])
    assert json.loads(decoded) == payload
