"""Regression coverage for provider-alert surge handling (#410)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable


ORG_ID = "4b5e2f59-4f88-4956-a660-4f544ccffbd7"
ALERTS_CHANNEL_ID = "C_ALERTS"
OUTAGE_START = datetime(2026, 7, 21, 16, 40, tzinfo=timezone.utc)


def _rollbar_alert(item: int, marker: str, title: str) -> str:
    return (
        "<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/"
        f"{item}|#{item} {marker}: {title}>"
    )


class _SlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.threads: dict[str, list[dict[str, Any]]] = {}

    async def conversations_list(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "channels": [{"id": "C_SOFTWARE", "name": "4_software"}],
            "response_metadata": {"next_cursor": ""},
        }

    async def post_message(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        response = {
            "ok": True,
            "channel": kwargs["channel"],
            "ts": f"1784650000.{len(self.posts):06d}",
        }
        self.threads[response["ts"]] = []
        return response

    async def conversation_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "messages": self.threads.get(kwargs["thread_ts"], []),
            "response_metadata": {"next_cursor": ""},
        }


@pytest.fixture
async def surge_store(tmp_path, monkeypatch):
    from brain.platform.db.models.provider_alert import (
        ProviderAlertLedger,
        ProviderAlertOccurrence,
        ProviderAlertSurge,
    )

    database_path = Path(tmp_path) / "provider-alert-surges.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(CreateTable(ProviderAlertLedger.__table__))
        await connection.execute(CreateTable(ProviderAlertOccurrence.__table__))
        await connection.execute(CreateTable(ProviderAlertSurge.__table__))

    class TestUnitOfWork:
        async def __aenter__(self):
            self.session = sessions()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type:
                await self.session.rollback()
            else:
                await self.session.commit()
            await self.session.close()

    monkeypatch.setattr(
        "brain.systems.slack.provider_alert_surge.UnitOfWork",
        TestUnitOfWork,
    )
    monkeypatch.setattr(
        "brain.systems.slack.provider_alert_gate.UnitOfWork",
        TestUnitOfWork,
    )
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replay_posts_exactly_one_material_incident_by_eighth_alert(surge_store):
    """Acceptance 1: replay the outage across four Rollbar item ids."""

    from brain.systems.slack.provider_alert_surge import (
        handle_provider_alert_ingest_durable,
    )

    timeout = "TimeoutError: Garment description generation timed out"
    concurrency = "TimeoutError: Description LLM concurrency budget exhausted"
    sequence = [
        (2278, "New error", timeout),
        (2279, "New error", timeout),
        (2278, "2nd error", timeout),
        (2279, "2nd error", timeout),
        (2278, "3rd error", timeout),
        (2279, "3rd error", timeout),
        (2278, "4th error", timeout),
        (2279, "4th error", timeout),
        (2278, "5th error", timeout),
        (2279, "5th error", timeout),
        (2293, "10th error", concurrency),
        (2295, "New error", concurrency),
        (2278, "100th error", timeout),
    ]
    while len(sequence) < 30:
        item = (2278, 2279, 2293, 2295)[len(sequence) % 4]
        title = concurrency if item in {2293, 2295} else timeout
        sequence.append((item, f"{len(sequence) + 1}th error", title))

    client = _SlackClient()
    material_post_indexes: list[int] = []
    for index, (item, marker, title) in enumerate(sequence, start=1):
        minute = round((index - 1) * 60 / (len(sequence) - 1))
        result = await handle_provider_alert_ingest_durable(
            client,
            org_id=ORG_ID,
            channel_id=ALERTS_CHANNEL_ID,
            message_ts=f"178464{index:04d}.000000",
            text=_rollbar_alert(item, marker, title),
            occurred_at=OUTAGE_START + timedelta(minutes=minute),
        )
        if result is not None and result.material_posted:
            material_post_indexes.append(index)

    replayed = await handle_provider_alert_ingest_durable(
        client,
        org_id=ORG_ID,
        channel_id=ALERTS_CHANNEL_ID,
        message_ts="1784640008.000000",
        text=_rollbar_alert(*sequence[7]),
        occurred_at=OUTAGE_START + timedelta(minutes=14),
    )

    assert material_post_indexes == [8]
    assert replayed is not None and replayed.material_posted is False
    assert len(client.posts) == 1
    assert client.posts[0]["channel"] == "C_SOFTWARE"
    assert "Material incident — alert surge" in client.posts[0]["text"]
    assert "Subsystem: Uwear-API" in client.posts[0]["text"]
    assert "Owner: Uwear engineering on-call" in client.posts[0]["text"]
    assert "Next action:" in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_new_error_title_is_a_distinct_tracked_signature(surge_store):
    """Acceptance 2: a distinct New error title is never folded into timeout."""

    from brain.platform.db.models.provider_alert import ProviderAlertOccurrence
    from brain.platform.provider_alerts import (
        classify_provider_alert_body,
        classify_provider_alert_ingest,
    )
    from brain.systems.slack.provider_alert_surge import handle_provider_alert_ingest

    timeout = _rollbar_alert(
        2278,
        "New error",
        "TimeoutError: Garment description generation timed out",
    )
    concurrency_2293 = _rollbar_alert(
        2293,
        "10th error",
        "TimeoutError: Description LLM concurrency budget exhausted",
    )
    concurrency_2295 = _rollbar_alert(
        2295,
        "New error",
        "TimeoutError: Description LLM concurrency budget exhausted",
    )

    timeout_alert = classify_provider_alert_ingest(timeout)
    concurrency_alert = classify_provider_alert_ingest(concurrency_2295)
    assert timeout_alert is not None and concurrency_alert is not None
    assert concurrency_alert.is_new_error is True
    assert timeout_alert.signature != concurrency_alert.signature
    timeout_reply = classify_provider_alert_body(
        f"ALERT — HIGH provider failure\n{timeout}"
    )
    concurrency_reply = classify_provider_alert_body(
        f"ALERT — HIGH provider failure\n{concurrency_2295}"
    )
    concurrency_refire_reply = classify_provider_alert_body(
        f"ALERT — HIGH provider failure\n{concurrency_2293}"
    )
    assert timeout_reply is not None and concurrency_reply is not None
    assert concurrency_refire_reply is not None
    assert timeout_reply.signature != concurrency_reply.signature
    assert concurrency_refire_reply.signature == concurrency_reply.signature
    assert concurrency_reply.evidence.is_new_error is True

    client = _SlackClient()
    async with surge_store() as session:
        for index, text in enumerate((timeout, concurrency_2293, concurrency_2295), start=1):
            await handle_provider_alert_ingest(
                session,
                client,
                org_id=ORG_ID,
                channel_id=ALERTS_CHANNEL_ID,
                message_ts=f"178464100{index}.000000",
                text=text,
                occurred_at=OUTAGE_START + timedelta(minutes=index),
            )
        await session.commit()
        rows = list(
            (
                await session.scalars(
                    select(ProviderAlertOccurrence).order_by(
                        ProviderAlertOccurrence.occurred_at.asc()
                    )
                )
            ).all()
        )

    assert rows[0].signature != rows[1].signature
    assert rows[1].signature == rows[2].signature
    assert rows[1].signature_title == "TimeoutError: Description LLM concurrency budget exhausted"
    assert rows[1].is_new_signature is True
    assert rows[2].is_new_signature is False


@pytest.mark.asyncio
async def test_digest_query_returns_open_surge_and_skill_requires_lead_section(surge_store):
    """Acceptance 3: the digest has a queryable incident to render first."""

    from brain.systems.slack.provider_alert_surge import (
        handle_provider_alert_ingest,
        list_open_provider_alert_surges,
    )

    client = _SlackClient()
    async with surge_store() as session:
        for index in range(8):
            await handle_provider_alert_ingest(
                session,
                client,
                org_id=ORG_ID,
                channel_id=ALERTS_CHANNEL_ID,
                message_ts=f"178464200{index}.000000",
                text=_rollbar_alert(
                    2278 + (index % 2),
                    "New error" if index < 2 else f"{index + 1}th error",
                    "TimeoutError: Garment description generation timed out",
                ),
                occurred_at=OUTAGE_START + timedelta(minutes=index),
            )
        await session.commit()

        open_surges = await list_open_provider_alert_surges(
            session,
            org_id=ORG_ID,
            now=OUTAGE_START + timedelta(minutes=22),
        )
        closed_surges = await list_open_provider_alert_surges(
            session,
            org_id=ORG_ID,
            now=OUTAGE_START + timedelta(minutes=38),
        )

    assert len(open_surges) == 1
    assert open_surges[0]["subsystem"] == "Uwear-API"
    assert open_surges[0]["message_count"] == 8
    assert open_surges[0]["trigger_reason"] == "message_volume"
    assert closed_surges == []

    skill = Path(
        "brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md"
    ).read_text(encoding="utf-8")
    assert 'manage_slack` `action="open_alert_surges"' in skill
    assert "lead incident section" in skill


@pytest.mark.asyncio
async def test_digest_can_read_open_surges_through_manage_slack(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_manage_slack
    from brain.systems.runs.tool_catalog.registry import action_policy_for_tool

    expected = [{"subsystem": "Uwear-API", "trigger_reason": "message_volume"}]

    class _UnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def _open_surges(session, *, org_id, **_kwargs):
        assert session is _UnitOfWork.session
        assert org_id == ORG_ID
        return expected

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _UnitOfWork,
    )
    monkeypatch.setattr(
        "brain.systems.slack.provider_alert_surge.list_open_provider_alert_surges",
        _open_surges,
    )

    with bind_agent_context({"org_id": ORG_ID}):
        payload = json.loads(await _handle_manage_slack(action="open_alert_surges"))

    assert payload == {"ok": True, "open_alert_surges": expected, "count": 1}
    assert (
        action_policy_for_tool(
            "manage_slack",
            kwargs={"action": "open_alert_surges"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_quiet_single_refire_keeps_annotation_without_material_incident(
    surge_store,
    monkeypatch,
):
    """Acceptance 4: one ordinary refire keeps the per-alert-only behavior."""

    from brain.systems.slack.provider_alert_surge import handle_provider_alert_ingest

    client = _SlackClient()
    async with surge_store() as session:
        result = await handle_provider_alert_ingest(
            session,
            client,
            org_id=ORG_ID,
            channel_id=ALERTS_CHANNEL_ID,
            message_ts="1784643000.000000",
            text=_rollbar_alert(
                2278,
                "2nd error",
                "TimeoutError: Garment description generation timed out",
            ),
            occurred_at=OUTAGE_START,
        )
        await session.commit()

    assert result is not None
    assert result.surge is None
    assert result.material_posted is False
    assert client.posts == []

    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    async def _slack_client():
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        _slack_client,
    )
    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 410,
            "slack_trigger": {
                "bot_user_id": "BILLO",
                "response_target": {
                    "channel_id": ALERTS_CHANNEL_ID,
                    "thread_ts": "1784643000.000000",
                    "visibility": "public",
                },
            },
        }
    ):
        annotation = json.loads(
            await _handle_post_slack_reply(
                body=(
                    "ALERT — HIGH provider failure\n"
                    "Provider: Vertex\nHTTP 504\n"
                    "Quiet refire annotated on the existing incident."
                )
            )
        )

    assert annotation["posted"] is True
    assert len(client.posts) == 1
    assert client.posts[0]["thread_ts"] == "1784643000.000000"


@pytest.mark.asyncio
async def test_message_volume_predicate_uses_a_rolling_window(surge_store):
    from brain.systems.slack.provider_alert_surge import handle_provider_alert_ingest

    client = _SlackClient()
    offsets = [0, 1, 2, 3, 4, 5, 6, 37]
    async with surge_store() as session:
        for index, minute in enumerate(offsets):
            result = await handle_provider_alert_ingest(
                session,
                client,
                org_id=ORG_ID,
                channel_id=ALERTS_CHANNEL_ID,
                message_ts=f"178464350{index}.000000",
                text=_rollbar_alert(
                    2278,
                    f"{index + 2}th error",
                    "TimeoutError: Garment description generation timed out",
                ),
                occurred_at=OUTAGE_START + timedelta(minutes=minute),
            )
            assert result is not None and result.surge is None
        await session.commit()

    assert client.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alerts", "expected_reason"),
    [
        (
            [
                (2278, "100th error", "TimeoutError: generation timed out"),
            ],
            "occurrence_milestone",
        ),
        (
            [
                (2293, "New error", "TimeoutError: generation timed out"),
                (2295, "New error", "RuntimeError: concurrency budget exhausted"),
            ],
            "distinct_new_signatures",
        ),
    ],
)
async def test_non_volume_surge_predicates(alerts, expected_reason, surge_store):
    from brain.systems.slack.provider_alert_surge import handle_provider_alert_ingest

    client = _SlackClient()
    last_result = None
    async with surge_store() as session:
        for index, (item, marker, title) in enumerate(alerts):
            last_result = await handle_provider_alert_ingest(
                session,
                client,
                org_id=ORG_ID,
                channel_id=ALERTS_CHANNEL_ID,
                message_ts=f"178464400{index}.000000",
                text=_rollbar_alert(item, marker, title),
                occurred_at=OUTAGE_START + timedelta(minutes=index),
            )
        await session.commit()

    assert last_result is not None and last_result.surge is not None
    assert last_result.surge.trigger_reason == expected_reason
    assert len(client.posts) == 1


def test_surge_thresholds_are_loaded_from_named_config():
    from brain.platform.provider_alerts import provider_alert_surge_policy

    policy = provider_alert_surge_policy()

    assert policy.message_threshold == 8
    assert policy.window_minutes == 30
    assert policy.milestone_threshold == 100
    assert policy.new_signature_threshold == 2
    assert policy.material_channel == "#4_software"


@pytest.mark.asyncio
async def test_monitored_connector_records_alert_before_run_admission(monkeypatch):
    from brain.systems.slack.connector import (
        SlackConnectorConfig,
        _handle_monitored_provider_alert,
    )

    captured: dict[str, Any] = {}

    async def _handle(client, **kwargs):
        captured.update({"client": client, **kwargs})
        return SimpleNamespace(
            alert=SimpleNamespace(
                service="Uwear-API",
                subsystem="Uwear-API",
                external_id="Uwear-API#2295",
                signature="tracked-signature",
                signature_title="Description LLM concurrency budget exhausted",
                occurrence_milestone=None,
                is_new_error=True,
            ),
            surge=object(),
            material_posted=True,
            material_post_error=None,
        )

    monkeypatch.setattr(
        "brain.systems.slack.provider_alert_surge.handle_provider_alert_ingest_durable",
        _handle,
    )
    envelope = {
        "payload": {
            "channel_id": ALERTS_CHANNEL_ID,
            "message_ts": "1784643000.000000",
            "text": _rollbar_alert(
                2295,
                "New error",
                "Description LLM concurrency budget exhausted",
            ),
        }
    }
    await _handle_monitored_provider_alert(
        connection={"org_id": ORG_ID},
        config=SlackConnectorConfig(bot_token="xoxb-test", app_token="xapp-test"),
        envelope=envelope,
    )

    assert captured["org_id"] == ORG_ID
    assert envelope["payload"]["provider_alert"] == {
        "service": "Uwear-API",
        "subsystem": "Uwear-API",
        "external_id": "Uwear-API#2295",
        "tracked_signature": "tracked-signature",
        "signature_title": "Description LLM concurrency budget exhausted",
        "occurrence_milestone": None,
        "is_new_error": True,
        "surge_open": True,
        "material_posted": True,
        "material_post_error": None,
    }
