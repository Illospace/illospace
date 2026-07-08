"""Tests for passive Slack channel monitoring (Route A).

Covers the full path: ingress admission of monitored-channel messages, the 👀
reflex reaction, headless triage run framing (no forced reply), and the
``manage_slack`` self-configuration surface for monitored channels.
"""

from __future__ import annotations

from typing import Any

import pytest


def _socket_mode_channel_message(**event_overrides):
    """A Socket Mode ``message`` event posted to a channel (no @-mention)."""

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
            "authorizations": [{"team_id": "T789", "user_id": "BILLO", "is_bot": True}],
        },
    }


def _channel_monitor_payload() -> dict[str, Any]:
    """A normalized Slack payload for a monitored-channel message."""

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
        "response_target": {"channel_id": "C_ALERTS", "thread_ts": None, "visibility": "public"},
    }


# --------------------------------------------------------------------------- #
# Ingress: which channel messages become actionable                            #
# --------------------------------------------------------------------------- #


def test_monitored_channel_plain_message_admitted_as_channel_message():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.channel_message"
    assert envelope["payload"]["event_kind"] == "channel_message"
    assert envelope["payload"]["channel_id"] == "C_ALERTS"
    assert envelope["payload"]["surface"] == "slack_channel"
    assert envelope["idempotency_key"] == "slack:T789:C_ALERTS:1716900000.000200"


def test_monitored_channel_reply_threads_under_alert():
    """A monitored-channel triage reply must thread UNDER the original alert.

    Regression: monitored alerts are top-level (thread_ts == message_ts). The
    default reply-target logic drops thread_ts for top-level messages (so Illo
    does not force threads on plain mentions), which sent Illo's monitored-channel
    reply as a detached top-level message instead of a threaded reply under the
    alert. The monitored-channel origin is the deliberate exception.
    """
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.channel_message"
    # Threads under the alert's own ts, in both mirrors of response_target.
    assert envelope["hints"]["response_target"]["thread_ts"] == "1716900000.000200"
    assert envelope["payload"]["response_target"]["thread_ts"] == "1716900000.000200"


def test_monitored_channel_reply_threads_under_existing_parent_thread():
    """If the monitored alert is itself already threaded, reply under its parent."""
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(thread_ts="1716899999.000001"),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["hints"]["response_target"]["thread_ts"] == "1716899999.000001"


def test_unmonitored_channel_plain_message_is_ignored():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    assert (
        normalize_slack_socket_event(
            _socket_mode_channel_message(),
            bot_user_id="BILLO",
            monitored_channels=set(),
        )
        is None
    )
    assert (
        normalize_slack_socket_event(
            _socket_mode_channel_message(),
            bot_user_id="BILLO",
            monitored_channels={"C_OTHER"},
        )
        is None
    )


def test_monitored_channel_ignores_illo_own_message_by_user():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    assert (
        normalize_slack_socket_event(
            _socket_mode_channel_message(user="BILLO"),
            bot_user_id="BILLO",
            monitored_channels={"C_ALERTS"},
        )
        is None
    )


def test_monitored_channel_ignores_illo_own_app_message():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    # A message authored by Illo's own Slack app (event.app_id == payload.api_app_id).
    assert (
        normalize_slack_socket_event(
            _socket_mode_channel_message(user="", bot_id="B_ILLO", app_id="A111"),
            bot_user_id="BILLO",
            monitored_channels={"C_ALERTS"},
        )
        is None
    )


def test_monitored_channel_admits_third_party_bot_alert():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(
            user="",
            bot_id="B_SENTRY",
            app_id="A_SENTRY",
            text="New Sentry issue: TypeError in checkout",
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.channel_message"


def test_monitored_channel_mention_still_routes_as_mention():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(text="<@BILLO> please take a look"),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.app_mention"


def test_monitored_channel_ignores_edit_subtype():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    assert (
        normalize_slack_socket_event(
            _socket_mode_channel_message(subtype="message_changed"),
            bot_user_id="BILLO",
            monitored_channels={"C_ALERTS"},
        )
        is None
    )


def test_dm_is_never_treated_as_channel_message():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(channel="D1", channel_type="im", text="hi"),
        bot_user_id="BILLO",
        monitored_channels={"D1"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.direct_message"


# --------------------------------------------------------------------------- #
# Trigger shaping: monitored messages become headless triage runs              #
# --------------------------------------------------------------------------- #


def test_slack_event_type_detects_channel_message():
    from brain.systems.slack.triggers import slack_event_type

    assert slack_event_type({"origin": "slack.channel_message"}) == "slack.channel_message"
    assert slack_event_type({"event_kind": "channel_message"}) == "slack.channel_message"


def test_build_payload_for_channel_message_is_headless_and_not_forced_reply():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
        inbound_event_id="evt1",
        connection_id="conn1",
        idempotency_key="idem1",
    )

    assert payload["event_type"] == "slack.channel_message"
    metadata = payload["payload"]["metadata"]
    assert metadata["slack_monitor"] is True
    assert metadata["headless"] is True
    assert metadata["final_answer_target_surface"] == "headless"
    assert "required_response_tool" not in metadata
    run_message = payload["payload"]["run_message"]
    assert "monitoring Slack #alerts" in run_message
    assert "👀" in run_message


def test_build_payload_for_mention_still_forces_reply():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload={
            "origin": "slack.app_mention",
            "event_kind": "mention",
            "team_id": "T789",
            "channel_id": "C1",
            "channel_type": "channel",
            "message_ts": "1716900000.000100",
            "thread_ts": "1716900000.000100",
            "slack_user_id": "U1",
            "text": "<@BILLO> hi",
            "response_target": {"channel_id": "C1", "thread_ts": None, "visibility": "public"},
        },
    )

    metadata = payload["payload"]["metadata"]
    assert metadata["required_response_tool"] == "post_slack_reply"
    assert metadata["final_answer_target_surface"] == "slack"
    assert "headless" not in metadata
    # Origin-scoped: the monitored-channel threading exception must NOT change
    # mention behavior — a top-level mention still replies top-level (no thread).
    assert metadata["slack_trigger"]["response_target"]["thread_ts"] is None


def test_agent_run_request_for_monitor_is_headless_without_forced_tool():
    from brain.systems.runs.work_intake_slack import agent_run_request_for_slack
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    trigger_payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
        inbound_event_id="evt1",
        connection_id="conn1",
        idempotency_key="idem1",
    )

    request = agent_run_request_for_slack(trigger_payload)

    assert request.metadata.get("headless") is True
    assert request.metadata.get("slack_monitor") is True
    assert not request.metadata.get("required_response_tool")
    assert request.metadata.get("final_answer_target_surface") == "headless"
    assert request.target_ref.get("headless") is True
    assert request.target_ref.get("slack_trigger", {}).get("channel_id") == "C_ALERTS"


def test_channel_monitor_reply_target_threads_under_alert():
    """The trigger built for a monitored message carries a thread anchor so an
    explicit post_slack_reply (the only reply path in a headless monitor run)
    threads under the alert rather than posting a detached channel message.
    """
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
        inbound_event_id="evt1",
        connection_id="conn1",
        idempotency_key="idem1",
    )

    slack_trigger = payload["payload"]["metadata"]["slack_trigger"]
    assert slack_trigger["response_target"]["thread_ts"] == "1716900000.000200"


# --------------------------------------------------------------------------- #
# Client: the 👀 reaction call                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_reaction_posts_normalized_eyes(monkeypatch):
    from brain.systems.slack import client as slack_client_module
    from brain.systems.slack.client import SlackWebClient

    calls: list[tuple[str, dict[str, Any]]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"ok": True}

    class _HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def post(self, url: str, *, headers, json=None):
            calls.append((url, json))
            return _Response()

    monkeypatch.setattr(slack_client_module, "async_http_client", lambda **_kwargs: _HttpClient())

    result = await SlackWebClient("xoxb-test").add_reaction(
        channel="C_ALERTS", timestamp="1716900000.000200", name=":eyes:"
    )

    assert result["ok"] is True
    assert calls == [
        (
            "https://slack.com/api/reactions.add",
            {"channel": "C_ALERTS", "timestamp": "1716900000.000200", "name": "eyes"},
        )
    ]


# --------------------------------------------------------------------------- #
# Connector: the reflex reaction fires only for monitored channel messages      #
# --------------------------------------------------------------------------- #


class _FakeConnection:
    def __init__(self, metadata=None, org_id="org1"):
        self.id = "conn1"
        self.org_id = org_id
        self.agent_kind = "slack"
        self.transport = "slack_socket_mode"
        self.metadata_ = dict(metadata or {})


class _FakeSession:
    def __init__(self, connection):
        self._connection = connection

    async def get(self, _model, _id):
        return self._connection

    async def flush(self):
        return None


def _patch_connector(monkeypatch):
    from brain.systems.slack import connector as connector_module

    reactions: list[tuple[str, str, str]] = []
    submitted: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, token):
            self.token = token

        async def add_reaction(self, *, channel, timestamp, name):
            reactions.append((channel, timestamp, name))
            return {"ok": True}

    async def _fake_submit(session, *, connection, envelope, ingress_context):
        submitted.append(envelope)
        return {"status": "processed"}

    monkeypatch.setattr(connector_module, "SlackWebClient", _FakeClient)
    monkeypatch.setattr(connector_module, "submit_inbound_envelope", _fake_submit)
    return connector_module, reactions, submitted


@pytest.mark.asyncio
async def test_process_socket_payload_reacts_to_monitored_message(monkeypatch):
    connector_module, reactions, submitted = _patch_connector(monkeypatch)
    connection = _FakeConnection(metadata={"slack": {"monitored_channels": ["C_ALERTS"]}})
    config = connector_module.SlackConnectorConfig(
        bot_token="xoxb-x", app_token="xapp-x", bot_user_id="BILLO"
    )

    result = await connector_module.process_socket_payload(
        None,
        connection=connection,
        socket_payload=_socket_mode_channel_message(),
        config=config,
    )

    assert result["ignored"] is False
    assert reactions == [("C_ALERTS", "1716900000.000200", "eyes")]
    assert submitted and submitted[0]["origin"] == "slack.channel_message"


@pytest.mark.asyncio
async def test_process_socket_payload_does_not_react_to_mention(monkeypatch):
    connector_module, reactions, submitted = _patch_connector(monkeypatch)
    connection = _FakeConnection(metadata={"slack": {"monitored_channels": ["C_ALERTS"]}})
    config = connector_module.SlackConnectorConfig(
        bot_token="xoxb-x", app_token="xapp-x", bot_user_id="BILLO"
    )

    await connector_module.process_socket_payload(
        None,
        connection=connection,
        socket_payload=_socket_mode_channel_message(text="<@BILLO> hey"),
        config=config,
    )

    assert reactions == []
    assert submitted and submitted[0]["origin"] == "slack.app_mention"


@pytest.mark.asyncio
async def test_process_socket_payload_ignores_unmonitored_channel(monkeypatch):
    connector_module, reactions, submitted = _patch_connector(monkeypatch)
    connection = _FakeConnection(metadata={"slack": {"monitored_channels": []}})
    config = connector_module.SlackConnectorConfig(
        bot_token="xoxb-x", app_token="xapp-x", bot_user_id="BILLO"
    )

    result = await connector_module.process_socket_payload(
        None,
        connection=connection,
        socket_payload=_socket_mode_channel_message(),
        config=config,
    )

    assert result["ignored"] is True
    assert reactions == []
    assert submitted == []


# --------------------------------------------------------------------------- #
# Self-configuration: manage_slack monitored-channel helpers                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_monitor_channel_add_list_remove_roundtrip():
    from brain.systems.slack.monitors import (
        add_monitored_channel,
        list_monitored_channels,
        monitored_channel_ids,
        remove_monitored_channel,
    )

    connection = _FakeConnection()
    session = _FakeSession(connection)

    await add_monitored_channel(
        session, connection_id="conn1", channel_id="C_ALERTS", org_id="org1", channel_name="alerts"
    )
    listed = await list_monitored_channels(session, connection_id="conn1", org_id="org1")
    assert listed == [{"channel_id": "C_ALERTS", "channel_name": "alerts", "enabled": True}]
    assert monitored_channel_ids(connection) == {"C_ALERTS"}

    # Re-adding is idempotent.
    await add_monitored_channel(session, connection_id="conn1", channel_id="C_ALERTS", org_id="org1")
    assert len(await list_monitored_channels(session, connection_id="conn1", org_id="org1")) == 1

    removed = await remove_monitored_channel(
        session, connection_id="conn1", channel_id="C_ALERTS", org_id="org1"
    )
    assert removed["removed"] is True
    assert monitored_channel_ids(connection) == set()


def test_monitored_channel_ids_supports_legacy_strings_and_disabled():
    from brain.systems.slack.monitors import monitored_channel_ids

    connection = _FakeConnection(
        metadata={
            "slack": {
                "monitored_channels": [
                    "C1",
                    {"channel_id": "C2", "enabled": False},
                    {"channel_id": "C3"},
                ]
            }
        }
    )

    assert monitored_channel_ids(connection) == {"C1", "C3"}


@pytest.mark.asyncio
async def test_add_monitored_channel_rejects_non_slack_connection():
    from brain.systems.slack.monitors import SlackMonitorConfigError, add_monitored_channel

    connection = _FakeConnection()
    connection.agent_kind = "github"

    with pytest.raises(SlackMonitorConfigError):
        await add_monitored_channel(
            _FakeSession(connection), connection_id="conn1", channel_id="C1", org_id="org1"
        )


@pytest.mark.asyncio
async def test_monitor_channel_preserves_identity_map_metadata():
    from brain.systems.slack.monitors import add_monitored_channel

    connection = _FakeConnection(metadata={"slack": {"identity_map": {"U1": "user-1"}}})
    session = _FakeSession(connection)

    await add_monitored_channel(session, connection_id="conn1", channel_id="C_ALERTS", org_id="org1")

    # Adding a monitor must not clobber the existing identity map.
    assert connection.metadata_["slack"]["identity_map"] == {"U1": "user-1"}
    assert connection.metadata_["slack"]["monitored_channels"][0]["channel_id"] == "C_ALERTS"
