from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from brain.platform.db.models.domain import DomainEvent, DomainRecord
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionKeyRow,
    InboundEventRow,
)
from brain.systems.inbound import service as inbound
from brain.systems.user_domains.service import AsyncDomainService
from tests.inbound_admin_support import session as session
from tests.inbound_preservation_support import (
    CONNECTION_ID,
    ORG_ID,
    USER_ID,
    _seed_connection,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def source(session):
    principal = await _seed_connection(session)
    policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="GitHub issues",
        origin_patterns=["github:example/repo"],
        envelope_kinds=["github_event"],
        allowed_actions=[inbound.ACTION_DOMAIN_PROJECTION_UPSERT],
    )
    envelope = {
        "kind": "github_event",
        "origin": "github:example/repo",
        "payload": {"issue": {"number": 900, "title": "Track issue", "state": "open"}},
        "idempotency_key": "github:fanout:900",
    }
    return principal, policy, envelope


async def _add_projection(session, policy, name):
    domain = await AsyncDomainService(session).create_domain(
        ORG_ID,
        name=name,
        objects=[{
            "key": "issue",
            "title_field": "summary",
            "fields": [
                {"key": "external_id", "field_type": "text", "required": True},
                {"key": "summary", "field_type": "text", "required": True},
                {"key": "status", "field_type": "text"},
            ],
        }],
        actor_id=USER_ID,
    )
    return await inbound.create_domain_projection(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        policy_id=str(policy.id),
        domain_id=domain.id,
        object_key="issue",
        external_id_path="payload.issue.number",
        external_id_field="external_id",
        field_mapping={"summary": "payload.issue.title", "status": "payload.issue.state"},
        title_path="payload.issue.title",
        validation_failure_status=inbound.STATUS_QUARANTINED,
    )


@pytest.fixture
async def projections(session, source):
    _, policy, _ = source
    feed = await _add_projection(session, policy, "Event Feed")
    tracker = await _add_projection(session, policy, "Tracker")
    feed.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.created_at = feed.created_at + timedelta(days=1)
    await session.flush()
    return feed, tracker


@pytest.mark.parametrize("order_by", ["created_at", "id"])
async def test_all_enabled_projections_apply_in_order(session, source, projections, order_by):
    principal, policy, envelope = source
    feed, tracker = projections
    if order_by == "created_at":
        # Reverse insertion order to prove the query's timestamp ordering.
        tracker.created_at = feed.created_at - timedelta(days=1)
        expected = [tracker, feed]
    else:
        tracker.created_at = feed.created_at
        expected = sorted(projections, key=lambda projection: str(projection.id))
    disabled = await _add_projection(session, policy, "Disabled")
    disabled.enabled = False
    disabled.created_at = feed.created_at - timedelta(days=2)
    other_policy = await inbound.create_source_policy(
        session,
        org_id=ORG_ID,
        connection_id=CONNECTION_ID,
        name="Later matching policy",
        origin_patterns=[envelope["origin"]],
        envelope_kinds=[envelope["kind"]],
        allowed_actions=[inbound.ACTION_DOMAIN_PROJECTION_UPSERT],
        priority=policy.priority + 1,
    )
    await _add_projection(session, other_policy, "Other policy")
    await session.flush()

    # The admin preview's single-result helper must still return the oldest one.
    assert await inbound._projection_for_policy(session, policy) is expected[0]
    result = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)

    assert result["status"] == inbound.STATUS_PROCESSED
    assert result["error"] is None
    assert result["confidence"] == 1.0
    assert result["matched_policy_id"] == str(policy.id)
    assert result["domain_projection_id"] == str(expected[0].id)
    outcomes = result["ilo_outcome"]["projections"]
    assert [outcome["projection_id"] for outcome in outcomes] == [str(p.id) for p in expected]
    assert [outcome["domain_id"] for outcome in outcomes] == [p.domain_id for p in expected]
    assert all(outcome["status"] == inbound.STATUS_PROCESSED for outcome in outcomes)
    assert all(outcome["result"]["operation"] == "created" for outcome in outcomes)
    records = list(await session.scalars(select(DomainRecord).order_by(DomainRecord.id)))
    assert [record.domain_id for record in records] == [p.domain_id for p in expected]
    assert all(record.data == {
        "external_id": "900", "summary": "Track issue", "status": "open",
    } for record in records)
    keys = list(await session.scalars(select(InboundDomainProjectionKeyRow)))
    assert {key.projection_id for key in keys} == {str(p.id) for p in expected}
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert receipt.outcome == result["ilo_outcome"]
    assert [target["record_id"] for target in receipt.target["projections"]] == [r.id for r in records]

    replay = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)
    assert replay["idempotent_replay"] is True
    assert replay["ilo_outcome"] == result["ilo_outcome"]
    assert len(list(await session.scalars(select(DomainRecord)))) == 2
    assert len(list(await session.scalars(select(InboundDecisionReceiptRow)))) == 1

    envelope = {
        **envelope,
        "idempotency_key": "github:fanout:900:closed",
        "payload": {"issue": {"number": 900, "title": "Track issue", "state": "closed"}},
    }
    updated = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)
    assert [item["result"]["operation"] for item in updated["ilo_outcome"]["projections"]] == [
        "updated", "updated",
    ]
    assert all(record.data["status"] == "closed" and record.version == 2 for record in records)


async def test_single_projection_keeps_exact_result_and_receipt(session, source):
    principal, policy, envelope = source
    projection = await _add_projection(session, policy, "Event Feed")
    result = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)
    record = (await session.scalars(select(DomainRecord))).one()
    key = (await session.scalars(select(InboundDomainProjectionKeyRow))).one()
    event = (await session.scalars(select(InboundEventRow))).one()
    serialized_record = await AsyncDomainService(session).serialize_record(record)
    expected_outcome = {
        "operation": "created",
        "domain_id": projection.domain_id,
        "object_key": "issue",
        "record_id": record.id,
        "external_id": "900",
        "projection_key_id": str(key.id),
        "record": {
            **serialized_record,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        },
    }
    assert result == {
        "status": inbound.STATUS_PROCESSED,
        "event_id": str(event.id),
        "matched_policy_id": str(policy.id),
        "domain_projection_id": str(projection.id),
        "ilo_outcome": expected_outcome,
        "confidence": 1.0,
        "idempotent_replay": False,
        "error": None,
    }
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert receipt.outcome == expected_outcome
    assert receipt.target == {
        "domain_id": projection.domain_id, "object_key": "issue", "record_id": record.id,
    }
    assert receipt.tool_use == {"type": inbound.ACTION_DOMAIN_PROJECTION_UPSERT}
    assert receipt.reasoning_summary == "Configured Domain Projection handled this signal deterministically."
    assert record.title == "Track issue"
    assert record.data == {"external_id": "900", "summary": "Track issue", "status": "open"}


@pytest.mark.parametrize("failed_index", [0, 1])
@pytest.mark.parametrize(("failure_kind", "configured_status", "expected_status"), [
    ("validation", "quarantined", "quarantined"),
    ("validation", "failed", "failed"),
    ("validation", "review_required", "review_required"),
    ("validation", "invalid", "review_required"),
    ("runtime", "quarantined", "failed"),
    ("flush", "review_required", "failed"),
])
async def test_projection_failure_is_isolated_and_attributed(
    session, source, projections, monkeypatch, failed_index,
    failure_kind, configured_status, expected_status,
):
    principal, _, envelope = source
    failed = projections[failed_index]
    healthy = projections[1 - failed_index]
    failed.validation_failure_status = configured_status
    if failure_kind == "validation":
        # This fails after claiming an external-id key, which must also roll back.
        failed.upsert_mode = "update_only"
    apply_projection = inbound._apply_domain_projection

    async def apply_with_failure(session, **kwargs):
        result = await apply_projection(session, **kwargs)
        if kwargs["projection"] is failed:
            if failure_kind == "runtime":
                raise RuntimeError("projection crashed after writing")
            if failure_kind == "flush":
                # Exercise a real failed flush, leaving the savepoint inactive.
                session.add(InboundDomainProjectionKeyRow(
                    org_id=ORG_ID,
                    projection_id=str(failed.id),
                    domain_id=failed.domain_id,
                    external_id="900",
                ))
                await session.flush()
        return result

    monkeypatch.setattr(inbound, "_apply_domain_projection", apply_with_failure)
    triage = AsyncMock(return_value={"status": "skipped", "reason": "test"})
    monkeypatch.setattr(inbound, "_queue_illo_triage", triage)
    await session.flush()

    result = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)
    assert result["status"] == expected_status
    assert result["confidence"] is None
    outcomes = result["ilo_outcome"]["projections"]
    assert len(outcomes) == 2
    assert outcomes[failed_index]["status"] == expected_status
    assert outcomes[failed_index]["projection_id"] == str(failed.id)
    assert outcomes[failed_index]["domain_id"] == failed.domain_id
    assert outcomes[failed_index]["reason"] == (
        "validation_error" if failure_kind == "validation" else "processing_failed"
    )
    assert result["error"] == outcomes[failed_index]["error"]
    assert f"Projection {failed.id} (domain {failed.domain_id}):" in result["error"]
    assert outcomes[1 - failed_index]["status"] == inbound.STATUS_PROCESSED
    assert outcomes[1 - failed_index]["result"]["operation"] == "created"
    assert triage.await_count == (1 if expected_status == "review_required" else 0)
    if expected_status == "review_required":
        assert triage.call_args.kwargs["reasoning_summary"] == result["error"]

    # Only the healthy projection's record, mutation event, and claim survive.
    await session.commit()
    record = (await session.scalars(select(DomainRecord))).one()
    assert record.domain_id == healthy.domain_id
    key = (await session.scalars(select(InboundDomainProjectionKeyRow))).one()
    assert key.projection_id == str(healthy.id)
    assert key.record_id == record.id
    record_events = list(await session.scalars(select(DomainEvent).where(DomainEvent.record_id.is_not(None))))
    assert len(record_events) == 1
    assert record_events[0].record_id == record.id
    event = (await session.scalars(select(InboundEventRow))).one()
    assert event.action_result == result["ilo_outcome"]
    assert event.error == result["error"]
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert receipt.outcome == result["ilo_outcome"]
    assert receipt.reasoning_summary == result["error"]
