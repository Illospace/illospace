from __future__ import annotations

import base64
from pathlib import Path
import json
import re

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
import yaml

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.org import Org, User


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "deploy" / "slack" / "illo-self-hosted-manifest.yml"
ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
MAPPED_USER_ID = "55555555-5555-4555-8555-555555555555"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    for name in ("visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_slack_teammate_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._slack_teammate_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_slack_connection(session):
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    connection = ExternalAgentConnectionRow(
        id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Slack",
        agent_kind="slack",
        transport="slack_socket_mode",
        status="online",
        remote_agent_id="T789",
        remote_agent_card={},
        capabilities={"slack": {"socket_mode": True}},
        auth_metadata={"bot_token_ref": "env:SLACK_BOT_TOKEN", "app_token_ref": "env:SLACK_APP_TOKEN"},
        metadata_={"slack": {"team_id": "T789", "bot_user_id": "BILLO"}},
    )
    session.add(connection)
    await session.flush()
    return connection


async def _seed_second_slack_connection(session, *, status: str = "online"):
    connection = ExternalAgentConnectionRow(
        id="44444444-4444-4444-8444-444444444444",
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Slack duplicate connector",
        agent_kind="slack",
        transport="slack_socket_mode",
        status=status,
        remote_agent_id="T789",
        remote_agent_card={},
        capabilities={"slack": {"socket_mode": True}},
        auth_metadata={"bot_token_ref": "env:SLACK_BOT_TOKEN", "app_token_ref": "env:SLACK_APP_TOKEN"},
        metadata_={"slack": {"team_id": "T789", "bot_user_id": "BILLO"}},
    )
    session.add(connection)
    await session.flush()
    return connection


def test_self_hosted_slack_manifest_supports_illo_teammate_loop():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text())

    assert manifest["display_information"]["name"] == "Illo"
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert "request_url" not in manifest["settings"].get("event_subscriptions", {})

    bot_scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert {
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "chat:write.public",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "users:read",
    }.issubset(bot_scopes)

    bot_events = {event["type"] for event in manifest["settings"]["event_subscriptions"]["bot_events"]}
    assert {"app_mention", "message.im"} <= bot_events
    assert "message.channels" not in bot_events
    assert "message.groups" not in bot_events

    features = manifest["features"]
    assert features["bot_user"]["display_name"] == "Illo"
    assert "assistant_view" not in features


@pytest.mark.asyncio
async def test_slack_connector_runtime_config_falls_back_to_vault(monkeypatch):
    from brain.systems.slack import connector

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("ILLO_SLACK_ORG_ID", raising=False)
    monkeypatch.delenv("ILLO_SLACK_OWNER_USER_ID", raising=False)

    async def resolve_authority(*, org_id, owner_user_id):
        assert org_id is None
        assert owner_user_id is None
        return ORG_ID, USER_ID

    async def read_runtime_secret(key_name, *, context, reason, requested_by, access, allow_env_fallback):
        assert context.org_id == ORG_ID
        assert context.actor_user_id == USER_ID
        assert reason
        assert requested_by == "slack_connector"
        assert access == "service"
        assert allow_env_fallback is True
        return {
            "SLACK_BOT_TOKEN": "xoxb-from-vault",
            "SLACK_APP_TOKEN": "xapp-from-vault",
        }[key_name]

    monkeypatch.setattr(connector, "resolve_slack_connector_authority", resolve_authority)
    monkeypatch.setattr("brain.systems.vault.runtime_secrets.read_runtime_secret", read_runtime_secret)

    config = await connector.SlackConnectorConfig.from_runtime()

    assert config.bot_token == "xoxb-from-vault"
    assert config.app_token == "xapp-from-vault"
    assert config.org_id == ORG_ID
    assert config.owner_user_id == USER_ID


def _socket_mode_app_mention(**event_overrides):
    event = {
        "type": "app_mention",
        "user": "U123",
        "text": "<@BILLO> can you turn this into work?",
        "ts": "1716900000.000100",
        "event_ts": "1716900000.000100",
        "channel": "C456",
        "channel_type": "channel",
    }
    event.update(event_overrides)
    return {
        "type": "events_api",
        "envelope_id": "env-1",
        "payload": {
            "team_id": "T789",
            "api_app_id": "A111",
            "event_id": "Ev222",
            "event_time": 1716900000,
            "event": event,
            "authorizations": [
                {
                    "team_id": "T789",
                    "user_id": "BILLO",
                    "is_bot": True,
                }
            ],
        },
    }


def test_socket_mode_app_mention_normalizes_to_slack_surface_envelope():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(_socket_mode_app_mention(), bot_user_id="BILLO")

    assert envelope is not None
    assert envelope["kind"] == "slack_message"
    assert envelope["origin"] == "slack.app_mention"
    assert envelope["idempotency_key"] == "slack:T789:C456:1716900000.000100"
    assert envelope["payload"]["team_id"] == "T789"
    assert envelope["payload"]["channel_id"] == "C456"
    assert envelope["payload"]["channel_type"] == "channel"
    assert envelope["payload"]["message_ts"] == "1716900000.000100"
    assert envelope["payload"]["thread_ts"] == "1716900000.000100"
    assert envelope["payload"]["slack_user_id"] == "U123"
    assert envelope["payload"]["text"] == "<@BILLO> can you turn this into work?"
    assert envelope["hints"]["surface"]["kind"] == "slack"
    assert envelope["hints"]["response_target"] == {
        "channel_id": "C456",
        "thread_ts": None,
        "visibility": "public",
    }


def test_socket_mode_preserves_channel_name_when_present():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_app_mention(channel="G456", channel_name="4_software", channel_type="group"),
        bot_user_id="BILLO",
    )

    assert envelope is not None
    assert envelope["payload"]["channel_id"] == "G456"
    assert envelope["payload"]["channel_name"] == "4_software"
    assert envelope["hints"]["surface"]["channel_name"] == "4_software"


def test_socket_mode_ignores_non_dm_messages_without_illo_mention():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    ignored = normalize_slack_socket_event(
        _socket_mode_app_mention(
            type="message",
            text="just chatting in a channel",
        ),
        bot_user_id="BILLO",
    )

    assert ignored is None


def test_socket_mode_thread_mention_keeps_thread_reply_target():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_app_mention(thread_ts="1716899999.000001"),
        bot_user_id="BILLO",
    )

    assert envelope is not None
    assert envelope["payload"]["surface"] == "slack_thread"
    assert envelope["hints"]["response_target"] == {
        "channel_id": "C456",
        "thread_ts": "1716899999.000001",
        "visibility": "public",
    }


def test_socket_mode_dm_response_target_never_threads():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_app_mention(
            type="message",
            channel="D123",
            channel_type="im",
            text="can you help?",
            thread_ts="1716899999.000001",
        ),
        bot_user_id="BILLO",
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.direct_message"
    assert envelope["payload"]["surface"] == "slack_dm"
    assert envelope["hints"]["response_target"] == {
        "channel_id": "D123",
        "thread_ts": None,
        "visibility": "public",
    }


def test_socket_mode_duplicate_slack_events_share_message_idempotency_key():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    app_mention = normalize_slack_socket_event(
        _socket_mode_app_mention(event_id="Ev-app-mention"),
        bot_user_id="BILLO",
    )
    message_event = normalize_slack_socket_event(
        _socket_mode_app_mention(
            type="message",
            event_id="Ev-message-channel",
        ),
        bot_user_id="BILLO",
    )

    assert app_mention is not None
    assert message_event is not None
    assert app_mention["idempotency_key"] == message_event["idempotency_key"]
    assert app_mention["idempotency_key"] == "slack:T789:C456:1716900000.000100"


def test_slack_adapter_builds_teammate_trigger():
    from brain.app.triggers.adapters.slack import build_slack_message_trigger

    trigger = build_slack_message_trigger(
        org_id="org-1",
        authority_user_id="owner-1",
        payload={
            "event_kind": "mention",
            "origin": "slack.app_mention",
            "team_id": "T789",
            "channel_id": "C456",
            "channel_type": "channel",
            "message_ts": "1716900000.000100",
            "thread_ts": "1716900000.000100",
            "slack_user_id": "U123",
            "text": "<@BILLO> ship it",
            "permalink": "https://example.slack.com/archives/C456/p1716900000000100",
        },
        inbound_event_id="inbound-1",
        connection_id="conn-1",
        idempotency_key="slack:T789:Ev222",
    )

    assert trigger.source == "slack"
    assert trigger.event_type == "slack.app_mention"
    assert trigger.org_id == "org-1"
    assert trigger.actor.id == "owner-1"
    assert trigger.target["kind"] == "slack_message"
    assert trigger.target["team_id"] == "T789"
    assert trigger.target["channel_id"] == "C456"
    assert trigger.payload["user_id"] == "owner-1"
    assert trigger.payload["metadata"]["required_response_tool"] == "post_slack_reply"
    assert trigger.payload["metadata"]["final_answer_target_surface"] == "slack"
    assert trigger.payload["metadata"]["inbound_event"]["event_id"] == "inbound-1"
    assert trigger.payload["metadata"]["slack_trigger"]["response_target"] == {
        "channel_id": "C456",
        "thread_ts": None,
        "visibility": "public",
    }
    assert "Decide whether this is a simple Slack reply" in trigger.payload["run_message"]
    assert "thread_url" in trigger.payload["run_message"]
    assert "post_slack_reply" in trigger.payload["run_message"]


@pytest.mark.asyncio
async def test_slack_work_intake_builds_surface_aware_agent_run_request():
    from brain.app.triggers.adapters.slack import build_slack_message_trigger
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = build_slack_message_trigger(
        org_id="org-1",
        authority_user_id="owner-1",
        payload={
            "event_kind": "direct_message",
            "origin": "slack.direct_message",
            "team_id": "T789",
            "channel_id": "D123",
            "channel_type": "im",
            "message_ts": "1716900100.000200",
            "thread_ts": "1716900100.000200",
            "slack_user_id": "U123",
            "text": "can you summarize my day?",
        },
        connection_id="conn-1",
        idempotency_key="slack:T789:Ev333",
    )

    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger.to_payload()),
    )

    assert request.org_id == "org-1"
    assert request.user_id == "owner-1"
    assert request.thread_id == "slack:T789:D123:1716900100.000200"
    assert request.target_ref["kind"] == "slack_message"
    assert request.target_ref["surface"] == "slack_dm"
    assert request.target_ref["slack_trigger"]["response_target"] == {
        "channel_id": "D123",
        "thread_ts": None,
        "visibility": "public",
    }
    assert request.metadata["source"] == "slack"
    assert request.metadata["required_response_tool"] == "post_slack_reply"
    assert request.metadata["final_answer_target_surface"] == "slack"
    assert request.metadata["slack_trigger"]["channel_type"] == "im"


@pytest.mark.asyncio
async def test_slack_work_intake_uses_backing_cortex_thread_when_provided():
    from brain.app.triggers.adapters.slack import build_slack_message_trigger
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = build_slack_message_trigger(
        org_id="org-1",
        authority_user_id="owner-1",
        payload={
            "event_kind": "mention",
            "origin": "slack.app_mention",
            "team_id": "T789",
            "channel_id": "C456",
            "channel_type": "channel",
            "message_ts": "1716900000.000100",
            "thread_ts": "1716900000.000100",
            "slack_user_id": "U123",
            "text": "<@BILLO> ship it",
        },
        connection_id="conn-1",
        idempotency_key="slack:T789:C456:1716900000.000100",
    ).to_payload()
    trigger["target"]["idea_id"] = "idea-123"
    trigger["target"]["thread_id"] = "idea-123"
    trigger["target"]["thread_url"] = "https://illo.example.com/threads/idea-123"

    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger),
    )

    assert request.thread_id == "idea-123"
    assert request.target_ref["idea_id"] == "idea-123"
    assert request.target_ref["thread_id"] == "idea-123"
    assert request.target_ref["thread_url"] == "https://illo.example.com/threads/idea-123"
    assert request.target_ref["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert request.target_ref["related_surfaces"]["slack"]["thread_id"] == "slack:T789:C456:1716900000.000100"


@pytest.mark.asyncio
async def test_slack_inbound_envelope_records_event_and_admits_slack_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.slack.ingress import normalize_slack_socket_event

    connection = await _seed_slack_connection(session)
    envelope = normalize_slack_socket_event(_socket_mode_app_mention(), bot_user_id="BILLO")

    result = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-1"},
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    run = (await session.scalars(select(AgentRunRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "slack_run_admitted"
    assert result["ilo_outcome"]["run_id"] == run.id
    assert event.kind == "slack_message"
    assert event.origin == "slack.app_mention"
    assert event.status == "processed"
    assert event.action_type == "slack.run_admitted"
    assert event.ingress_context["transport"] == "slack_socket_mode"
    assert "idea_id" not in result["ilo_outcome"]
    assert "thread_id" not in result["ilo_outcome"]
    assert "thread_url" not in result["ilo_outcome"]
    assert run.thread_id == "slack:T789:C456:1716900000.000100"
    assert run.target_ref["kind"] == "slack_message"
    assert run.target_ref["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert run.metadata_["slack_trigger"]["channel_id"] == "C456"
    assert run.metadata_["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert "cortex_thread" not in run.metadata_
    assert run.metadata_["inbound_event"]["event_id"] == str(event.id)
    assert receipt.target["kind"] == "slack_message"
    assert receipt.target["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert receipt.tool_use["slack_thread_id"] == "slack:T789:C456:1716900000.000100"

    replay = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=envelope,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-1-retry"},
    )

    assert replay["idempotent_replay"] is True
    assert (await session.scalars(select(AgentRunRow))).all() == [run]


@pytest.mark.asyncio
async def test_duplicate_slack_events_across_connection_rows_share_one_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.slack.ingress import normalize_slack_socket_event

    first_connection = await _seed_slack_connection(session)
    second_connection = await _seed_second_slack_connection(session)
    app_mention = normalize_slack_socket_event(
        _socket_mode_app_mention(event_id="Ev-app-mention"),
        bot_user_id="BILLO",
    )
    message_event = normalize_slack_socket_event(
        _socket_mode_app_mention(
            type="message",
            event_id="Ev-message-channel",
        ),
        bot_user_id="BILLO",
    )

    first = await submit_inbound_envelope(
        session,
        connection=first_connection,
        envelope=app_mention,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-1"},
    )
    second = await submit_inbound_envelope(
        session,
        connection=second_connection,
        envelope=message_event,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-2"},
    )

    runs = (await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id.asc()))).all()
    events = (await session.scalars(select(InboundEventRow).order_by(InboundEventRow.created_at.asc()))).all()

    assert len(events) == 2
    assert len(runs) == 1
    assert runs[0].thread_id == "slack:T789:C456:1716900000.000100"
    assert runs[0].source_idempotency_scope == "slack"
    assert runs[0].source_idempotency_key == "slack:T789:C456:1716900000.000100"
    assert "thread_id" not in first["ilo_outcome"]
    assert "thread_id" not in second["ilo_outcome"]
    assert first["ilo_outcome"]["run_id"] == runs[0].id
    assert second["ilo_outcome"]["run_id"] == runs[0].id


@pytest.mark.asyncio
async def test_slack_thread_followup_reuses_slack_conversation_key_with_new_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.slack.ingress import normalize_slack_socket_event

    connection = await _seed_slack_connection(session)
    first_message = normalize_slack_socket_event(
        _socket_mode_app_mention(text="<@BILLO> go launch a thread for this"),
        bot_user_id="BILLO",
    )
    followup_message = normalize_slack_socket_event(
        _socket_mode_app_mention(
            text="<@BILLO> is the thread ongoing? can you share the link?",
            ts="1716900300.000400",
            event_ts="1716900300.000400",
            thread_ts="1716900000.000100",
            event_id="Ev-followup",
        ),
        bot_user_id="BILLO",
    )

    first = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=first_message,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-1"},
    )
    followup = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope=followup_message,
        ingress_context={"transport": "slack_socket_mode", "envelope_id": "env-2"},
    )

    runs = (await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id.asc()))).all()

    assert len(runs) == 2
    assert runs[0].thread_id == "slack:T789:C456:1716900000.000100"
    assert runs[1].thread_id == "slack:T789:C456:1716900000.000100"
    assert runs[0].source_idempotency_key == "slack:T789:C456:1716900000.000100"
    assert runs[1].source_idempotency_key == "slack:T789:C456:1716900300.000400"
    assert first["ilo_outcome"]["slack"]["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert followup["ilo_outcome"]["slack"]["slack_thread_id"] == "slack:T789:C456:1716900000.000100"
    assert "thread_url" not in first["ilo_outcome"]
    assert "thread_url" not in followup["ilo_outcome"]


@pytest.mark.asyncio
async def test_slack_connector_sets_native_processing_status_for_actionable_payload(session, monkeypatch):
    from brain.systems.slack import connector

    connection = await _seed_slack_connection(session)
    calls = []

    class _SlackClient:
        def __init__(self, token, **_kwargs):
            calls.append(("init", token))

        async def set_assistant_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"ok": True}

        async def post_message(self, **kwargs):
            calls.append(("message", kwargs))
            return {"ok": True, "ts": "1716900001.000200", "channel": kwargs["channel"]}

    monkeypatch.setattr(connector, "SlackWebClient", _SlackClient, raising=False)

    await connector.process_socket_payload(
        session,
        connection=connection,
        socket_payload=_socket_mode_app_mention(),
        config=connector.SlackConnectorConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            bot_user_id="BILLO",
        ),
    )

    assert calls == [
        ("init", "xoxb-test"),
        (
            "status",
            {
                "channel_id": "C456",
                "thread_ts": "1716900000.000100",
                "status": "is working on it...",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_slack_connector_does_not_reset_native_status_for_duplicate_run(session, monkeypatch):
    from brain.systems.slack import connector

    first_connection = await _seed_slack_connection(session)
    second_connection = await _seed_second_slack_connection(session)
    calls = []

    class _SlackClient:
        def __init__(self, token, **_kwargs):
            calls.append(("init", token))

        async def set_assistant_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"ok": True}

        async def post_message(self, **kwargs):
            calls.append(("message", kwargs))
            return {"ok": True, "ts": "1716900001.000200", "channel": kwargs["channel"]}

    monkeypatch.setattr(connector, "SlackWebClient", _SlackClient, raising=False)

    await connector.process_socket_payload(
        session,
        connection=first_connection,
        socket_payload=_socket_mode_app_mention(event_id="Ev-app-mention"),
        config=connector.SlackConnectorConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            bot_user_id="BILLO",
        ),
    )
    await connector.process_socket_payload(
        session,
        connection=second_connection,
        socket_payload=_socket_mode_app_mention(type="message", event_id="Ev-message-channel"),
        config=connector.SlackConnectorConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            bot_user_id="BILLO",
        ),
    )

    assert calls == [
        ("init", "xoxb-test"),
        (
            "status",
            {
                "channel_id": "C456",
                "thread_ts": "1716900000.000100",
                "status": "is working on it...",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_posts_to_triggering_thread(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "ts": "1716900200.000300", "channel": kwargs["channel"]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                    "visibility": "public",
                }
            },
        }
    ):
        result = json.loads(await _handle_post_slack_reply(body="On it."))

    assert result["ok"] is True
    assert calls == [
        {
            "channel": "C456",
            "text": "On it.",
            "thread_ts": "1716900000.000100",
        }
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_uploads_image_to_triggering_thread(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def upload_file(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "files": [{"id": "F123", "title": kwargs["title"]}]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    image_data = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                    "visibility": "public",
                }
            },
        }
    ):
        result = json.loads(
            await _handle_post_slack_reply(
                body="Here is the chart.",
                image_data=image_data,
                image_filename="weekly-active-users.png",
                image_title="Weekly active users",
                image_alt="Line chart of weekly active users",
            )
        )

    assert result["ok"] is True
    assert result["uploaded_image"] is True
    assert calls == [
        {
            "channel": "C456",
            "file_bytes": b"png-bytes",
            "filename": "weekly-active-users.png",
            "title": "Weekly active users",
            "initial_comment": "Here is the chart.",
            "thread_ts": "1716900000.000100",
            "alt_txt": "Line chart of weekly active users",
            "content_type": "image/png",
        }
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_uploads_image_without_body(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def upload_file(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "files": [{"id": "F123", "title": kwargs["title"]}]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    image_data = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                    "visibility": "public",
                }
            },
        }
    ):
        result = json.loads(await _handle_post_slack_reply(image_data=image_data, image_title="Graph"))

    assert result["ok"] is True
    assert result["uploaded_image"] is True
    assert calls[0]["initial_comment"] is None
    assert calls[0]["filename"] == "Graph.png"


@pytest.mark.asyncio
async def test_post_slack_reply_tool_requires_body_or_image_data():
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    result = json.loads(await _handle_post_slack_reply())

    assert result == {"error": "post_slack_reply requires body or image_data"}


@pytest.mark.asyncio
async def test_post_slack_reply_tool_posts_top_level_mentions_to_channel(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "ts": "1716900200.000300", "channel": kwargs["channel"]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        }
    ):
        result = json.loads(
            await _handle_post_slack_reply(
                body="On it.",
                thread_ts="1716900000.000100",
            )
        )

    assert result["ok"] is True
    assert calls == [
        {
            "channel": "C456",
            "text": "On it.",
            "thread_ts": None,
        }
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_clears_processing_status(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def post_message(self, **kwargs):
            calls.append(("post", kwargs))
            return {"ok": True, "ts": "1716900200.000300", "channel": kwargs["channel"]}

        async def set_assistant_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"ok": True}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "channel_id": "C456",
                "channel_type": "channel",
                "message_ts": "1716900000.000100",
                "thread_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        }
    ):
        result = json.loads(await _handle_post_slack_reply(body="Done."))

    assert result["ok"] is True
    assert calls == [
        (
            "post",
            {
                "channel": "C456",
                "text": "Done.",
                "thread_ts": None,
            },
        ),
        (
            "status",
            {
                "channel_id": "C456",
                "thread_ts": "1716900000.000100",
                "status": "",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_never_threads_originating_dm(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def post_message(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "ts": "1716900200.000300", "channel": kwargs["channel"]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 9,
            "slack_trigger": {
                "channel_id": "D123",
                "channel_type": "im",
                "message_ts": "1716900100.000200",
                "response_target": {
                    "channel_id": "D123",
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        }
    ):
        result = json.loads(
            await _handle_post_slack_reply(
                body="Hi.",
                thread_ts="1716900100.000200",
            )
        )

    assert result["ok"] is True
    assert calls == [
        {
            "channel": "D123",
            "text": "Hi.",
            "thread_ts": None,
        }
    ]


@pytest.mark.asyncio
async def test_slack_runtime_client_uses_central_vault_resolver(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _slack_client_from_runtime

    calls = []

    async def read_runtime_secret(key_name, *, context, reason, requested_by, access, allow_env_fallback):
        calls.append(
            {
                "key_name": key_name,
                "actor_user_id": context.actor_user_id,
                "org_id": context.org_id,
                "run_id": context.run_id,
                "reason": reason,
                "requested_by": requested_by,
                "access": access,
                "allow_env_fallback": allow_env_fallback,
            }
        )
        return "xoxb-from-vault"

    class _SlackClient:
        def __init__(self, token):
            self.token = token

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr("brain.systems.vault.runtime_secrets.read_runtime_secret", read_runtime_secret)
    monkeypatch.setattr("brain.systems.slack.client.SlackWebClient", _SlackClient)

    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID, "run_id": 9, "idea_id": "thread-1"}):
        client = await _slack_client_from_runtime()

    assert client.token == "xoxb-from-vault"
    assert calls == [
        {
            "key_name": "SLACK_BOT_TOKEN",
            "actor_user_id": USER_ID,
            "org_id": ORG_ID,
            "run_id": 9,
            "reason": "Use the configured Slack app to read and reply from Illo's Slack teammate surface.",
            "requested_by": "slack_runtime_tool",
            "access": "service",
            "allow_env_fallback": True,
        }
    ]


@pytest.mark.asyncio
async def test_post_slack_reply_tool_opens_dm_for_slack_user_id(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    calls = []

    class _SlackClient:
        async def open_conversation(self, **kwargs):
            calls.append(("open", kwargs))
            return {"ok": True, "channel": {"id": "D456"}}

        async def post_message(self, **kwargs):
            calls.append(("post", kwargs))
            return {"ok": True, "ts": "1716900200.000300", "channel": kwargs["channel"]}

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID, "run_id": 9}):
        result = json.loads(await _handle_post_slack_reply(body="hi", channel_id="U04R1A6MZST"))

    assert result["ok"] is True
    assert result["channel_id"] == "D456"
    assert calls == [
        ("open", {"users": "U04R1A6MZST"}),
        ("post", {"channel": "D456", "text": "hi", "thread_ts": None}),
    ]


@pytest.mark.asyncio
async def test_read_slack_conversation_tool_reads_bounded_thread_context(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_read_slack_conversation

    calls = []

    class _SlackClient:
        async def conversation_replies(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "messages": [
                    {"user": "U123", "text": "First", "ts": "1716900000.000100"},
                    {"user": "BILLO", "text": "Second", "ts": "1716900001.000200"},
                ],
            }

    async def slack_client():
        return _SlackClient()

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )

    with bind_agent_context(
        {
            "slack_trigger": {
                "channel_id": "C456",
                "thread_ts": "1716900000.000100",
                "response_target": {
                    "channel_id": "C456",
                    "thread_ts": "1716900000.000100",
                    "visibility": "public",
                },
            },
        }
    ):
        result = json.loads(await _handle_read_slack_conversation(scope="thread", limit=2))

    assert result["ok"] is True
    assert result["scope"] == "thread"
    assert len(result["messages"]) == 2
    assert calls == [
        {
            "channel": "C456",
            "thread_ts": "1716900000.000100",
            "limit": 2,
        }
    ]


def test_slack_connector_config_requires_socket_mode_tokens(monkeypatch):
    from brain.systems.slack.connector import SlackConnectorConfig
    from brain.systems.slack.client import SlackConfigurationError

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with pytest.raises(SlackConfigurationError, match="SLACK_BOT_TOKEN"):
        SlackConnectorConfig.from_env()

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with pytest.raises(SlackConfigurationError, match="SLACK_APP_TOKEN"):
        SlackConnectorConfig.from_env()

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    config = SlackConnectorConfig.from_env()
    assert config.bot_token == "xoxb-test"
    assert config.app_token == "xapp-test"


@pytest.mark.asyncio
async def test_slack_connector_marks_connection_error_when_socket_open_fails(session, monkeypatch):
    from brain.systems.slack import connector
    from brain.systems.slack.client import SlackApiError

    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    await session.flush()

    class _SessionFactory:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return None

        def __call__(self):
            return self

    async def open_socket_mode_url(_config):
        raise SlackApiError("not_allowed_token_type")

    monkeypatch.setattr(connector, "open_socket_mode_url", open_socket_mode_url)

    with pytest.raises(SlackApiError, match="not_allowed_token_type"):
        await connector.run_socket_mode_loop(
            config=connector.SlackConnectorConfig(
                bot_token="xoxb-test",
                app_token="xapp-test",
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                team_id="T789",
                bot_user_id="BILLO",
            ),
            session_factory=_SessionFactory(),
        )

    connection = (await session.scalars(select(ExternalAgentConnectionRow))).one()
    assert connection.status == "error"
    assert connection.last_error == "not_allowed_token_type"
    assert connection.metadata_["health"]["status"] == "error"
    assert connection.metadata_["health"]["last_error"] == "not_allowed_token_type"


@pytest.mark.asyncio
async def test_slack_connector_reports_bot_token_used_as_app_token(session):
    from brain.systems.slack import connector
    from brain.systems.slack.client import SlackConfigurationError

    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    await session.flush()

    class _SessionFactory:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return None

        def __call__(self):
            return self

    with pytest.raises(SlackConfigurationError, match="app-level Socket Mode token"):
        await connector.run_socket_mode_loop(
            config=connector.SlackConnectorConfig(
                bot_token="xoxb-test",
                app_token="xoxb-wrong-token",
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                team_id="T789",
                bot_user_id="BILLO",
            ),
            session_factory=_SessionFactory(),
        )

    connection = (await session.scalars(select(ExternalAgentConnectionRow))).one()
    assert connection.status == "error"
    assert "app-level Socket Mode token" in str(connection.last_error)
    assert connection.metadata_["health"]["status"] == "error"


@pytest.mark.asyncio
async def test_slack_connection_health_record_uses_env_backed_tokens(session):
    from brain.systems.slack.connector import ensure_slack_connection

    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    await session.flush()

    connection = await ensure_slack_connection(
        session,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        team_id="T789",
        bot_user_id="BILLO",
        status="connected",
    )

    assert connection.agent_kind == "slack"
    assert connection.transport == "slack_socket_mode"
    assert connection.remote_agent_id == "T789"
    assert connection.status == "connected"
    assert connection.auth_metadata == {
        "bot_token_ref": "env:SLACK_BOT_TOKEN",
        "app_token_ref": "env:SLACK_APP_TOKEN",
    }
    assert connection.metadata_["slack"]["team_id"] == "T789"
    assert connection.metadata_["slack"]["bot_user_id"] == "BILLO"


def test_socket_mode_ack_payload_uses_envelope_id():
    from brain.systems.slack.connector import socket_mode_ack

    assert socket_mode_ack({"envelope_id": "env-1"}) == {"envelope_id": "env-1"}


@pytest.mark.asyncio
async def test_slack_connector_registers_connection_before_first_socket_event(session, monkeypatch):
    import sys
    from types import SimpleNamespace

    from brain.systems.slack.connector import SlackConnectorConfig, run_socket_mode_loop

    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    await session.flush()

    async def auth_test(_client):
        return {"team_id": "T789", "user_id": "BILLO", "team": "Test Slack"}

    class _SessionFactory:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return None

        def __call__(self):
            return self

    class _Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr("brain.systems.slack.connector.SlackWebClient.auth_test", auth_test)
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=lambda _url: _Socket()))

    await run_socket_mode_loop(
        config=SlackConnectorConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            org_id=ORG_ID,
            owner_user_id=USER_ID,
            socket_mode_url="wss://socket.test",
        ),
        session_factory=_SessionFactory(),
    )

    connection = (await session.scalars(select(ExternalAgentConnectionRow))).one()
    assert connection.agent_kind == "slack"
    assert connection.transport == "slack_socket_mode"
    assert connection.remote_agent_id == "T789"
    assert connection.status == "connected"
    assert connection.last_seen_at is not None
    assert connection.metadata_["slack"]["team_id"] == "T789"
    assert connection.metadata_["slack"]["bot_user_id"] == "BILLO"


def test_slack_tools_are_available_on_normal_illo_tool_surface():
    from brain.systems.runs.tool_definitions import CHAT_TOOLS

    names = {tool["name"] for tool in CHAT_TOOLS}
    assert {"post_slack_reply", "read_slack_conversation", "manage_slack"} <= names
    slack_reply = next(tool for tool in CHAT_TOOLS if tool["name"] == "post_slack_reply")
    properties = slack_reply["input_schema"]["properties"]
    assert {"image_data", "image_filename", "image_title", "image_alt"} <= set(properties)
    assert "data:image/png;base64" in properties["image_data"]["description"]
    for keyword in ("anyOf", "oneOf", "allOf", "not", "enum"):
        assert keyword not in slack_reply["input_schema"]
    assert "required" not in slack_reply["input_schema"]


def test_manage_slack_tool_definition_has_no_operator_setup_action():
    from brain.systems.runs.tool_definitions import CHAT_TOOLS

    tool = next(tool for tool in CHAT_TOOLS if tool["name"] == "manage_slack")
    actions = tool["input_schema"]["properties"]["action"]["enum"]
    serialized_tool = json.dumps(tool)

    assert actions == [
        "status",
        "list_channels",
        "list_mappings",
        "link_identity",
        "unlink_identity",
        "list_monitored",
        "monitor_channel",
        "unmonitor_channel",
    ]
    assert "setup_instructions" not in serialized_tool
    assert "SLACK_" not in serialized_tool
    assert "docker" not in serialized_tool
    assert "deploy" not in serialized_tool
    assert "manifest" not in serialized_tool
    assert "secret" not in serialized_tool
    assert "does not" not in serialized_tool.lower()
    assert "cannot" not in serialized_tool.lower()


@pytest.mark.asyncio
async def test_manage_slack_status_returns_connection_facts_not_setup_guidance(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_manage_slack

    class _Scalars:
        def all(self):
            return []

    class _Session:
        async def scalars(self, _stmt):
            return _Scalars()

    class _UnitOfWork:
        session = _Session()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", _UnitOfWork)

    with bind_agent_context({"org_id": ORG_ID}):
        result = json.loads(await _handle_manage_slack(action="status"))

    assert result["ok"] is True
    assert result["setup_state"] == "not_connected"
    assert result["needs_connection"] is True
    assert result["connection_count"] == 0
    assert result["connections"] == []
    assert "setup_guidance" not in result
    assert "next_step" not in result
    serialized = json.dumps(result)
    delegated_role = "ad" + "min"
    assert "Illospace " + delegated_role not in serialized
    assert "admin" not in serialized.lower()
    assert "setup screen" not in serialized.lower()
    assert "connection setup" not in serialized.lower()
    assert "Socket Mode" not in serialized
    assert "SLACK_" not in serialized
    assert "docker compose" not in serialized
    assert "python -m" not in serialized
    assert "deploy/slack" not in serialized
    assert "manifest" not in serialized
    assert "secret manager" not in serialized
    assert "server secret store" not in serialized
    invented_surface = "Illospace Slack " + "connection setup"
    assert invented_surface not in serialized


@pytest.mark.asyncio
async def test_slack_web_client_lists_conversations_with_safe_defaults(monkeypatch):
    from brain.systems.slack.client import SlackWebClient

    calls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "channels": []}

    class _AsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url, *, headers, params):
            calls.append({"url": url, "headers": headers, "params": params})
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _AsyncClient)

    client = SlackWebClient("xoxb-test")
    await client.conversations_list(
        types="public_channel,private_channel",
        limit=5000,
        cursor="cursor-1",
        exclude_archived=True,
    )

    assert calls == [
        {
            "url": "https://slack.com/api/conversations.list",
            "headers": {
                "Authorization": "Bearer xoxb-test",
            },
            "params": {
                "types": "public_channel,private_channel",
                "limit": 1000,
                "exclude_archived": True,
                "cursor": "cursor-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_manage_slack_list_channels_returns_bot_visible_conversations(session, monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_manage_slack

    connection = await _seed_slack_connection(session)
    calls = []

    class _SlackClient:
        async def conversations_list(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "channels": [
                    {
                        "id": "C456",
                        "name": "team",
                        "is_channel": True,
                        "is_private": False,
                        "is_member": True,
                        "is_archived": False,
                        "num_members": 12,
                    }
                ],
                "response_metadata": {"next_cursor": "next-1"},
            }

    async def slack_client():
        return _SlackClient()

    class _UnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is None:
                await session.flush()
            return None

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", _UnitOfWork)

    with bind_agent_context({"org_id": ORG_ID}):
        result = json.loads(
            await _handle_manage_slack(
                action="list_channels",
                connection_id=str(connection.id),
                channel_types=["public_channel", "bogus", "private_channel"],
                limit=5000,
                cursor="cursor-1",
                include_archived=False,
            )
        )

    assert calls == [
        {
            "types": "public_channel,private_channel",
            "limit": 1000,
            "cursor": "cursor-1",
            "exclude_archived": True,
        }
    ]
    assert result["ok"] is True
    assert result["connection"]["id"] == str(connection.id)
    assert result["count"] == 1
    assert result["channels"] == [
        {
            "id": "C456",
            "name": "team",
            "is_channel": True,
            "is_group": None,
            "is_im": None,
            "is_mpim": None,
            "is_private": False,
            "is_member": True,
            "is_archived": False,
            "num_members": 12,
            "topic": None,
            "purpose": None,
        }
    ]
    assert result["next_cursor"] == "next-1"
    assert result["requested_channel_types"] == "public_channel,private_channel"
    assert result["observed_channel_count"] == 0
    assert "visible to the configured bot token" in result["visibility_note"]


@pytest.mark.asyncio
async def test_manage_slack_list_channels_auto_selects_single_live_connection(session, monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_manage_slack

    live_connection = await _seed_slack_connection(session)
    await _seed_second_slack_connection(session, status="error")
    calls = []

    class _SlackClient:
        async def conversations_list(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "channels": [], "response_metadata": {}}

    async def slack_client():
        return _SlackClient()

    class _UnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is None:
                await session.flush()
            return None

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", _UnitOfWork)

    with bind_agent_context({"org_id": ORG_ID}):
        result = json.loads(await _handle_manage_slack(action="list_channels"))

    assert result["ok"] is True
    assert result["connection"]["id"] == str(live_connection.id)
    assert calls == [
        {
            "types": "public_channel,private_channel,mpim,im",
            "limit": 200,
            "cursor": None,
            "exclude_archived": True,
        }
    ]


@pytest.mark.asyncio
async def test_manage_slack_list_channels_includes_observed_private_slack_surfaces(session, monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_manage_slack

    connection = await _seed_slack_connection(session)
    session.add(
        InboundEventRow(
            org_id=ORG_ID,
            connection_id=str(connection.id),
            kind="slack_message",
            origin="slack.app_mention",
            idempotency_key="slack:T789:G4SOFTWARE:1716901111.000100",
            raw_payload={
                "team_id": "T789",
                "channel_id": "G4SOFTWARE",
                "channel_name": "4_software",
                "channel_type": "group",
                "message_ts": "1716901111.000100",
                "permalink": "https://example.slack.com/archives/G4SOFTWARE/p1716901111000100",
            },
            normalized_payload={},
            envelope={},
            ingress_context={},
            source_actor={},
            authority_user_id=USER_ID,
            status="processed",
        )
    )
    await session.flush()

    calls = []

    class _SlackClient:
        async def conversations_list(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "channels": [],
                "response_metadata": {},
            }

    async def slack_client():
        return _SlackClient()

    class _UnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is None:
                await session.flush()
            return None

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", _UnitOfWork)

    with bind_agent_context({"org_id": ORG_ID}):
        result = json.loads(await _handle_manage_slack(action="list_channels", channel_types="private_channel"))

    assert calls[0]["types"] == "private_channel"
    assert result["count"] == 1
    assert result["observed_channel_count"] == 1
    observed_channel = result["channels"][0]
    assert observed_channel.pop("observed_at")
    assert result["channels"] == [
        {
            "id": "G4SOFTWARE",
            "name": "4_software",
            "is_channel": True,
            "is_group": True,
            "is_im": False,
            "is_mpim": False,
            "is_private": True,
            "is_member": True,
            "is_archived": None,
            "num_members": None,
            "topic": None,
            "purpose": None,
            "source": "observed_slack_event",
            "channel_type": "group",
            "slack_channel_type": "private_channel",
            "permalink": "https://example.slack.com/archives/G4SOFTWARE/p1716901111000100",
        }
    ]
    assert "observed_slack_event" in result["visibility_note"]


@pytest.mark.asyncio
async def test_slack_connector_processes_actionable_socket_payload(session):
    from brain.systems.slack.connector import SlackConnectorConfig, process_socket_payload

    connection = await _seed_slack_connection(session)
    config = SlackConnectorConfig(bot_token="xoxb-test", app_token="xapp-test", bot_user_id="BILLO")

    result = await process_socket_payload(
        session,
        connection=connection,
        socket_payload=_socket_mode_app_mention(),
        config=config,
    )

    assert result["ack"] == {"envelope_id": "env-1"}
    assert result["ignored"] is False
    assert result["inbound"]["status"] == "processed"
    run = (await session.scalars(select(AgentRunRow))).one()
    assert run.metadata_["required_response_tool"] == "post_slack_reply"


@pytest.mark.asyncio
async def test_slack_identity_mapping_uses_linked_illospace_user_for_run(session):
    from brain.systems.inbound.service import submit_inbound_envelope
    from brain.systems.slack.ingress import normalize_slack_socket_event

    connection = await _seed_slack_connection(session)
    session.add(User(id=MAPPED_USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.com"))
    connection.metadata_ = {
        "slack": {
            "team_id": "T789",
            "bot_user_id": "BILLO",
            "identity_map": {"U123": MAPPED_USER_ID},
        }
    }
    await session.flush()

    envelope = normalize_slack_socket_event(_socket_mode_app_mention(), bot_user_id="BILLO")

    await submit_inbound_envelope(session, connection=connection, envelope=envelope)

    run = (await session.scalars(select(AgentRunRow))).one()
    assert run.user_id == MAPPED_USER_ID
    assert run.metadata_["slack_trigger"]["slack_user_id"] == "U123"


@pytest.mark.asyncio
async def test_slack_identity_mapping_service_links_user(session):
    from brain.systems.slack.identity import link_slack_identity, list_slack_identity_mappings

    connection = await _seed_slack_connection(session)
    session.add(User(id=MAPPED_USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.com"))
    await session.flush()

    mapping = await link_slack_identity(
        session,
        connection_id=str(connection.id),
        slack_user_id="U123",
        user_id=MAPPED_USER_ID,
        org_id=ORG_ID,
    )
    mappings = await list_slack_identity_mappings(session, connection_id=str(connection.id), org_id=ORG_ID)

    assert mapping == {"slack_user_id": "U123", "user_id": MAPPED_USER_ID}
    assert mappings == [{"slack_user_id": "U123", "user_id": MAPPED_USER_ID, "user_name": "Alex"}]
    refreshed = await session.get(ExternalAgentConnectionRow, connection.id)
    assert refreshed.metadata_["slack"]["identity_map"] == {"U123": MAPPED_USER_ID}
