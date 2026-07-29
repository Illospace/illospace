"""Behavioral coverage for thread-level Slack knowledge ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.inbound import InboundEventRow
from brain.platform.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemEmbedding,
    KnowledgeSyncState,
)
from brain.platform.db.models.org import Org, User
from brain.jobs.pipelines.knowledge_index_sync import CONNECTOR_FACTORIES
from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.systems.knowledge.connectors.slack import SlackKnowledgeConnector
from brain.systems.knowledge.service import sync_connector
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig


_ORG_ID = "11111111-1111-4111-8111-111111111111"
_USER_ID = "22222222-2222-4222-8222-222222222222"
_CONNECTION_ID = "33333333-3333-4333-8333-333333333333"


def test_slack_connector_runs_in_the_shared_knowledge_sync_pipeline():
    assert SlackKnowledgeConnector in CONNECTOR_FACTORIES


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            InboundEventRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            KnowledgeItem.__table__,
            KnowledgeItemEmbedding.__table__,
            KnowledgeSyncState.__table__,
        ]
    )


async def _seed_monitored_slack(session) -> None:
    session.add(Org(id=_ORG_ID, name="Test Org", slug="test-org"))
    session.add(
        User(
            id=_USER_ID,
            org_id=_ORG_ID,
            name="Reda",
            email="reda@example.com",
        )
    )
    session.add(
        ExternalAgentConnectionRow(
            id=_CONNECTION_ID,
            org_id=_ORG_ID,
            owner_user_id=_USER_ID,
            display_name="Slack",
            agent_kind="slack",
            transport="slack_socket_mode",
            status="online",
            remote_agent_id="T789",
            remote_agent_card={},
            capabilities={"slack": {"socket_mode": True}},
            auth_metadata={
                "bot_token_ref": "runtime:SLACK_BOT_TOKEN",
                "app_token_ref": "runtime:SLACK_APP_TOKEN",
            },
            metadata_={
                "slack": {
                    "team_id": "T789",
                    "monitored_channels": [
                        {
                            "channel_id": "CINCIDENTS",
                            "channel_name": "incidents",
                            "enabled": True,
                        }
                    ],
                }
            },
        )
    )
    await session.flush()


class _SlackHistory:
    def __init__(self) -> None:
        self.messages = [
            {"ts": "1785283200.000100", "user": "U1", "text": "Checkout is failing"},
            {"ts": "1785283260.000200", "user": "U2", "text": "Payment API is timing out"},
        ]
        self.history_calls: list[dict] = []
        self.reply_calls: list[str | None] = []

    async def conversation_history(self, **kwargs):
        self.history_calls.append(dict(kwargs))
        return {
            "messages": [
                {
                    **self.messages[0],
                    "reply_count": len(self.messages) - 1,
                    "latest_reply": self.messages[-1]["ts"],
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }

    async def conversation_replies(
        self,
        *,
        channel,
        thread_ts,
        limit,
        cursor=None,
    ):
        del channel, thread_ts, limit
        self.reply_calls.append(cursor)
        if cursor is None:
            return {
                "messages": [self.messages[0]],
                "response_metadata": {"next_cursor": "replies-2"},
            }
        return {
            "messages": list(self.messages[1:]),
            "response_metadata": {"next_cursor": ""},
        }


class _PagedSlackHistory:
    def __init__(self) -> None:
        self.roots = [
            {"ts": "1785283200.000100", "user": "U1", "text": "First thread"},
            {"ts": "1785283260.000200", "user": "U2", "text": "Second thread"},
        ]

    async def conversation_history(self, *, channel, limit, cursor=None):
        del channel
        assert limit == 1
        if cursor is None:
            return {
                "messages": [self.roots[0]],
                "response_metadata": {"next_cursor": "history-2"},
            }
        assert cursor == "history-2"
        return {
            "messages": [self.roots[1]],
            "response_metadata": {"next_cursor": ""},
        }

    async def conversation_replies(self, *, channel, thread_ts, limit, cursor=None):
        del channel, limit
        assert cursor is None
        return {
            "messages": [root for root in self.roots if root["ts"] == thread_ts],
            "response_metadata": {"next_cursor": ""},
        }


@pytest.fixture
def embedding_runtime(monkeypatch):
    from brain.systems.memory import embeddings as embedding_client
    from brain.systems.runtime_settings import memory as runtime_settings

    runtime = EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="test-knowledge-embedding",
        cpu_model="unused",
        dimensions=KNOWLEDGE_EMBEDDING_DIM,
        api_key="test-key",
    )

    async def fake_runtime_config(session, *, include_secret=True):
        del session, include_secret
        return runtime

    vector = np.zeros(KNOWLEDGE_EMBEDDING_DIM, dtype=np.float32)
    vector[0] = 1.0
    monkeypatch.setattr(
        runtime_settings,
        "async_get_embedding_runtime_config",
        fake_runtime_config,
    )
    monkeypatch.setattr(
        embedding_client,
        "embed_document",
        lambda text, runtime_config=None: vector,
    )
    return runtime


async def test_slack_backfill_is_bounded_and_reports_each_page_through_shared_stats(
    session,
    embedding_runtime,
):
    del embedding_runtime
    await _seed_monitored_slack(session)
    connector = SlackKnowledgeConnector(client=_PagedSlackHistory(), max_items=1)

    first_dispatch = await sync_connector(session, connector)
    first_run = (await session.scalars(select(AgentRunRow))).one()
    first_run.status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=first_run.id,
            root_run_id=first_run.root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "What happened in the first thread?",
                    "summary": "The first Slack thread was distilled.",
                    "resolution": None,
                    "systems": ["slack"],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()
    first = await sync_connector(session, connector)

    second_dispatch = await sync_connector(session, connector)
    second_run = (
        await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id.desc()))
    ).first()
    assert second_run is not None
    second_run.status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=second_run.id,
            root_run_id=second_run.root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "What happened in the second thread?",
                    "summary": "The second Slack thread was distilled.",
                    "resolution": None,
                    "systems": ["slack"],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()
    second = await sync_connector(session, connector)

    assert first_dispatch.status == "pending"
    assert first_dispatch.stats["pending"] == 1
    assert first.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
        "distilled": 1,
    }
    assert first.cursor["phase"] == "backfill"
    assert first.cursor["history_cursor"] == "history-2"
    assert second_dispatch.status == "pending"
    assert second_dispatch.stats["pending"] == 1
    assert second.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
        "distilled": 1,
    }
    assert second.cursor["phase"] == "incremental"
    assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 2
    state = await session.get(KnowledgeSyncState, "slack")
    assert state.cursor == second.cursor
    assert state.last_stats == second.stats
    rows = list(
        (
            await session.scalars(
                select(KnowledgeItem).order_by(KnowledgeItem.source_ref)
            )
        ).all()
    )
    assert {row.extra["org_id"] for row in rows} == {_ORG_ID}
    assert {row.extra["actor_user_id"] for row in rows} == {_USER_ID}


async def test_slack_connector_does_not_inherit_the_monitor_prompt_text_cap(session):
    await _seed_monitored_slack(session)
    slack = _SlackHistory()
    slack.messages = [
        {
            "ts": "1785283200.000100",
            "user": "U1",
            "text": f"{'x' * 5000}TAIL_MARKER",
        }
    ]

    drafts, _cursor = await SlackKnowledgeConnector(
        client=slack,
        max_items=1,
    ).enumerate_changed(session, {})

    assert len(drafts) == 1
    assert drafts[0].raw_text.endswith("TAIL_MARKER")
    assert len(drafts[0].raw_text) > 5000


async def test_slack_connector_reuses_runtime_transport_under_connection_authority(
    session,
    monkeypatch,
):
    from brain.systems.slack import client as slack_client

    await _seed_monitored_slack(session)
    calls: list[dict] = []

    async def fake_runtime_client(**kwargs):
        calls.append(dict(kwargs))
        return _SlackHistory()

    monkeypatch.setattr(
        slack_client,
        "slack_web_client_from_runtime",
        fake_runtime_client,
    )

    drafts, _cursor = await SlackKnowledgeConnector(max_items=1).enumerate_changed(
        session,
        {},
    )

    assert len(drafts) == 1
    assert calls == [
        {
            "requested_by": "knowledge_index_sync",
            "reason": "Backfill and incrementally refresh monitored Slack threads.",
            "org_id": _ORG_ID,
            "owner_user_id": _USER_ID,
        }
    ]


async def test_slack_connector_repulls_one_complete_thread_when_a_reply_arrives(session):
    await _seed_monitored_slack(session)
    slack = _SlackHistory()
    connector = SlackKnowledgeConnector(client=slack, max_items=1)

    first, cursor = await connector.enumerate_changed(session, {})

    assert len(first) == 1
    assert first[0].source_ref == "slack:T789:CINCIDENTS:1785283200.000100"
    assert first[0].kind == "slack_thread"
    assert first[0].distill is True
    assert first[0].raw_text == (
        "[2026-07-29T00:00:00.000100+00:00] U1: Checkout is failing\n"
        "[2026-07-29T00:01:00.000200+00:00] U2: Payment API is timing out"
    )
    assert first[0].extra["participants"] == ["U1", "U2"]
    assert first[0].extra["message_count"] == 2
    assert first[0].source_updated_at == datetime(
        2026, 7, 29, 0, 1, tzinfo=timezone.utc
    ).replace(microsecond=200)
    assert cursor == {
        "version": 1,
        "connection_id": _CONNECTION_ID,
        "channel_set_digest": hashlib.sha256(b"CINCIDENTS").hexdigest(),
        "phase": "incremental",
        "channel_id": None,
        "history_cursor": None,
        "event_created_at": None,
        "event_id": None,
    }

    slack.messages.append(
        {
            "ts": "1785283320.000300",
            "user": "U1",
            "text": "Resolved by raising the upstream timeout",
        }
    )
    session.add(
        InboundEventRow(
            id="44444444-4444-4444-8444-444444444444",
            org_id=_ORG_ID,
            connection_id=_CONNECTION_ID,
            kind="slack_message",
            origin="slack.channel_message",
            idempotency_key="slack:T789:CINCIDENTS:1785283320.000300",
            raw_payload={},
            normalized_payload={
                "kind": "slack_message",
                "origin": "slack.channel_message",
                "payload": {
                    "team_id": "T789",
                    "channel_id": "CINCIDENTS",
                    "thread_ts": "1785283200.000100",
                    "message_ts": "1785283320.000300",
                },
            },
            envelope={},
            ingress_context={},
            source_actor={},
            status="processed",
            created_at=datetime(2026, 7, 29, 0, 2, tzinfo=timezone.utc),
        )
    )
    await session.flush()

    second, new_cursor = await connector.enumerate_changed(session, cursor)

    assert len(second) == 1
    assert second[0].source_ref == first[0].source_ref
    assert second[0].extra["message_count"] == 3
    assert "Resolved by raising the upstream timeout" in second[0].raw_text
    assert slack.reply_calls == [None, "replies-2", None, "replies-2"]
    assert new_cursor["phase"] == "incremental"
    assert new_cursor["event_created_at"] == "2026-07-29T00:02:00+00:00"
    assert new_cursor["event_id"] == "44444444-4444-4444-8444-444444444444"

    # Illo's own Slack posts are intentionally ignored by monitored intake.
    # The bounded refresh sweep still makes the canonical thread complete.
    slack.messages.append(
        {
            "ts": "1785283380.000400",
            "user": "BILLO",
            "text": "Confirmed healthy after the timeout change",
        }
    )

    refreshed, refresh_cursor = await connector.enumerate_changed(session, new_cursor)

    assert len(refreshed) == 1
    assert refreshed[0].extra["message_count"] == 4
    assert refreshed[0].extra["participants"] == ["BILLO", "U1", "U2"]
    assert "Confirmed healthy after the timeout change" in refreshed[0].raw_text
    assert refresh_cursor["phase"] == "incremental"
