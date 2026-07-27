"""Neutral Slack monitoring fixtures shared across intake policy suites."""

from __future__ import annotations

from typing import Any


def socket_mode_channel_message(**event_overrides):
    event = {
        "type": "message",
        "user": "U123",
        "text": "Payments API is throwing 500s in prod",
        "ts": "1716900000.000200",
        "event_ts": "1716900000.000200",
        "channel": "C_ALERTS",
        "channel_type": "channel",
    }
    event.update(event_overrides)
    return {
        "type": "events_api",
        "envelope_id": "env-2",
        "payload": {
            "team_id": "T789",
            "api_app_id": "A111",
            "event_id": "Ev333",
            "event_time": 1716900000,
            "event": event,
            "authorizations": [
                {"team_id": "T789", "user_id": "BILLO", "is_bot": True}
            ],
        },
    }


def channel_monitor_payload() -> dict[str, Any]:
    return {
        "origin": "slack.channel_message",
        "event_kind": "channel_message",
        "team_id": "T789",
        "channel_id": "C_ALERTS",
        "channel_name": "alerts",
        "channel_type": "channel",
        "message_ts": "1716900000.000200",
        "thread_ts": "1716900000.000200",
        "slack_user_id": "U123",
        "text": "Payments API 500s in prod",
        "response_target": {
            "channel_id": "C_ALERTS",
            "thread_ts": None,
            "visibility": "public",
        },
    }


class FakeSlackConnection:
    def __init__(
        self,
        metadata=None,
        org_id="org1",
        owner_user_id="user-reda",
    ):
        self.id = "conn1"
        self.org_id = org_id
        self.owner_user_id = owner_user_id
        self.agent_kind = "slack"
        self.transport = "slack_socket_mode"
        self.metadata_ = dict(metadata or {})


class FakeSlackSession:
    def __init__(self, connection):
        self._connection = connection

    async def get(self, _model, _id):
        return self._connection

    async def flush(self):
        return None


def patch_slack_connector(monkeypatch):
    from brain.systems.slack import connector as connector_module

    reactions: list[tuple[str, str, str]] = []
    submitted: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, token):
            self.token = token

        async def add_reaction(self, *, channel, timestamp, name):
            reactions.append((channel, timestamp, name))
            return {"ok": True}

    async def _fake_submit(
        session,
        *,
        connection,
        envelope,
        ingress_context,
    ):
        submitted.append(envelope)
        return {"status": "processed"}

    monkeypatch.setattr(connector_module, "SlackWebClient", _FakeClient)
    monkeypatch.setattr(
        connector_module,
        "submit_inbound_envelope",
        _fake_submit,
    )
    return connector_module, reactions, submitted


__all__ = [
    "FakeSlackConnection",
    "FakeSlackSession",
    "channel_monitor_payload",
    "patch_slack_connector",
    "socket_mode_channel_message",
]
