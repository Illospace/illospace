from __future__ import annotations

import pytest
from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import Org, User


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            OpenAsk.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_app_report_connection(session):
    session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    connection = ExternalAgentConnectionRow(
        id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        display_name="Uwear app support reports",
        agent_kind="uwear_app",
        transport="webhook",
        status="online",
        remote_agent_card={},
        capabilities={"app_report": True},
        auth_metadata={},
        metadata_={},
    )
    session.add(connection)
    await session.flush()
    return connection


def _report_payload() -> dict:
    return {
        "email": "customer@example.com",
        "profileId": "profile-123",
        "type": "Issue",
        "message": "The generation completed, but the result stayed blank.",
        "attachments": [
            "https://cdn.example.com/support/screenshot.png",
            {"url": "https://cdn.example.com/support/log.txt", "name": "log.txt"},
        ],
        "generation_ids": [901, "gen-902"],
        "batch_ids": [77, "batch-78"],
    }


@pytest.mark.asyncio
async def test_app_report_work_intake_carries_customer_request_contract():
    from brain.systems.app_report.triggers import build_app_report_work_intake_payload
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    trigger = build_app_report_work_intake_payload(
        org_id=ORG_ID,
        authority_user_id=USER_ID,
        payload=_report_payload(),
        inbound_event_id="inbound-1",
        connection_id=CONNECTION_ID,
        idempotency_key="app-report:profile-123:1",
        origin="uwear.app_report",
    )
    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger),
    )

    assert trigger["source"] == "app_report"
    assert trigger["event_type"] == "customer_request.issue"
    assert trigger["target"]["kind"] == "app_report"
    assert trigger["payload"]["app_report"] == _report_payload()
    assert request.thread_id == "app-report:inbound-1"
    assert request.target_ref["generation_ids"] == [901, "gen-902"]
    assert request.target_ref["batch_ids"] == [77, "batch-78"]
    assert request.metadata["app_report"]["generation_ids"] == [901, "gen-902"]
    assert request.metadata["app_report"]["batch_ids"] == [77, "batch-78"]
    assert request.metadata["signal_kind"] == "customer_request"
    assert request.metadata["headless"] is True
    assert request.metadata["work_intake"]["source"] == "app_report"
    assert "do not guess a causing generation" in request.message


@pytest.mark.asyncio
async def test_app_report_envelope_is_admitted_and_acknowledged_as_processed(session):
    from brain.systems.inbound.service import submit_inbound_envelope

    connection = await _seed_app_report_connection(session)
    payload = _report_payload()
    result = await submit_inbound_envelope(
        session,
        connection=connection,
        envelope={
            "kind": "app_report",
            "origin": "uwear.app_report",
            "payload": payload,
            "summary": payload["message"],
            "desired_outcome": "Admit this customer request for Illo coordination.",
            "idempotency_key": "app-report:profile-123:1",
        },
        ingress_context={"surface": "webhook", "source": "contact-support"},
    )

    event = (await session.scalars(select(InboundEventRow))).one()
    run = (await session.scalars(select(AgentRunRow))).one()
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert result["status"] == "processed"
    assert result["ilo_outcome"]["operation"] == "app_report_run_admitted"
    assert result["ilo_outcome"]["run_id"] == run.id
    assert result["ilo_outcome"]["ack"] == {
        "status": "accepted",
        "message": "Thanks — your issue was received.",
        "event_id": str(event.id),
    }
    assert event.kind == "app_report"
    assert event.origin == "uwear.app_report"
    assert event.status == "processed"
    assert event.action_type == "app_report.run_admitted"
    assert event.raw_payload == payload
    assert event.ingress_context == {
        "surface": "webhook",
        "source": "contact-support",
    }
    assert run.thread_id == f"app-report:{event.id}"
    assert run.target_ref["kind"] == "app_report"
    assert run.target_ref["generation_ids"] == [901, "gen-902"]
    assert run.target_ref["batch_ids"] == [77, "batch-78"]
    assert run.metadata_["app_report"] == payload
    assert run.metadata_["inbound_event"]["event_id"] == str(event.id)
    assert run.metadata_["work_intake"]["event_type"] == "customer_request.issue"
    assert receipt.target["kind"] == "app_report"
    assert receipt.target["generation_ids"] == [901, "gen-902"]
    assert receipt.target["batch_ids"] == [77, "batch-78"]
    assert receipt.outcome["ack"] == result["ilo_outcome"]["ack"]
    assert receipt.tool_use["type"] == "app_report_intake"
    assert receipt.tool_use["status"] == "accepted"
