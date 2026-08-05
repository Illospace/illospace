"""Direct Slack liveness probes bypass AgentRun execution."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from tests.slack_monitor_fixtures import (
    FakeSlackConnection,
    socket_mode_channel_message,
)


@pytest.mark.asyncio
async def test_monitored_direct_liveness_probe_replies_on_interrupt_path(
    monkeypatch,
):
    from brain.systems.slack import connector

    submitted = []
    posts = []

    class FakeSlackClient:
        def __init__(self, _token):
            pass

        async def post_message(self, **kwargs):
            posts.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1716900001.000300"}

    async def submit_inbound_envelope(
        _session,
        *,
        connection,
        envelope,
        ingress_context,
    ):
        submitted.append(envelope)
        return {
            "status": "processed",
            "idempotent_replay": False,
            "ilo_outcome": {"operation": "slack_liveness_probe_interrupt"},
        }

    monkeypatch.setattr(connector, "SlackWebClient", FakeSlackClient)
    monkeypatch.setattr(
        connector,
        "submit_inbound_envelope",
        submit_inbound_envelope,
    )
    connection = FakeSlackConnection(
        metadata={"slack": {"monitored_channels": ["C_ALERTS"]}}
    )

    result = await connector.process_socket_payload(
        None,
        connection=connection,
        socket_payload=socket_mode_channel_message(
            type="app_mention",
            text="<@BILLO> are you alive?",
        ),
        config=connector.SlackConnectorConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            bot_user_id="BILLO",
        ),
    )

    assert result["ignored"] is False
    assert submitted[0]["origin"] == "slack.direct_liveness_probe"
    assert submitted[0]["payload"]["event_kind"] == "direct_liveness_probe"
    assert posts == [
        {
            "channel": "C_ALERTS",
            "text": "Yes — Illo is alive and received this probe.",
            "thread_ts": "1716900000.000200",
            "client_msg_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "illospace:slack-interrupt:slack:T789:C_ALERTS:1716900000.000200",
                )
            ),
        }
    ]


@pytest.mark.asyncio
async def test_liveness_probe_inbound_completes_without_admitting_agent_run(
    monkeypatch,
):
    from brain.systems.inbound.handlers import InboundHandlerContext
    from brain.systems.slack import inbound
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(
            type="app_mention",
            text="<@BILLO> are you alive?",
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )
    assert envelope is not None
    completion = None

    async def complete(value):
        nonlocal completion
        completion = value
        return {"status": value.status, "ilo_outcome": dict(value.action_result)}

    admit = AsyncMock()
    monkeypatch.setattr(inbound, "admit_surface_envelope", admit)

    result = await inbound.process_slack_message_envelope(
        object(),
        context=InboundHandlerContext(
            connection_id="conn-1",
            org_id="org-1",
            owner_user_id="user-1",
            token_id=None,
            scopes=None,
            display_name="Slack",
            source_kind="slack",
        ),
        event=type("InboundEvent", (), {"id": "event-1"})(),
        normalized=envelope,
        complete=complete,
    )

    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "slack_liveness_probe_interrupt"
    assert completion.action_type == "slack.liveness_probe_interrupt"
    assert completion.tool_use == {
        "type": "post_slack_reply",
        "status": "interrupt",
    }
    admit.assert_not_awaited()


@pytest.mark.parametrize(
    ("event_overrides", "expected_thread_ts"),
    [
        (
            {
                "type": "message",
                "channel": "D_ILLO",
                "channel_type": "im",
                "text": "Are you there?!",
            },
            None,
        ),
        (
            {
                "type": "app_mention",
                "channel": "C_ORDINARY",
                "text": "<@BILLO>, YOU ALIVE...",
            },
            "1716900000.000200",
        ),
    ],
)
def test_direct_liveness_probe_uses_interrupt_policy_on_dm_and_ordinary_channel(
    event_overrides,
    expected_thread_ts,
):
    from brain.systems.slack.ingress import normalize_slack_socket_event
    from brain.systems.slack.monitored_intakes import monitored_intake_policy

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(**event_overrides),
        bot_user_id="BILLO",
        monitored_channels=set(),
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.direct_liveness_probe"
    assert monitored_intake_policy(envelope).interrupt is not None
    assert envelope["payload"]["response_target"]["thread_ts"] == expected_thread_ts


@pytest.mark.parametrize(
    ("event_overrides", "expected_origin"),
    [
        (
            {
                "type": "app_mention",
                "text": "<@BILLO> keep the worker alive while you deploy this fix",
            },
            "slack.app_mention",
        ),
        (
            {
                "type": "message",
                "text": "<@BOTHER> are you alive?",
            },
            "slack.channel_message",
        ),
    ],
)
def test_liveness_probe_near_misses_keep_their_normal_route(
    event_overrides,
    expected_origin,
):
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(**event_overrides),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == expected_origin


def test_ordinary_monitored_intake_routing_is_unchanged_for_non_probe():
    from brain.systems.slack.ingress import normalize_slack_socket_event
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(
            text="Payments API is throwing 500s in prod",
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )
    assert envelope is not None

    work = build_slack_work_intake_payload(
        org_id="org-1",
        authority_user_id="user-1",
        payload=envelope["payload"],
    )
    metadata = work["payload"]["metadata"]

    assert envelope["origin"] == "slack.channel_message"
    assert metadata["origin"] == "slack_channel_monitor"
    assert metadata["slack_monitor"] is True
    assert metadata["headless"] is True
    assert "required_response_tool" not in metadata
