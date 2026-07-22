from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest.mock import patch

import pytest
from sqlalchemy import select

from brain.app.api.routers import launch_handoffs as launch_handoffs_router
from brain.app.api.schemas.launch_handoffs import LaunchHandoffCreateRequest
from brain.platform.db.models.launch_handoff import LaunchHandoff
from brain.systems.launch_handoffs import (
    LaunchHandoffCreateInput,
    LaunchHandoffError,
    _retire_derived_key,
    create_launch_handoff_with_status,
    derive_launch_handoff_idempotency_key,
)
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.launch_handoffs import (
    _handle_create_launch_handoff,
)


_ORG_ID = "11111111-1111-4111-8111-111111111111"
_OWNER_ID = "22222222-2222-4222-8222-222222222222"
_OTHER_OWNER_ID = "33333333-3333-4333-8333-333333333333"
_DERIVED_PREFIX = "derived:launch-handoff:v1:"
_RETIRED_PREFIX = "retired:lh:v1:"


class _UnitOfWork:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await async_sqlite_session_factory([LaunchHandoff.__table__])


def _rollbar_source(item_number: int = 2206, occurrence: int = 1) -> dict:
    return {
        "slack_trigger": {
            "text": (
                f"<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/{item_number}|"
                f"#{item_number} {occurrence}th occurrence: ClientError>"
            ),
            "thread_ts": f"1784700{occurrence}.000001",
        },
        "illo_run_id": occurrence,
    }


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


def _idempotency_state(row: LaunchHandoff) -> dict:
    return row.metadata_["_illo_system"]["idempotency"]


@pytest.mark.parametrize(
    ("source_ref", "owner"),
    [
        ({}, _OWNER_ID),
        ({"slack_trigger": {}}, _OWNER_ID),
        ({"slack_trigger": {"text": None}}, _OWNER_ID),
        (
            {
                "slack_trigger": {
                    "text": {"url": _rollbar_source()["slack_trigger"]["text"]}
                }
            },
            _OWNER_ID,
        ),
        ({"trigger": _rollbar_source()["slack_trigger"]}, _OWNER_ID),
        ({"external_id": "Uwear-API#2206"}, _OWNER_ID),
        ({"url": "https://github.com/Illospace/illospace/issues/409"}, _OWNER_ID),
        ({"slack_trigger": {"text": "ordinary Slack message mentioning #2206"}}, _OWNER_ID),
    ],
)
def test_key_derivation_fails_closed_without_the_rollbar_slack_contract(source_ref, owner):
    assert derive_launch_handoff_idempotency_key(
        source_ref,
        created_by_user_id=owner,
    ) is None


@pytest.mark.parametrize(
    ("occurrence", "owner", "identity_owner"),
    [
        (1, _OWNER_ID, _OWNER_ID),
        (30, _OWNER_ID, _OWNER_ID),
        (30, None, "unassigned"),
    ],
)
def test_key_derivation_uses_rollbar_signature_and_explicit_owner(
    occurrence,
    owner,
    identity_owner,
):
    expected_digest = sha256(
        f"Uwear-API#2206\0{identity_owner}".encode("utf-8")
    ).hexdigest()

    assert derive_launch_handoff_idempotency_key(
        _rollbar_source(occurrence=occurrence),
        created_by_user_id=owner,
    ) == f"{_DERIVED_PREFIX}{expected_digest}"


def test_key_derivation_is_scoped_to_the_created_by_user():
    source_ref = _rollbar_source()

    assert derive_launch_handoff_idempotency_key(
        source_ref,
        created_by_user_id=_OWNER_ID,
    ) != derive_launch_handoff_idempotency_key(
        source_ref,
        created_by_user_id=_OTHER_OWNER_ID,
    )


async def test_replaying_thirty_alerts_for_four_rollbar_items_mints_four_rows(session):
    for alert_index in range(30):
        item_number = 2200 + (alert_index % 4)
        await create_launch_handoff_with_status(
            session,
            _payload(source_ref=_rollbar_source(item_number, alert_index + 1)),
            derive_rollbar_idempotency=True,
        )

    rows = list((await session.scalars(select(LaunchHandoff))).all())
    assert len(rows) == 4
    assert {_idempotency_state(row)["refire_count"] for row in rows} == {6, 7}


async def test_actionable_derived_key_reuses_without_overwriting_the_handoff(session):
    source_ref = _rollbar_source()
    original, created = await create_launch_handoff_with_status(
        session,
        _payload(
            source_ref=source_ref,
            title="Original title",
            instructions="Original instructions",
            metadata={
                "kept": "value",
                "refire_count": "caller-owned",
                "_illo_system": {"idempotency": {"refire_count": 999}},
            },
        ),
        derive_rollbar_idempotency=True,
    )
    original.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    replayed, replay_created = await create_launch_handoff_with_status(
        session,
        _payload(
            source_ref=_rollbar_source(occurrence=2),
            title="Replacement title",
            instructions="Replacement instructions",
            metadata={"replacement": True},
        ),
        derive_rollbar_idempotency=True,
    )

    assert created is True
    assert replay_created is False
    assert replayed is original
    assert original.title == "Original title"
    assert original.instructions == "Original instructions"
    assert original.source_ref == source_ref
    assert original.metadata_["kept"] == "value"
    assert original.metadata_["refire_count"] == "caller-owned"
    assert "replacement" not in original.metadata_
    assert _idempotency_state(original)["refire_count"] == 1
    assert _idempotency_state(original)["last_refire_at"].endswith("+00:00")


@pytest.mark.parametrize(
    ("status", "expires_at"),
    [
        ("launched", None),
        ("open", datetime.now(timezone.utc) - timedelta(seconds=1)),
    ],
)
async def test_non_actionable_derived_holder_is_retired_before_replacement(
    session,
    status,
    expires_at,
):
    holder, _ = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source()),
        derive_rollbar_idempotency=True,
    )
    holder.status = status
    holder.expires_at = expires_at
    await session.flush()
    derived_key = holder.idempotency_key

    replacement, created = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source(occurrence=2)),
        derive_rollbar_idempotency=True,
    )

    assert created is True
    assert replacement is not holder
    assert replacement.idempotency_key == derived_key
    assert holder.idempotency_key.startswith(_RETIRED_PREFIX)
    assert len(holder.idempotency_key) <= 120
    state = _idempotency_state(holder)
    assert state["retired_key"] == derived_key
    assert state["retired_at"].endswith("+00:00")
    assert state["retirement_reason"] == "derived_key_holder_not_actionable"
    assert len((await session.scalars(select(LaunchHandoff))).all()) == 2


async def test_retired_tombstones_are_unique_across_recurrences(session):
    first, _ = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source()),
        derive_rollbar_idempotency=True,
    )
    first.status = "launched"
    second, _ = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source(occurrence=2)),
        derive_rollbar_idempotency=True,
    )
    second.status = "launched"
    await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source(occurrence=3)),
        derive_rollbar_idempotency=True,
    )

    assert first.idempotency_key != second.idempotency_key
    assert first.idempotency_key.startswith(_RETIRED_PREFIX)
    assert second.idempotency_key.startswith(_RETIRED_PREFIX)


@pytest.mark.parametrize(
    "caller_key",
    ["caller:strict-key", f"{_DERIVED_PREFIX}caller-controlled"],
)
def test_retirement_refuses_a_caller_supplied_key(caller_key):
    row = LaunchHandoff(
        id="44444444-4444-4444-8444-444444444444",
        org_id=_ORG_ID,
        title="Caller keyed",
        instructions="Keep strict",
        idempotency_key=caller_key,
        metadata_={},
    )

    with pytest.raises(LaunchHandoffError, match="caller-supplied"):
        _retire_derived_key(row, datetime.now(timezone.utc))

    assert row.idempotency_key == caller_key
    assert row.metadata_ == {}


async def test_explicit_key_remains_strict_after_launch(session):
    explicit_key = "caller:strict-request-key"
    launched, _ = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source(), idempotency_key=explicit_key),
        derive_rollbar_idempotency=True,
    )
    launched.status = "launched"

    replayed, created = await create_launch_handoff_with_status(
        session,
        _payload(source_ref=_rollbar_source(occurrence=2), idempotency_key=explicit_key),
        derive_rollbar_idempotency=True,
    )

    assert created is False
    assert replayed is launched
    assert replayed.idempotency_key == explicit_key
    assert _idempotency_state(replayed)["refire_count"] == 1


async def test_missing_rollbar_identity_keeps_null_key_and_mints_each_time(session):
    source_ref = {
        "external_id": "Uwear-API#2206",
        "record": {"id": 2206, "url": _rollbar_source()["slack_trigger"]["text"]},
    }

    for _ in range(2):
        _row, created = await create_launch_handoff_with_status(
            session,
            _payload(source_ref=source_ref),
            derive_rollbar_idempotency=True,
        )
        assert created is True

    rows = list((await session.scalars(select(LaunchHandoff))).all())
    assert [row.idempotency_key for row in rows] == [None, None]


async def test_tool_handler_opts_into_rollbar_identity_and_requires_evidence(session):
    context = {
        "org_id": _ORG_ID,
        "user_id": _OWNER_ID,
        "run_id": 409,
        "slack_trigger": _rollbar_source()["slack_trigger"],
    }

    with bind_agent_context(context), patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        return_value=_UnitOfWork(session),
    ):
        rejected = json.loads(
            await _handle_create_launch_handoff(
                title="Evidence-free alert",
                instructions="Investigate it.",
                context_parts=[],
                acceptance_criteria=[],
            )
        )
        first = json.loads(
            await _handle_create_launch_handoff(
                title="Alert with evidence",
                instructions="Investigate it.",
                context_parts=[{"source": "rollbar", "ref": "Uwear-API#2206"}],
            )
        )
        replay = json.loads(
            await _handle_create_launch_handoff(
                title="Repeated alert",
                instructions="Investigate it again.",
                acceptance_criteria=["Confirm the failure."],
            )
        )

    assert rejected == {
        "error": "create_launch_handoff requires context_parts or acceptance_criteria evidence"
    }
    assert first["ok"] is True
    assert first["reused"] is False
    assert replay["handoff"]["id"] == first["handoff"]["id"]
    # A refire must be distinguishable from the first mint so the caller can post
    # the annotation line without a second Launch: block (issue #409, check 2).
    assert replay["reused"] is True
    rows = list((await session.scalars(select(LaunchHandoff))).all())
    assert len(rows) == 1
    assert rows[0].source_ref["slack_trigger"] == context["slack_trigger"]
    assert rows[0].idempotency_key.startswith(_DERIVED_PREFIX)


async def test_api_router_does_not_opt_into_rollbar_identity(session):
    payload = LaunchHandoffCreateRequest(
        title="Human-authored sparse handoff",
        instructions="The human will add details after launch.",
        source_ref=_rollbar_source(),
    )

    for _ in range(2):
        result = await launch_handoffs_router.create_launch_handoff(
            payload,
            db=session,
            user={"id": _OWNER_ID, "org_id": _ORG_ID},
        )
        assert result["handoff"]["context_parts"] == []
        assert result["handoff"]["acceptance_criteria"] == []

    rows = list((await session.scalars(select(LaunchHandoff))).all())
    assert [row.idempotency_key for row in rows] == [None, None]
