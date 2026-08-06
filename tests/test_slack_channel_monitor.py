"""Tests for passive Slack channel monitoring (Route A).

Covers the full path: ingress admission of monitored-channel messages, the 👀
reflex reaction, headless triage run framing (no forced reply), and the
``manage_slack`` self-configuration surface for monitored channels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.slack_monitor_fixtures import (
    FakeSlackConnection as _FakeConnection,
    FakeSlackSession as _FakeSession,
    channel_monitor_payload as _channel_monitor_payload,
    patch_slack_connector as _patch_connector,
    socket_mode_channel_message as _socket_mode_channel_message,
)


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


def test_rollbar_alert_in_same_monitored_channel_remains_byte_identical():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(
            user="",
            bot_id="B_ROLLBAR",
            app_id="A_ROLLBAR",
            text="Rollbar: #2206 100th error: ClientError 400 INVALID_ARGUMENT",
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    serialized = (
        json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    baseline = (
        Path(__file__).parent
        / "fixtures"
        / "slack"
        / "rollbar_alert_origin_main.json"
    ).read_bytes()
    assert serialized == baseline


def test_attachment_only_bot_alert_surfaces_fallback_into_monitor_prompt():
    from brain.systems.slack.ingress import normalize_slack_socket_event
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    fallback = (
        "<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/2206|"
        "#2206 100th error: ClientError: 400 INVALID_ARGUMENT>"
    )
    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(
            user="",
            bot_id="B_ROLLBAR",
            app_id="A_ROLLBAR",
            text="",
            attachments=[
                {"fallback": fallback},
                {"title": "secondary alert context"},
                {"fallback": "ignored third attachment"},
            ],
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert fallback in envelope["payload"]["text"]
    assert "secondary alert context" in envelope["payload"]["text"]
    assert "ignored third attachment" not in envelope["payload"]["text"]
    assert fallback in envelope["summary"]

    work = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=envelope["payload"],
    )
    assert fallback in work["payload"]["thread_message"]
    assert f"Message text: {fallback}" in work["payload"]["run_message"]


def test_attachment_previews_are_bounded_to_two_by_500_chars():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(
            user="",
            bot_id="B_ROLLBAR",
            app_id="A_ROLLBAR",
            text="",
            attachments=[
                {"fallback": "a" * 600},
                {"title": "b" * 600},
                {"fallback": "c" * 600},
            ],
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["payload"]["text"] == f"{'a' * 500}\n{'b' * 500}"


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
    assert "Do not use react_to_slack_message" in run_message
    assert "alternative_response_tools" not in metadata


def test_channel_monitor_prompt_requires_customer_support_recall_before_action():
    from brain.systems.slack.channel_monitor_rendering import (
        SUPPORT_INTAKE_RECALL_MANDATE,
    )
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    monitored = _channel_monitor_payload()
    monitored["channel_id"] = "C082SUKQKJL"
    monitored["channel_name"] = "support-intake"
    monitored["response_target"]["channel_id"] = "C082SUKQKJL"
    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=monitored,
    )

    run_message = payload["payload"]["run_message"]
    assert "monitoring Slack #support-intake" in run_message
    assert SUPPORT_INTAKE_RECALL_MANDATE in run_message
    assert run_message.index(SUPPORT_INTAKE_RECALL_MANDATE) < run_message.index(
        "Classify this message and act accordingly:"
    )
    assert "requester email, company id, and error signature" in run_message
    assert "Silence is not allowed for a customer support intake." in run_message
    assert "`uwear-customer-generation-report-triage`" in run_message
    assert "appropriate read-only payload reader and `search_knowledge`" in run_message


def test_channel_monitor_framing_routes_feature_requests_to_tickets():
    # Regression: a Retool-relayed customer feature request ("*New:* Idea") was
    # classified "low-signal" and dropped because the framing only made
    # user-reported *problems* ticket-worthy (run 1550, 2026-07-16).
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
    )

    run_message = payload["payload"]["run_message"]
    assert "feature request or product idea" in run_message
    assert "NOT chatter and NOT low-signal" in run_message
    assert "email/profile id" in run_message


def test_channel_monitor_filing_keeps_shared_triage_ownership_contract():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
    )

    run_message = payload["payload"]["run_message"]
    assert "Load the 'uwear-engineering-triage' skill" in run_message
    assert "first for routing/ownership rules" in run_message
    assert "creating work items" in run_message


def test_channel_monitor_framing_preserves_alert_thread_provenance_on_tracker_records():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
    )

    run_message = payload["payload"]["run_message"]
    assert "alert_slack_channel" in run_message
    assert "alert_slack_thread_ts" in run_message
    assert "future sweeps can re-read human resolution replies" in run_message


def test_channel_monitor_persists_known_timezone_preference_before_confirming():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    monitored = _channel_monitor_payload()
    monitored["text"] = "Please always show these alert times in Eastern."
    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=monitored,
    )

    run_message = payload["payload"]["run_message"]
    assert "manage_runtime_preferences with action='set', setting='display_timezone'" in run_message
    assert "Map ET/Eastern to America/New_York" in run_message
    assert "Only after manage_runtime_preferences returns status='saved'" in run_message
    assert "include its confirmation verbatim" in run_message
    assert "concrete setting" in run_message


def test_channel_monitor_declines_preference_without_write_target():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    monitored = _channel_monitor_payload()
    monitored["text"] = "Always make alerts rhyme."
    payload = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=monitored,
    )

    run_message = payload["payload"]["run_message"]
    assert "If the request has no known writable setting" in run_message
    assert "I can do that for this message, but I have no way to make it stick — file it?" in run_message
    assert "Never imply persistence" in run_message


@pytest.mark.asyncio
async def test_preference_claim_gate_requires_committed_same_run_write(monkeypatch):
    import json
    from types import SimpleNamespace

    from brain.systems.runs import actions as action_audit
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.composition import _get_tool_handlers
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply
    from brain.systems.runtime_settings import display as display_settings
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    stored: dict[str, str] = {}
    state = {"fail_writes": False, "write_commits": 0}
    user = SimpleNamespace(
        id="user1",
        org_id="org1",
        role="owner",
        name="Owner",
        email="owner@example.com",
    )

    class _Rows:
        def all(self):
            return []

    class _Session:
        def __init__(self):
            self.pending: dict[str, str] = {}

        async def get(self, _model, _key):
            return user

        async def scalars(self, _statement):
            return _Rows()

    class _UnitOfWork:
        async def __aenter__(self):
            self.session = _Session()
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            if exc_type is None and self.session.pending:
                stored.update(self.session.pending)
                state["write_commits"] += 1
            return False

    async def _read(_session, key):
        return stored.get(key)

    async def _write(session, key, value):
        if state["fail_writes"]:
            raise RuntimeError("runtime display write failed")
        session.pending[key] = value

    class _SlackClient:
        def __init__(self):
            self.posts = []

        async def post_message(self, **kwargs):
            self.posts.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1785000000.000001"}

    client = _SlackClient()

    async def _slack_client():
        return client

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _UnitOfWork,
    )
    monkeypatch.setattr(display_settings, "_async_read_runtime_config_value", _read)
    monkeypatch.setattr(display_settings, "_async_write_runtime_config_value", _write)
    monkeypatch.setattr(action_audit, "record_action_manifest", lambda _manifest: None)
    monkeypatch.setattr(
        action_audit,
        "complete_action_manifest",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        _slack_client,
    )

    monitored = _channel_monitor_payload()
    monitored["text"] = "Please always show these alert times in Eastern."
    intake = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=monitored,
    )
    metadata = dict(intake["payload"]["metadata"])
    metadata.update({"run_id": 513, "org_id": "org1"})
    manage_preferences = _get_tool_handlers()["manage_runtime_preferences"]

    with bind_agent_context(
        {
            "user_id": "user1",
            "org_id": "org1",
            "execution_metadata": metadata,
            "slack_trigger": metadata["slack_trigger"],
        }
    ):
        direct = json.loads(
            await _handle_post_slack_reply(
                body="Yes, I'll remember to show alert times in Eastern."
            )
        )
        assert direct["error"] == "persistence_claim_requires_write_receipt"
        assert direct["posted"] is False
        assert client.posts == []

        state["fail_writes"] = True
        with pytest.raises(RuntimeError, match="runtime display write failed"):
            await manage_preferences(
                action="set",
                setting="display_timezone",
                value="Eastern",
            )
        failed_confirmation = json.loads(
            await _handle_post_slack_reply(
                body="Saved: alerts will render Eastern alongside UTC."
            )
        )
        assert failed_confirmation["error"] == "persistence_claim_requires_write_receipt"
        assert state["write_commits"] == 0
        assert client.posts == []

        state["fail_writes"] = False
        saved = await manage_preferences(
            action="set",
            setting="display_timezone",
            value="Eastern",
        )
        assert saved["status"] == "saved"
        assert saved["write_receipt"]["run_id"] == 513
        assert state["write_commits"] == 1
        assert json.loads(stored["runtime_display"])["write_receipts"] == [
            saved["write_receipt"]
        ]

        committed_confirmation = json.loads(
            await _handle_post_slack_reply(body=saved["confirmation"])
        )
        assert committed_confirmation["ok"] is True
        assert len(client.posts) == 1


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
    assert metadata["alternative_response_tools"] == ["react_to_slack_message"]
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


def test_channel_monitor_replays_2026_07_16_requests_without_silence():
    """Both #358 anchor messages reach a visible-action classifier branch."""
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    messages = [
        "demande assez facile a faire je pense",
        (
            "@Illo bug in staging, downloading in bulk make ZIPs that cant be opened "
            "(also abnormaly just 6kb zips vs excpected 3MB and more for multiples photos)"
        ),
    ]

    run_messages = []
    for message in messages:
        monitored_payload = _channel_monitor_payload()
        monitored_payload["text"] = message
        work = build_slack_work_intake_payload(
            org_id="org1",
            authority_user_id="user1",
            payload=monitored_payload,
        )
        run_message = work["payload"]["run_message"]
        assert f"Message text: {message}" in run_message
        assert "ask exactly ONE focused clarifying question in-thread" in run_message
        run_messages.append(run_message)

    bulk_download_run_message = run_messages[1]
    assert "root-cause hypothesis naming the target repo" in bulk_download_run_message
    assert "repo and incident clear" in bulk_download_run_message
    assert (
        "include the investigation findings in the issue body"
        in bulk_download_run_message.lower()
    )
    assert "same run" in bulk_download_run_message


def test_channel_monitor_third_branch_preserves_casual_commentary_silence():
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    work = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=_channel_monitor_payload(),
    )

    run_message = work["payload"]["run_message"]
    # The silence branch must survive the #358 third branch: chatter and pure alert
    # commentary still get no visible action.
    assert (
        "Casual chatter, or discussion about an existing alert that does not itself ask for "
        "work: take NO visible action. Do not reply."
    ) in run_message
    # ...but proximity to an alert must not by itself route a human request into
    # silence — that misread is what produced run 1562 (#358 instance 1).
    assert "NOT alert commentary merely because it arrived near an alert" in run_message
    assert "does not apply to casual chatter or genuine commentary" in run_message


@pytest.mark.parametrize("event_type", ["app_mention", "message"])
def test_every_app_mention_inbound_event_requires_a_visible_response(event_type):
    from brain.systems.slack.ingress import normalize_slack_socket_event
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    envelope = normalize_slack_socket_event(
        _socket_mode_channel_message(
            type=event_type,
            text="<@BILLO> not enough detail yet",
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "slack.app_mention"
    work = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user1",
        payload=envelope["payload"],
    )
    metadata = work["payload"]["metadata"]
    assert metadata["required_response_tool"] == "post_slack_reply"
    assert metadata["final_answer_target_surface"] == "slack"
    assert "No visible action taken." not in work["payload"]["run_message"]
