from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from brain.app.api.routers import launch_handoffs as launch_handoffs_router
from brain.app.api.schemas.launch_handoffs import LaunchHandoffCreateRequest
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.systems.launch_handoffs import (
    LaunchHandoffCreateInput,
    create_launch_handoff_with_status,
    derive_launch_handoff_idempotency_key,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.launch_handoffs import (
    _handle_create_launch_handoff,
)


pytestmark = pytest.mark.asyncio

_ORG_ID = "11111111-1111-4111-8111-111111111111"
_OWNER_ID = "22222222-2222-4222-8222-222222222222"


class _LaunchHandoffSession:
    def __init__(self) -> None:
        self.rows = []
        self.flush_count = 0

    async def scalar(self, statement):
        values = {str(value) for value in statement.compile().params.values()}
        rows = [
            row
            for row in self.rows
            if str(row.org_id) in values and str(row.idempotency_key) in values
        ]
        sql = str(statement)
        if "launch_handoffs.status =" in sql:
            rows = [row for row in rows if row.status == "open"]
        if "launch_handoffs.expires_at IS NULL" in sql:
            now = next(
                value
                for value in statement.compile().params.values()
                if isinstance(value, datetime)
            )
            rows = [
                row
                for row in rows
                if row.expires_at is None or row.expires_at > now
            ]
        if "launch_handoffs.created_at DESC" in sql:
            rows.sort(
                key=lambda row: (row.created_at, str(row.id)),
                reverse=True,
            )
        return rows[0] if rows else None

    def add(self, row) -> None:
        if not row.id:
            row.id = f"handoff-{len(self.rows) + 1}"
        if row.status is None:
            row.status = "open"
        if row.launch_count is None:
            row.launch_count = 0
        self.rows.append(row)

    async def flush(self) -> None:
        self.flush_count += 1


class _UnitOfWork:
    def __init__(self, session: _LaunchHandoffSession) -> None:
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _payload(
    *,
    source_ref: dict,
    title: str = "Investigate Rollbar alert",
    instructions: str = "Fetch the evidence and fix the tracked issue.",
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> LaunchHandoffCreateInput:
    return LaunchHandoffCreateInput(
        org_id=_ORG_ID,
        created_by_user_id=_OWNER_ID,
        title=title,
        instructions=instructions,
        source_ref=source_ref,
        context_parts=[{"source": "rollbar", "ref": "Uwear-API#2206"}],
        acceptance_criteria=["The tracked failure no longer refires."],
        idempotency_key=idempotency_key,
        metadata=metadata or {},
    )


async def test_replaying_thirty_alerts_for_four_rollbar_items_mints_four_rows():
    session = _LaunchHandoffSession()

    for alert_index in range(30):
        item_number = 2200 + (alert_index % 4)
        source_ref = {
            "trigger": {
                "text": (
                    f"<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/{item_number}|"
                    f"#{item_number} {alert_index + 1}th occurrence: ClientError>"
                ),
                "thread_ts": f"1784700{alert_index}.000001",
            },
            "illo_run_id": alert_index + 1000,
        }
        await create_launch_handoff_with_status(session, _payload(source_ref=source_ref))

    assert len(session.rows) == 4
    assert {row.metadata_["refire_count"] for row in session.rows} == {6, 7}


async def test_existing_open_handoff_reports_an_idempotency_hit():
    session = _LaunchHandoffSession()
    source_ref = {"external_id": "github:Illospace/illospace:issue:409"}

    row, created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )
    row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    replayed, replay_created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )

    assert row.status == "open"
    assert replayed is row
    assert created is True
    assert replay_created is False
    assert len(session.rows) == 1


async def test_launched_derived_handoff_does_not_suppress_a_new_mint():
    session = _LaunchHandoffSession()
    source_ref = {"external_id": "github:Illospace/illospace:issue:409"}
    launched, _ = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )
    launched.status = "launched"
    original_refire_count = launched.metadata_.get("refire_count")

    replacement, created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )

    assert created is True
    assert replacement is not launched
    assert replacement.status == "open"
    assert launched.status == "launched"
    assert launched.metadata_.get("refire_count") == original_refire_count
    assert len(session.rows) == 2


async def test_expired_derived_handoff_does_not_suppress_a_new_mint():
    session = _LaunchHandoffSession()
    source_ref = {"external_id": "rollbar:Uwear-API#2206"}
    expired, _ = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )
    expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    original_refire_count = expired.metadata_.get("refire_count")

    replacement, created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )

    assert created is True
    assert replacement is not expired
    assert expired.metadata_.get("refire_count") == original_refire_count
    assert len(session.rows) == 2


async def test_derived_key_reuses_the_most_recent_actionable_handoff():
    session = _LaunchHandoffSession()
    source_ref = {"external_id": "tracked-item-with-duplicate-open-rows"}
    derived_key = derive_launch_handoff_idempotency_key(
        source_ref,
        created_by_user_id=_OWNER_ID,
    )
    assert derived_key is not None
    older = LaunchHandoff(
        id="handoff-older",
        org_id=_ORG_ID,
        created_by_user_id=_OWNER_ID,
        source_ref=source_ref,
        title="Older open handoff",
        instructions="Older instructions",
        idempotency_key=derived_key,
        status="open",
        expires_at=None,
        metadata_={},
    )
    older.created_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    newer = LaunchHandoff(
        id="handoff-newer",
        org_id=_ORG_ID,
        created_by_user_id=_OWNER_ID,
        source_ref=source_ref,
        title="Newer open handoff",
        instructions="Newer instructions",
        idempotency_key=derived_key,
        status="open",
        expires_at=None,
        metadata_={},
    )
    newer.created_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    session.rows.extend([older, newer])

    reused, created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )

    assert created is False
    assert reused is newer
    assert newer.metadata_["refire_count"] == 1
    assert older.metadata_ == {}


async def test_stale_row_releases_derived_key_before_real_database_insert(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory([LaunchHandoff.__table__])
    source_ref = {"external_id": "tracked-item-recurrence"}
    launched, _ = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )
    launched.status = "launched"
    await session.flush()
    derived_key = launched.idempotency_key

    replacement, created = await create_launch_handoff_with_status(
        session, _payload(source_ref=source_ref)
    )

    assert created is True
    assert replacement.idempotency_key == derived_key
    assert launched.idempotency_key is None


async def test_repeated_hits_bump_occurrence_metadata_without_overwriting_the_handoff():
    session = _LaunchHandoffSession()
    source_ref = {"tracked_issue": {"external_id": "Uwear-API#2206"}}
    original, _ = await create_launch_handoff_with_status(
        session,
        _payload(
            source_ref=source_ref,
            title="Original title",
            instructions="Original instructions",
            metadata={"kept": "value"},
        ),
    )

    for replay_index in range(1, 4):
        replayed, created = await create_launch_handoff_with_status(
            session,
            _payload(
                source_ref={**source_ref, "alert_event_id": f"event-{replay_index}"},
                title=f"Replacement title {replay_index}",
                instructions=f"Replacement instructions {replay_index}",
                metadata={"replacement": replay_index},
            ),
        )
        assert replayed is original
        assert created is False
        assert replayed.metadata_["refire_count"] == replay_index
        assert replayed.metadata_["last_refire_at"].endswith("+00:00")

    assert original.title == "Original title"
    assert original.instructions == "Original instructions"
    assert original.source_ref == source_ref
    assert original.metadata_["kept"] == "value"
    assert "replacement" not in original.metadata_


async def test_explicit_idempotency_key_takes_precedence_over_tracked_identity():
    session = _LaunchHandoffSession()

    first, first_created = await create_launch_handoff_with_status(
        session,
        _payload(
            source_ref={"external_id": "ticket-A"},
            idempotency_key="caller:shared-key",
        ),
    )
    second, second_created = await create_launch_handoff_with_status(
        session,
        _payload(
            source_ref={"external_id": "ticket-B"},
            idempotency_key="caller:shared-key",
        ),
    )

    assert first.idempotency_key == "caller:shared-key"
    assert first_created is True
    assert second is first
    assert second_created is False
    assert len(session.rows) == 1


async def test_explicit_key_still_dedupes_against_a_launched_handoff():
    session = _LaunchHandoffSession()
    explicit_key = "caller:strict-request-key"
    launched, _ = await create_launch_handoff_with_status(
        session,
        _payload(source_ref={}, idempotency_key=explicit_key),
    )
    launched.status = "launched"

    replayed, created = await create_launch_handoff_with_status(
        session,
        _payload(source_ref={}, idempotency_key=explicit_key),
    )

    assert created is False
    assert replayed is launched
    assert replayed.status == "launched"
    assert replayed.metadata_["refire_count"] == 1
    assert len(session.rows) == 1


async def test_missing_tracked_identity_preserves_null_key_and_mints_each_time():
    session = _LaunchHandoffSession()

    for run_id in (100, 101):
        _row, created = await create_launch_handoff_with_status(
            session,
            _payload(
                source_ref={
                    "illo_run_id": run_id,
                    "thread_ts": f"1784700{run_id}.000001",
                    "alert_event_id": f"event-{run_id}",
                    "record": {
                        "id": run_id,
                        "title": "Alert event",
                        "status": "open",
                    },
                }
            ),
        )
        assert created is True

    assert len(session.rows) == 2
    assert [row.idempotency_key for row in session.rows] == [None, None]


async def test_derived_key_is_owner_scoped_and_ignores_volatile_source_fields():
    first_ref = {
        "rollbar_item": "Uwear-API#2206",
        "illo_run_id": 1,
        "thread_ts": "1784700000.000001",
    }
    second_ref = {
        "rollbar_item": "uwear-api#2206",
        "illo_run_id": 2,
        "thread_ts": "1784709999.000001",
    }

    first = derive_launch_handoff_idempotency_key(
        first_ref, created_by_user_id=_OWNER_ID
    )
    second = derive_launch_handoff_idempotency_key(
        second_ref, created_by_user_id=_OWNER_ID
    )
    other_owner = derive_launch_handoff_idempotency_key(
        first_ref,
        created_by_user_id="33333333-3333-4333-8333-333333333333",
    )

    assert first == second
    assert first is not None and first.startswith("derived:launch-handoff:v1:")
    assert other_owner != first


async def test_tool_handler_rejects_evidence_free_mint_and_accepts_evidence():
    session = _LaunchHandoffSession()
    uow = _UnitOfWork(session)
    context = {
        "org_id": _ORG_ID,
        "user_id": _OWNER_ID,
        "run_id": 409,
        "slack_trigger": {
            "text": (
                "<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/2206|"
                "#2206 30th occurrence: ClientError>"
            )
        },
    }

    with bind_agent_context(context), patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        return_value=uow,
    ):
        rejected = json.loads(
            await _handle_create_launch_handoff(
                title="Evidence-free alert",
                instructions="Investigate it.",
                context_parts=[],
                acceptance_criteria=[],
            )
        )
        accepted = json.loads(
            await _handle_create_launch_handoff(
                title="Alert with evidence",
                instructions="Investigate it.",
                context_parts=[{"source": "rollbar", "ref": "Uwear-API#2206"}],
                acceptance_criteria=[],
            )
        )

    assert rejected == {
        "error": "create_launch_handoff requires context_parts or acceptance_criteria evidence"
    }
    assert accepted["ok"] is True
    assert len(session.rows) == 1
    assert session.rows[0].source_ref["slack_trigger"] == context["slack_trigger"]
    assert session.rows[0].idempotency_key.startswith("derived:launch-handoff:v1:")


async def test_api_router_still_accepts_an_evidence_free_handoff():
    session = _LaunchHandoffSession()

    result = await launch_handoffs_router.create_launch_handoff(
        LaunchHandoffCreateRequest(
            title="Human-authored sparse handoff",
            instructions="The human will add details after launch.",
        ),
        db=session,
        user={"id": _OWNER_ID, "org_id": _ORG_ID},
    )

    assert result["handoff"]["context_parts"] == []
    assert result["handoff"]["acceptance_criteria"] == []
    assert len(session.rows) == 1
