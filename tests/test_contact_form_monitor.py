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


def test_contact_form_policy_builds_threaded_mandate_route_and_obligation():
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
    assert metadata["contact_form_lead_skill"] == "contact-form-lead-intake"
    assert metadata["contact_form_lead_mandate_source"] == "installed_skill"
    spec = obligation_spec_from_metadata(metadata["obligation_spec"])
    assert spec is not None
    assert spec.answerer.slack_user_id == "UREDA"
    assert spec.notice_after.total_seconds() == 24 * 60 * 60
    assert "contact_form_lead_dossier" not in metadata
    assert work["payload"]["slack"]["response_target"]["thread_ts"] == (
        "1716900000.000200"
    )
    run_message = work["payload"]["run_message"]
    assert run_message.startswith("/contact-form-lead-intake\n")
    assert "skill_view" in run_message
    assert '"company_website": "https://www.madamedusk.com"' in run_message
    assert '"slack_user_id": "UREDA"' in run_message
    assert "*Contact-form lead*" not in run_message
    assert "*Answer:*" not in run_message


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
        "user_id": None,
    }


def test_contact_form_owner_policy_never_uses_the_connection_owner():
    from brain.systems.slack.contact_form_lead_owner import (
        CONTACT_FORM_OWNER_POLICY,
    )

    owner = CONTACT_FORM_OWNER_POLICY.resolve(
        FakeSlackConnection(
            owner_user_id="user-axel",
            metadata={
                "slack": {
                    "identity_map": {
                        "UAXEL": "user-axel",
                    }
                },
                "identity_links": {
                    "slack": {
                        "UAXEL": {
                            "user_id": "user-axel",
                            "display_name": "Axel",
                        }
                    }
                },
            },
        )
    )

    assert owner.to_metadata() == {
        "name": "Reda",
        "slack_user_id": "U04R1A6MZST",
        "user_id": None,
    }


def test_contact_form_owner_policy_does_not_splice_an_unmatched_configured_user():
    from brain.systems.slack.contact_form_lead_owner import (
        CONTACT_FORM_OWNER_POLICY,
    )

    owner = CONTACT_FORM_OWNER_POLICY.resolve(
        FakeSlackConnection(
            metadata={
                "slack": {
                    "contact_form_lead_owner": {
                        "user_id": "user-unpaired",
                    },
                    "identity_map": {
                        "U04R1A6MZST": "user-reda",
                    },
                },
                "identity_links": {
                    "slack": {
                        "U04R1A6MZST": {
                            "user_id": "user-reda",
                            "display_name": "Reda",
                        }
                    }
                },
            }
        )
    )

    assert owner.to_metadata() == {
        "name": "Reda",
        "slack_user_id": "U04R1A6MZST",
        "user_id": None,
    }


def test_contact_form_owner_policy_keeps_conflicting_identity_fields_together():
    from brain.systems.slack.contact_form_lead_owner import (
        CONTACT_FORM_OWNER_POLICY,
    )

    owner = CONTACT_FORM_OWNER_POLICY.resolve(
        FakeSlackConnection(
            metadata={
                "slack": {
                    "contact_form_lead_owner": {
                        "name": "Axel",
                        "slack_user_id": "UREDA",
                        "user_id": "user-axel",
                    },
                    "identity_map": {
                        "UREDA": "user-reda",
                        "UAXEL": "user-axel",
                    },
                },
                "identity_links": {
                    "slack": {
                        "UREDA": {
                            "user_id": "user-reda",
                            "display_name": "Reda",
                        },
                        "UAXEL": {
                            "user_id": "user-axel",
                            "display_name": "Axel",
                        },
                    }
                },
            }
        )
    )

    assert owner.to_metadata() == {
        "name": "Reda",
        "slack_user_id": "UREDA",
        "user_id": "user-reda",
    }


def test_contact_form_reply_behavior_is_an_installed_runtime_skill():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(
        BUILTIN_SKILL_BUNDLE_ROOT / "contact-form-lead-intake"
    )

    assert bundle.manifest.source == "self_hosted"
    assert bundle.manifest.visibility == "private_local"
    procedure = bundle.skill_markdown
    for requirement in (
        "`search_knowledge`",
        "`post_slack_reply`",
    ):
        assert requirement in procedure
    for hardcoded_example in (
        "madamedusk",
        "lingerie",
        "bikini",
        "corset",
        "thong",
        "bergzeit",
    ):
        assert hardcoded_example not in procedure.casefold()


def test_contact_form_intake_context_has_a_versioned_validated_contract():
    import json

    from brain.systems.runs.obligation_specs import ObligationAnswerer
    from brain.systems.slack.contact_form_lead_rendering import (
        CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION,
        ContactFormLeadIntakeContext,
        ContactFormLeadSlackResponseTarget,
    )
    from brain.systems.slack.contact_form_leads import ContactFormLead

    lead = ContactFormLead(
        name="Prospect",
        email="prospect@example.com",
        company_website="https://prospect.example",
        phone=None,
        message="Tell me about Uwear.",
    )
    owner = ObligationAnswerer(
        name="Reda",
        slack_user_id="UREDA",
        user_id="user-reda",
    )
    context = ContactFormLeadIntakeContext(
        lead=lead,
        owner=owner,
        slack_response_target=ContactFormLeadSlackResponseTarget(
            channel_id="C_ALERTS",
            thread_ts="1716900000.000200",
        ),
        source_permalink="https://example.slack.com/archives/C_ALERTS/p1716900000000200",
    )

    serialized = json.loads(context.serialize())

    assert set(serialized) == {
        "schema_version",
        "lead",
        "owner",
        "slack_response_target",
        "source_permalink",
    }
    assert (
        serialized["schema_version"]
        == CONTACT_FORM_LEAD_INTAKE_SCHEMA_VERSION
        == 1
    )
    assert set(serialized["lead"]) == {
        "name",
        "email",
        "company_website",
        "phone",
        "message",
    }
    assert set(serialized["owner"]) == {
        "name",
        "slack_user_id",
        "user_id",
    }
    assert serialized["slack_response_target"] == {
        "channel_id": "C_ALERTS",
        "thread_ts": "1716900000.000200",
    }

    for invalid_target in (
        ContactFormLeadSlackResponseTarget(
            channel_id="",
            thread_ts="1716900000.000200",
        ),
        ContactFormLeadSlackResponseTarget(
            channel_id="C_ALERTS",
            thread_ts="",
        ),
    ):
        invalid_context = ContactFormLeadIntakeContext(
            lead=lead,
            owner=owner,
            slack_response_target=invalid_target,
        )
        with pytest.raises(ValueError, match="Slack response target"):
            invalid_context.serialize()


@pytest.mark.asyncio
async def test_contact_form_mandate_is_runtime_editable_connection_metadata():
    from brain.systems.runs.obligation_specs import ObligationAnswerer
    from brain.systems.runs.tool_catalog.definitions.cortex_thread import (
        CHAT_TOOLS,
    )
    from brain.systems.slack.contact_form_lead_rendering import (
        contact_form_lead_run_message,
    )
    from brain.systems.slack.contact_form_leads import ContactFormLead
    from brain.systems.slack.monitors import (
        contact_form_lead_mandate,
        set_contact_form_lead_mandate,
    )

    connection = FakeSlackConnection()
    mandate = "Post a two-line commercial assessment with source links."
    result = await set_contact_form_lead_mandate(
        FakeSlackSession(connection),
        connection_id=connection.id,
        org_id=connection.org_id,
        mandate=mandate,
    )
    run_message = contact_form_lead_run_message(
        ContactFormLead(
            name="Prospect",
            email="prospect@example.com",
            company_website="https://prospect.example",
            phone=None,
            message="Tell me about Uwear.",
        ),
        ObligationAnswerer(
            name="Reda",
            slack_user_id="UREDA",
            user_id="user-reda",
        ),
        {
            "response_target": {
                "channel_id": "C_ALERTS",
                "thread_ts": "1716900000.000200",
            },
        },
        mandate=contact_form_lead_mandate(connection),
    )
    manage_slack = next(
        tool for tool in CHAT_TOOLS if tool["name"] == "manage_slack"
    )

    assert result["metadata_path"] == "slack.contact_form_lead_mandate"
    assert mandate in run_message
    assert run_message.startswith("/contact-form-lead-intake\n")
    assert "skill_view" in run_message
    assert "Connection overlay:" in run_message
    assert "within the installed skill's contracts" in run_message
    assert "set_contact_form_lead_mandate" in (
        manage_slack["input_schema"]["properties"]["action"]["enum"]
    )
    assert "mandate" in manage_slack["input_schema"]["properties"]

    cleared = await set_contact_form_lead_mandate(
        FakeSlackSession(connection),
        connection_id=connection.id,
        org_id=connection.org_id,
        mandate="",
    )

    assert cleared["cleared"] is True
    assert cleared["mandate"] is None
    assert contact_form_lead_mandate(connection) is None
    default_run_message = contact_form_lead_run_message(
        ContactFormLead(
            name="Prospect",
            email="prospect@example.com",
            company_website="https://prospect.example",
            phone=None,
            message="Tell me about Uwear.",
        ),
        ObligationAnswerer(
            name="Reda",
            slack_user_id="UREDA",
            user_id="user-reda",
        ),
        {
            "response_target": {
                "channel_id": "C_ALERTS",
                "thread_ts": "1716900000.000200",
            },
        },
        mandate=contact_form_lead_mandate(connection),
    )
    assert default_run_message.startswith("/contact-form-lead-intake\n")
    assert "Connection overlay:" not in default_run_message


@pytest.mark.asyncio
async def test_contact_form_lead_gets_common_eyes_and_policy_enrichment(
    monkeypatch,
):
    from brain.systems.slack.triggers import build_slack_work_intake_payload

    connector_module, reactions, submitted = patch_slack_connector(monkeypatch)
    connection = FakeSlackConnection(
        owner_user_id="user-axel",
        metadata={
            "slack": {
                "monitored_channels": ["C_ALERTS"],
                "identity_map": {"UREDA": "user-reda"},
                "contact_form_lead_mandate": "Configured lead assessment.",
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
    assert (
        submitted[0]["payload"]["contact_form_lead_mandate"]
        == "Configured lead assessment."
    )
    owner = submitted[0]["payload"]["contact_form_lead"]["owner"]
    assert owner == {
        "name": "Reda",
        "slack_user_id": "UREDA",
        "user_id": "user-reda",
    }
    work = build_slack_work_intake_payload(
        org_id=connection.org_id,
        authority_user_id="user-reda",
        payload=submitted[0]["payload"],
    )
    assert (
        work["payload"]["metadata"]["contact_form_lead_mandate_source"]
        == "installed_skill_with_connection_overlay"
    )
    assert work["payload"]["run_message"].startswith(
        "/contact-form-lead-intake\n"
    )
    assert "Configured lead assessment." in work["payload"]["run_message"]


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
