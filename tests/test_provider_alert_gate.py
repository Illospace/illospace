"""Regression coverage for the durable provider-alert posting gate (#402)."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable


SEEDREAM_NSFW_ALERT = (
    "ALERT — HIGH provider failure\n"
    "Provider: Seedream/FAL\n"
    "HTTP 422\n"
    "typed reason=nsfw\n"
    "Request rejected before generation."
)
ORG_ID = "4b5e2f59-4f88-4956-a660-4f544ccffbd7"
CHANNEL_ID = "C_ALERTS"


@pytest.mark.parametrize(
    ("body", "classification"),
    [
        (
            "ALERT — LOW provider failure: Vertex HTTP 401 unauthorized",
            "provider_auth_unavailable",
        ),
        (
            "ALERT — LOW provider failure: model call HTTP 504 timeout",
            "provider_timeout",
        ),
        (
            "ALERT — LOW provider failure: Runtime.OutOfMemory (OOM)",
            "provider_resource_exhausted",
        ),
    ],
)
def test_infrastructure_provider_classes_remain_high(body: str, classification: str):
    from brain.platform.provider_alerts import classify_provider_alert_body

    decision = classify_provider_alert_body(body)

    assert decision is not None
    assert decision.severity == "high"
    assert decision.classification == classification
    assert "ALERT — HIGH" in decision.body


def test_seedream_fal_422_with_typed_nsfw_reason_is_content_policy_not_high():
    from brain.platform.provider_alerts import classify_provider_alert_body

    decision = classify_provider_alert_body(SEEDREAM_NSFW_ALERT)

    assert decision is not None
    assert decision.rule_id == "seedream_fal_422_typed_content_policy"
    assert decision.classification == "content_policy"
    assert decision.severity == "low"
    assert "ALERT — HIGH" not in decision.body
    assert "LOW · content-policy" in decision.body


@pytest.mark.parametrize(
    ("suffix", "escalation_reason"),
    [
        ("", "missing_typed_reason"),
        ("typed reason=nsfw\nFallback failed and refund is missing.", "broken_refund_or_fallback"),
        ("typed reason=nsfw\nSafe inputs were misclassified.", "safe_input_misclassified"),
        ("typed reason=nsfw\nOccurrences: 20", "abnormal_volume"),
    ],
)
def test_content_policy_escalates_only_for_configured_exceptions(
    suffix: str,
    escalation_reason: str,
):
    from brain.platform.provider_alerts import classify_provider_alert_body

    body = f"ALERT — LOW provider failure: Seedream/FAL HTTP 422\n{suffix}"
    decision = classify_provider_alert_body(body)

    assert decision is not None
    assert decision.severity == "high"
    assert decision.escalation_reason == escalation_reason


def test_severity_map_is_reloaded_from_durable_config_every_run(tmp_path, monkeypatch):
    from brain.platform.provider_alerts import (
        DEFAULT_PROVIDER_ALERT_POLICY_PATH,
        PROVIDER_ALERT_POLICY_ENV,
        classify_provider_alert_body,
    )

    policy_path = tmp_path / "provider-alert-severity.json"
    policy = json.loads(DEFAULT_PROVIDER_ALERT_POLICY_PATH.read_text(encoding="utf-8"))
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv(PROVIDER_ALERT_POLICY_ENV, str(policy_path))

    first = classify_provider_alert_body(SEEDREAM_NSFW_ALERT)
    assert first is not None and first.severity == "low"
    assert first.policy_source == str(policy_path.resolve())

    policy["rules"][0]["severity"] = "medium"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    second = classify_provider_alert_body(SEEDREAM_NSFW_ALERT)

    assert second is not None and second.severity == "medium"
    assert second.policy_source == str(policy_path.resolve())


def test_provider_alert_gate_has_no_memory_consolidation_dependency():
    import brain.platform.provider_alerts as classifier
    import brain.systems.slack.provider_alert_gate as ledger_gate

    imported_modules: set[str] = set()
    for module in (classifier, ledger_gate):
        tree = ast.parse(inspect.getsource(module))
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )

    assert not any(
        module.startswith(("brain.systems.memory", "brain.systems.reconstructive_memory"))
        for module in imported_modules
    )


class _SlackClient:
    def __init__(self):
        self.posts: list[dict[str, Any]] = []
        self.threads: dict[str, list[dict[str, Any]]] = {}

    async def post_message(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        message_ts = f"1784493000.{len(self.posts):06d}"
        self.threads[message_ts] = [
            {
                "user": "BILLO",
                "bot_id": "BILLO",
                "text": kwargs["text"],
                "ts": message_ts,
            }
        ]
        return {"ok": True, "channel": kwargs["channel"], "ts": message_ts}

    async def conversation_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "messages": list(self.threads.get(kwargs["thread_ts"], [])),
            "response_metadata": {"next_cursor": ""},
        }


@pytest.fixture
async def provider_alert_store(tmp_path, monkeypatch):
    from brain.platform.db.models.provider_alert import ProviderAlertLedger

    database_path = Path(tmp_path) / "provider-alerts.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(CreateTable(ProviderAlertLedger.__table__))

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
        "brain.systems.slack.provider_alert_gate.UnitOfWork",
        TestUnitOfWork,
    )
    try:
        yield sessions
    finally:
        await engine.dispose()


async def _post_provider_alert(monkeypatch, client: _SlackClient) -> dict[str, Any]:
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.slack import _handle_post_slack_reply

    async def slack_client() -> _SlackClient:
        return client

    monkeypatch.setattr(
        "brain.systems.runs.tool_catalog.handlers.slack._slack_client_from_runtime",
        slack_client,
    )
    with bind_agent_context(
        {
            "org_id": ORG_ID,
            "run_id": 402,
            "slack_trigger": {
                "bot_user_id": "BILLO",
                "response_target": {
                    "channel_id": CHANNEL_ID,
                    "thread_ts": None,
                    "visibility": "public",
                },
            },
        }
    ):
        return json.loads(await _handle_post_slack_reply(body=SEEDREAM_NSFW_ALERT))


@pytest.mark.asyncio
async def test_posting_path_rewrites_typed_benign_alert_and_suppresses_duplicate(
    monkeypatch,
    provider_alert_store,
):
    client = _SlackClient()

    first = await _post_provider_alert(monkeypatch, client)
    second = await _post_provider_alert(monkeypatch, client)

    assert first["posted"] is True
    assert first["alert_severity"] == "low"
    assert "ALERT — HIGH" not in client.posts[0]["text"]
    assert "content-policy" in client.posts[0]["text"]
    assert second["posted"] is False
    assert second["suppressed"] is True
    assert second["reason"] == "duplicate_within_throttle"
    assert second["delta_line"] == "still ongoing, +1 since last"
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_human_known_stop_reply_persists_signature_acknowledgement(
    monkeypatch,
    provider_alert_store,
):
    from brain.platform.db.models.provider_alert import ProviderAlertLedger

    client = _SlackClient()
    first = await _post_provider_alert(monkeypatch, client)
    first_ts = first["slack"]["ts"]
    client.threads[first_ts].append(
        {
            "user": "U_REDA",
            "text": "Known and benign — stop alerting on this signature.",
            "ts": "1784493050.000001",
            "user_profile": {"display_name": "Reda"},
        }
    )

    second = await _post_provider_alert(monkeypatch, client)
    third = await _post_provider_alert(monkeypatch, client)

    assert second["reason"] == "signature_acknowledged"
    assert second["acknowledged_by"] == "Reda"
    assert third["reason"] == "signature_acknowledged"
    assert len(client.posts) == 1
    async with provider_alert_store() as session:
        ledger = await session.scalar(select(ProviderAlertLedger))
    assert ledger is not None
    assert ledger.acknowledged_by == "Reda"
    assert "Known and benign" in str(ledger.acknowledgement)
    assert ledger.occurrence_count == 3


@pytest.mark.parametrize(
    "text",
    [
        "This is not known or benign; keep investigating.",
        "Is this a known issue?",
        "There is no known safe explanation.",
    ],
)
def test_acknowledgement_parser_does_not_treat_uncertainty_as_ack(text: str):
    from brain.systems.slack.provider_alert_gate import (
        find_provider_alert_acknowledgement,
    )

    acknowledgement = find_provider_alert_acknowledgement(
        [
            {
                "user": "U1",
                "text": text,
                "ts": "1.0",
            }
        ],
        illo_user_id="BILLO",
    )

    assert acknowledgement is None
