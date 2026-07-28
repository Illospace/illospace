"""Contact-form policy cases for typed monitored Slack intake."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.slack_monitor_fixtures import (
    FakeSlackConnection,
    FakeSlackSession,
    channel_monitor_payload,
    patch_slack_connector,
    socket_mode_channel_message,
)


def _contact_form_text(*, include_phone: bool = True) -> str:
    fields = [
        "New Contact Form Submission",
        "Email: aline@madamedusk.com",
        "Name: Aline Athaydes",
        "Message: Can Uwear generate consistent models wearing lingerie?",
        "Company Website: https://www.madamedusk.com",
    ]
    if include_phone:
        fields.insert(3, "Phone: +55 11 99999-9999")
    return "\n".join(fields)


def test_alert_and_contact_form_are_peer_policies_in_one_registry():
    from brain.systems.slack.monitored_intakes import (
        MONITORED_INTAKE_POLICIES,
    )

    assert [policy.origin for policy in MONITORED_INTAKE_POLICIES] == [
        "contact_form_lead",
        "slack.channel_message",
    ]
    for policy in MONITORED_INTAKE_POLICIES:
        assert callable(policy.recognize)
        assert callable(policy.enrich)
        assert callable(policy.render)
        assert callable(policy.routing)
        assert callable(policy.obligation)


def test_contact_form_lead_is_decoded_once_with_reordered_fields_and_no_phone():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(
            user="",
            bot_id="B_CONTACT_FORM",
            app_id="A_CONTACT_FORM",
            text=_contact_form_text(include_phone=False),
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "contact_form_lead"
    assert envelope["payload"]["event_kind"] == "contact_form_lead"
    assert envelope["payload"]["contact_form_lead"] == {
        "name": "Aline Athaydes",
        "email": "aline@madamedusk.com",
        "company_website": "https://www.madamedusk.com",
        "phone": None,
        "message": "Can Uwear generate consistent models wearing lingerie?",
    }
    assert envelope["payload"]["obligation_requester"] == {
        "name": "Aline Athaydes",
        "slack_user_id": "B_CONTACT_FORM",
        "user_id": None,
    }


def test_contact_form_lead_is_decoded_from_slack_blocks():
    from brain.systems.slack.ingress import normalize_slack_socket_event

    envelope = normalize_slack_socket_event(
        socket_mode_channel_message(
            user="",
            bot_id="B_CONTACT_FORM",
            app_id="A_CONTACT_FORM",
            subtype="bot_message",
            text="",
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "New Contact Form Submission",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": "*Company Website:*\nwww.madamedusk.com",
                        },
                        {
                            "type": "mrkdwn",
                            "text": "*Name:*\nAline Athaydes",
                        },
                        {
                            "type": "mrkdwn",
                            "text": "*Email:*\naline@madamedusk.com",
                        },
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Message:*\nDoes it support rear-view shots?",
                    },
                },
            ],
        ),
        bot_user_id="BILLO",
        monitored_channels={"C_ALERTS"},
    )

    assert envelope is not None
    assert envelope["origin"] == "contact_form_lead"
    assert envelope["payload"]["contact_form_lead"]["message"] == (
        "Does it support rear-view shots?"
    )


def test_contact_form_policy_builds_threaded_dossier_route_and_obligation():
    from brain.systems.runs.obligation_specs import (
        obligation_spec_from_metadata,
    )
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    lead_payload = channel_monitor_payload()
    lead_payload.update(
        {
            "origin": "contact_form_lead",
            "event_kind": "contact_form_lead",
            "text": _contact_form_text(),
            "response_target": {
                "channel_id": "C_ALERTS",
                "thread_ts": "1716900000.000200",
                "visibility": "public",
            },
            "obligation_requester": {
                "name": "Aline Athaydes",
                "slack_user_id": "B_CONTACT_FORM",
                "user_id": None,
            },
            "contact_form_lead": {
                "name": "Aline Athaydes",
                "email": "aline@madamedusk.com",
                "company_website": "https://www.madamedusk.com",
                "phone": "+55 11 99999-9999",
                "message": "Can Uwear generate consistent models wearing lingerie?",
                "owner": {
                    "name": "Reda",
                    "slack_user_id": "UREDA",
                    "user_id": "user-reda",
                },
            },
        }
    )

    work = build_slack_work_intake_payload(
        org_id="org1",
        authority_user_id="user-reda",
        payload=lead_payload,
    )

    assert work["event_type"] == "contact_form_lead"
    metadata = work["payload"]["metadata"]
    assert metadata["required_response_tool"] == "post_slack_reply"
    assert metadata["headless"] is True
    assert metadata["contact_form_lead"]["owner"]["slack_user_id"] == "UREDA"
    spec = obligation_spec_from_metadata(metadata["obligation_spec"])
    assert spec is not None
    assert spec.answerer.slack_user_id == "UREDA"
    assert spec.notice_after.total_seconds() == 24 * 60 * 60
    assert metadata["contact_form_lead_dossier"].startswith(
        "*Contact-form lead*"
    )
    assert work["payload"]["slack"]["response_target"]["thread_ts"] == (
        "1716900000.000200"
    )
    dossier = metadata["contact_form_lead_dossier"]
    assert "Aline Athaydes" in dossier
    assert "https://www.madamedusk.com" in dossier
    assert "1. Can Uwear generate consistent models wearing lingerie?" in dossier
    assert "<@UREDA>" in dossier
    assert "*Next action:*" in dossier
    assert "reply in this thread with verified answers" in dossier
    assert "on-call" not in dossier.casefold()
    assert "answers_open_ask=false" in work["payload"]["run_message"]


def test_contact_form_owner_policy_defaults_once_without_identity_mapping():
    from brain.systems.slack.contact_form_lead_owner import (
        CONTACT_FORM_OWNER_POLICY,
    )

    owner = CONTACT_FORM_OWNER_POLICY.resolve(
        FakeSlackConnection(metadata={})
    )

    assert owner.to_metadata() == {
        "name": "Reda",
        "slack_user_id": "U04R1A6MZST",
        "user_id": "user-reda",
    }


def test_aline_athaydes_replay_keeps_all_capabilities_human_answered():
    from brain.systems.runs.obligation_specs import ObligationAnswerer
    from brain.systems.slack.contact_form_lead_rendering import (
        contact_form_lead_dossier,
    )
    from brain.systems.slack.contact_form_leads import ContactFormLead

    dossier = contact_form_lead_dossier(
        ContactFormLead(
            name="Aline Athaydes",
            email="aline@madamedusk.com",
            company_website="www.madamedusk.com",
            phone=None,
            message="\n".join(
                [
                    "I have four questions before signing up:",
                    (
                        "1. Can it generate AI models wearing lingerie and bikinis, "
                        "including sheer and lace pieces?"
                    ),
                    (
                        "2. Does it support back and rear-view shots (thong, "
                        "lace-up corset back from behind)?"
                    ),
                    (
                        "3. Does it preserve the real lace/embroidery rather than "
                        "substituting a garment?"
                    ),
                    (
                        "4. Can a consistent model carry across multiple products "
                        "for catalog cohesion?"
                    ),
                    (
                        "I understand your Qwen Intimate feature may require a "
                        "verification step for intimate apparel. Could you let me know "
                        "what's involved and how to get it enabled for my store?"
                    ),
                ]
            ),
        ),
        ObligationAnswerer(
            name="Reda",
            slack_user_id="UREDA",
            user_id="user-reda",
        ),
    )

    expected_asks = [
        "generate AI models wearing lingerie and bikinis",
        "support back and rear-view shots",
        "preserve the real lace/embroidery",
        "consistent model carry across multiple products",
        "Qwen Intimate feature may require a verification step",
    ]
    for index, ask in enumerate(expected_asks, start=1):
        assert f"{index}." in dossier
        assert ask in dossier
    assert dossier.count("*Answer:* needs a human answer") == 5


@pytest.mark.asyncio
async def test_contact_form_lead_gets_common_eyes_and_policy_enrichment(
    monkeypatch,
):
    connector_module, reactions, submitted = patch_slack_connector(monkeypatch)
    connection = FakeSlackConnection(
        metadata={
            "slack": {
                "monitored_channels": ["C_ALERTS"],
                "identity_map": {"UREDA": "user-reda"},
            },
            "identity_links": {
                "slack": {
                    "UREDA": {
                        "user_id": "user-reda",
                        "display_name": "Reda",
                    }
                }
            },
        }
    )
    config = connector_module.SlackConnectorConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        bot_user_id="BILLO",
    )

    await connector_module.process_socket_payload(
        None,
        connection=connection,
        socket_payload=socket_mode_channel_message(
            user="",
            bot_id="B_CONTACT_FORM",
            app_id="A_CONTACT_FORM",
            text=_contact_form_text(),
        ),
        config=config,
    )

    assert reactions == [("C_ALERTS", "1716900000.000200", "eyes")]
    assert submitted[0]["origin"] == "contact_form_lead"
    owner = submitted[0]["payload"]["contact_form_lead"]["owner"]
    assert owner == {
        "name": "Reda",
        "slack_user_id": "UREDA",
        "user_id": "user-reda",
    }


@pytest.mark.asyncio
async def test_connector_dispatches_thread_reply_to_generic_obligation_matcher(
    monkeypatch,
):
    from brain.systems.runs import open_asks

    connector_module, _reactions, _submitted = patch_slack_connector(
        monkeypatch
    )
    connection = FakeSlackConnection(
        metadata={"slack": {"monitored_channels": ["C_ALERTS"]}}
    )
    session = FakeSlackSession(connection)
    record_answer = AsyncMock(return_value=1)
    monkeypatch.setattr(
        open_asks,
        "record_inbound_slack_obligation_answer",
        record_answer,
    )
    config = connector_module.SlackConnectorConfig(
        bot_token="xoxb-x",
        app_token="xapp-x",
        bot_user_id="BILLO",
    )

    await connector_module.process_socket_payload(
        session,
        connection=connection,
        socket_payload=socket_mode_channel_message(
            user="UREDA",
            text="I sent Aline the verified answers.",
            ts="1716900900.000300",
            event_ts="1716900900.000300",
            thread_ts="1716900000.000200",
        ),
        config=config,
    )

    record_answer.assert_awaited_once_with(
        session,
        org_id="org1",
        channel_id="C_ALERTS",
        thread_ts="1716900000.000200",
        slack_user_id="UREDA",
        message_ts="1716900900.000300",
        answer_text="I sent Aline the verified answers.",
    )
