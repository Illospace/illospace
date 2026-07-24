from __future__ import annotations

import importlib.util
import os
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
    ExternalAgentTaskArtifactRow,
    ExternalAgentTaskEventRow,
    ExternalAgentTaskRow,
)
from brain.systems.external_agents import service
from brain.platform.db.models.idea import (
    Idea,
    IdeaProjectAttachment,
    IdeaThread,
    ProjectProfile,
    ProjectProfileAccess,
    UserMention,
)
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NotificationEvent,
)
from brain.platform.db.models.org import Org, User


ORG_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
OWNER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
TEAMMATE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"


def _patch_sqlite_for_external_agent_tables():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_external_agent_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._external_agent_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


def _load_bridge_module():
    bridge_path = Path(__file__).resolve().parents[1] / "tools" / "personal-agent-bridge" / "bridge.py"
    spec = importlib.util.spec_from_file_location("personal_agent_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bridge_token_helpers_are_stable_and_scoped():
    token = service.generate_connection_token()

    assert token.startswith("illo_conn_")
    assert service.token_prefix(token) == token[:18]
    assert service.hash_connection_token(token) == service.hash_connection_token(token)
    assert service.hash_connection_token(token) != service.hash_connection_token(token + "x")
    assert {
        service.SCOPE_TASK_CLAIM,
        service.SCOPE_TASK_COMPLETE,
        service.SCOPE_WORKSPACE_READ,
        service.SCOPE_DOMAIN_WRITE,
        service.SCOPE_ILLO_ASK,
        service.SCOPE_ILLO_THREAD_CREATE,
        service.SCOPE_SIGNAL_SUBMIT,
    }.issubset(set(service.DEFAULT_BRIDGE_SCOPES))


def test_headless_thread_ids_use_external_agent_namespace():
    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )

    assert f"external-agent:{principal.connection_id}:ask-1" == "external-agent:conn-1:ask-1"


@pytest.mark.asyncio
async def test_headless_ask_blocks_thread_mutation_tools():
    from brain.systems.runs.work_intake import WorkIntakeResult

    class FakeSession:
        def __init__(self):
            self.added = []
            self.flush_count = 0

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            self.flush_count += 1

        async def scalar(self, _stmt):
            return None

    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )
    captured_events = []

    async def fake_admit_work(_session, event):
        captured_events.append(event)
        return WorkIntakeResult(ok=True, run_id=42)

    with patch("brain.systems.external_agents.service.admit_work", side_effect=fake_admit_work):
        task = await service.create_headless_ask(
            FakeSession(),
            principal,
            question="What should I know before replying?",
        )

    assert task.illo_run_id == 42
    event = captured_events[0]
    assert event.source == "external_agent"
    assert event.event_type == "external_agent.headless_ask"
    assert event.target["kind"] == "external_agent_headless_ask"
    blocked_tools = event.payload["metadata"]["tool_policy"]["blocked_tools"]
    assert "manage_idea" in blocked_tools
    assert "post_chat_message" in blocked_tools
    assert "post_ai_timeline_message" in blocked_tools
    assert "post_thread_discussion_reply" in blocked_tools
    request_source = event.payload["metadata"]["request_source"]
    assert request_source["surface"] == "personal_agent_bridge"
    assert request_source["personal_agent"] == "Hermes"
    assert request_source["visibility"] == "headless_private"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "metadata", "skill_tier", "expected_effort", "expected_skill_lookups"),
    [
        ("xhigh", {"skill_name": "anchored-skill"}, "low", "xhigh", 0),
        (None, {"skill_name": "anchored-skill"}, "low", "low", 1),
        (None, {}, "low", "medium", 0),
    ],
)
async def test_headless_ask_effort_precedence(
    effort,
    metadata,
    skill_tier,
    expected_effort,
    expected_skill_lookups,
):
    from brain.systems.runs.work_intake import WorkIntakeResult

    class FakeSession:
        def __init__(self):
            self.skill_lookups = 0

        def add(self, _row):
            return None

        async def flush(self):
            return None

        async def scalar(self, stmt):
            if "skills" in str(stmt):
                self.skill_lookups += 1
                return skill_tier
            return None

    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )
    captured_events = []

    async def fake_admit_work(_session, event):
        captured_events.append(event)
        return WorkIntakeResult(ok=True, run_id=42)

    session = FakeSession()
    with patch("brain.systems.external_agents.service.admit_work", side_effect=fake_admit_work):
        await service.create_headless_ask(
            session,
            principal,
            question="Run the requested work",
            metadata=metadata,
            effort=effort,
        )

    model_policy = captured_events[0].payload["model_policy"]
    assert model_policy == {"thinking": expected_effort}
    assert "tier" not in model_policy
    assert session.skill_lookups == expected_skill_lookups


@pytest.mark.asyncio
async def test_headless_ask_whitelist_keeps_security_stamps_forced():
    from brain.systems.runs.work_intake import WorkIntakeResult

    class FakeSession:
        def add(self, _row):
            return None

        async def flush(self):
            return None

        async def scalar(self, _stmt):
            return None

    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )
    captured_events = []

    async def fake_admit_work(_session, event):
        captured_events.append(event)
        return WorkIntakeResult(ok=True, run_id=42)

    with patch("brain.systems.external_agents.service.admit_work", side_effect=fake_admit_work):
        await service.create_headless_ask(
            FakeSession(),
            principal,
            question="Do not trust caller control metadata",
            effort="low",
            metadata={
                "headless": False,
                "execution_profile": "deep",
                "recipe": "deep",
                "tool_policy": {"mode": "allow_all", "blocked_tools": []},
                "effort": "xhigh",
                "thinking_tier": "xhigh",
                "model_policy": {"thinking": "xhigh"},
            },
        )

    event = captured_events[0]
    run_metadata = event.payload["metadata"]
    assert event.payload["model_policy"] == {"thinking": "low"}
    assert run_metadata["headless"] is True
    assert run_metadata["execution_profile"] == "fast"
    assert run_metadata["recipe"] == "fast"
    assert run_metadata["tool_policy"] == {
        "mode": "read_mostly",
        "blocked_tools": list(service.HEADLESS_ASK_BLOCKED_TOOLS),
    }


@pytest.fixture
async def external_agent_session(async_sqlite_session_factory):
    _patch_sqlite_for_external_agent_tables()
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            ExternalAgentConnectionTokenRow.__table__,
            ExternalAgentTaskRow.__table__,
            ExternalAgentTaskEventRow.__table__,
            ExternalAgentTaskArtifactRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]
    )
    session.add_all(
        [
            Org(id="org-1", name="Org", slug="org"),
            User(id="user-1", org_id="org-1", name="User", email="user@example.com", approved=True),
            ExternalAgentConnectionRow(
                id="conn-1",
                org_id="org-1",
                owner_user_id="user-1",
                display_name="Codex",
                agent_kind="codex",
                transport="hosted_mcp",
            ),
        ]
    )
    await session.flush()
    return session


@pytest.mark.asyncio
async def test_connection_tokens_can_be_listed_and_revoked(external_agent_session):
    raw_token, token = await service.mint_connection_token(
        external_agent_session,
        connection_id="conn-1",
        org_id="org-1",
        name="Codex MCP token",
    )

    tokens = await service.list_connection_tokens(
        external_agent_session,
        connection_id="conn-1",
        org_id="org-1",
    )

    assert raw_token.startswith("illo_conn_")
    assert [row.id for row in tokens] == [token.id]

    revoked = await service.revoke_connection_token(
        external_agent_session,
        connection_id="conn-1",
        token_id=token.id,
        org_id="org-1",
    )
    active_tokens = await service.list_connection_tokens(
        external_agent_session,
        connection_id="conn-1",
        org_id="org-1",
    )

    assert revoked.revoked_at is not None
    assert active_tokens == []


@pytest.mark.asyncio
async def test_find_reusable_connection_matches_active_install_case_insensitively(external_agent_session):
    disabled = ExternalAgentConnectionRow(
        id="conn-disabled",
        org_id="org-1",
        owner_user_id="user-1",
        display_name="Codex",
        agent_kind="codex",
        transport="hosted_mcp",
        status="disabled",
        disabled_at=service.utcnow(),
    )
    external_agent_session.add(disabled)
    await external_agent_session.flush()

    found = await service.find_reusable_connection(
        external_agent_session,
        org_id="org-1",
        owner_user_id="user-1",
        display_name="codex",
        agent_kind="CODEX",
        transport="HOSTED_MCP",
    )

    assert found is not None
    assert found.id == "conn-1"


@pytest.mark.asyncio
async def test_signal_submit_backfill_only_updates_active_personal_agent_tokens(external_agent_session):
    now = service.utcnow()
    codex_token = ExternalAgentConnectionTokenRow(
        id="token-codex-active",
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_hash="hash-codex-active",
        token_prefix="illo_conn_codex",
        name="Old Codex MCP token",
        scopes=[service.SCOPE_ILLO_ASK],
    )
    revoked_codex_token = ExternalAgentConnectionTokenRow(
        id="token-codex-revoked",
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_hash="hash-codex-revoked",
        token_prefix="illo_conn_revoke",
        name="Revoked Codex MCP token",
        scopes=[service.SCOPE_ILLO_ASK],
        revoked_at=now,
    )
    webhook_connection = ExternalAgentConnectionRow(
        id="conn-webhook",
        org_id="org-1",
        owner_user_id="user-1",
        display_name="Webhook Source",
        agent_kind="webhook",
        transport="webhook",
    )
    webhook_token = ExternalAgentConnectionTokenRow(
        id="token-webhook",
        connection_id="conn-webhook",
        org_id="org-1",
        owner_user_id="user-1",
        token_hash="hash-webhook",
        token_prefix="illo_conn_webhk",
        name="Webhook source token",
        scopes=[service.SCOPE_ILLO_ASK],
    )
    external_agent_session.add_all(
        [webhook_connection, codex_token, revoked_codex_token, webhook_token]
    )
    await external_agent_session.flush()

    changed = await service.backfill_signal_submit_scope_for_personal_agent_tokens(
        external_agent_session,
        org_id="org-1",
    )

    assert changed == 1
    assert codex_token.scopes == [service.SCOPE_ILLO_ASK, service.SCOPE_SIGNAL_SUBMIT]
    assert revoked_codex_token.scopes == [service.SCOPE_ILLO_ASK]
    assert webhook_token.scopes == [service.SCOPE_ILLO_ASK]


@pytest.mark.asyncio
async def test_disable_connection_hides_connection_and_revokes_tokens(external_agent_session):
    raw_token, token = await service.mint_connection_token(
        external_agent_session,
        connection_id="conn-1",
        org_id="org-1",
        name="Codex MCP token",
    )

    connection = await service.disable_connection(
        external_agent_session,
        connection_id="conn-1",
        org_id="org-1",
    )

    assert raw_token.startswith("illo_conn_")
    assert connection.status == "disabled"
    assert connection.disabled_at is not None
    assert token.revoked_at is not None
    assert await service.list_connections(external_agent_session, org_id="org-1") == []
    assert [row.id for row in await service.list_connections(
        external_agent_session,
        org_id="org-1",
        include_disabled=True,
    )] == ["conn-1"]


@pytest.mark.asyncio
async def test_headless_ask_serializes_after_run_admission_without_lazy_loading(
    external_agent_session,
):
    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Codex",
        agent_kind="codex",
    )

    task = await service.create_headless_ask(
        external_agent_session,
        principal,
        question="What context matters?",
        context={"topic": "inbound smoke"},
        metadata={"mcp_tool": "illo_ask"},
    )
    payload = await service.serialize_task(task, include_events=True, session=external_agent_session)

    assert payload["id"] == task.id
    assert payload["status"] == "submitted"
    assert payload["illo_run_id"] is not None
    assert payload["updated_at"]
    assert [event["event_type"] for event in payload["events"]] == ["external_task.ask_illo_submitted"]


@pytest.mark.asyncio
async def test_failed_headless_ask_returns_only_the_public_failure(external_agent_session):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    raw_diagnostic = "provider traceback token=headless-secret"
    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Codex",
        agent_kind="codex",
    )
    run = AgentRunRow(
        org_id="org-1",
        user_id="user-1",
        thread_id="external-agent:conn-1:ask-failed",
        trace_id="run:failed-headless",
        profile="fast",
        recipe="fast",
        status="failed",
        input_message="Private question",
        target_ref={},
        workspace_ref={},
        model_policy={},
        context_summary=raw_diagnostic,
        metadata_={"failure": {"category": "upstream", "error": raw_diagnostic}},
        failed_at=service.utcnow(),
    )
    external_agent_session.add(run)
    await external_agent_session.flush()
    task = ExternalAgentTaskRow(
        id="ask-failed",
        org_id="org-1",
        connection_id="conn-1",
        created_by_user_id="user-1",
        source_surface="bridge_ask_illo",
        title="Private question",
        instructions="Private question",
        input_parts=[],
        status="submitted",
        idempotency_key="ask:failed",
        illo_run_id=run.id,
        error=raw_diagnostic,
        metadata_={"headless": True},
    )
    external_agent_session.add(task)
    await external_agent_session.flush()

    payload = await service.get_headless_ask(
        external_agent_session,
        principal,
        ask_id=task.id,
    )
    serialized = str(payload)

    assert raw_diagnostic not in serialized
    assert payload["answer"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert payload["failure"]["message"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert payload["ask"]["error"] == UPSTREAM_FAILED_RUN_MESSAGE


@pytest.mark.asyncio
async def test_workspace_search_returns_visible_project_context_profiles(async_sqlite_session_factory):
    _patch_sqlite_for_external_agent_tables()
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Idea.__table__,
            IdeaThread.__table__,
            ProjectProfile.__table__,
            ProjectProfileAccess.__table__,
            IdeaProjectAttachment.__table__,
        ]
    )
    session.add_all(
        [
            Org(id=ORG_ID, name="Org", slug="org"),
            User(id=OWNER_ID, org_id=ORG_ID, name="User", email="user@example.com", approved=True),
        ]
    )
    await session.flush()
    profile = ProjectProfile(
        id="cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
        org_id=ORG_ID,
        user_id=OWNER_ID,
        slug="aritzia-uwear-client-project",
        name="Aritzia / Uwear Client Project",
        description="85K asset pilot with QA retry proof and delivery package.",
        visibility="private",
        active=True,
        project_context={
            "resources": [
                {
                    "id": "delivery-package",
                    "kind": "folder",
                    "label": "Delivery package",
                    "path": "/projects/aritzia/delivery",
                }
            ]
        },
    )
    session.add(profile)
    await session.flush()

    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id=ORG_ID,
        owner_user_id=OWNER_ID,
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Codex",
        agent_kind="codex",
    )

    payload = await service.search_workspace(
        session,
        principal,
        query="Aritzia 85K asset pilot",
        limit=5,
    )

    assert payload["query"] == "Aritzia 85K asset pilot"
    assert payload["results"][0]["type"] == "project_context_profile"
    assert payload["results"][0]["slug"] == "aritzia-uwear-client-project"
    assert payload["results"][0]["resources"]["count"] == 1
    assert payload["results"][0]["resources"]["items"][0]["path"] == "/projects/aritzia/delivery"


class _ScalarResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ExternalAgentSession:
    def __init__(self):
        self.users = {
            OWNER_ID: User(id=OWNER_ID, org_id=ORG_ID, name="Reda", email="reda@test.com"),
            TEAMMATE_ID: User(id=TEAMMATE_ID, org_id=ORG_ID, name="JB", email="jb@test.com"),
        }
        self.added: list[object] = []
        self._next_thread_id = 1
        self._next_mention_id = 1
        self._next_notification_id = 1

    def add(self, row):
        self.added.append(row)

    async def get(self, model, item_id):
        if model is User:
            return self.users.get(str(item_id))
        return None

    async def scalars(self, _stmt):
        return _ScalarResult([])

    async def flush(self):
        for row in self.added:
            if isinstance(row, Idea) and not row.id:
                row.id = str(uuid.uuid4())
            elif isinstance(row, IdeaThread) and row.id is None:
                row.id = self._next_thread_id
                self._next_thread_id += 1
            elif isinstance(row, UserMention) and row.id is None:
                row.id = self._next_mention_id
                self._next_mention_id += 1
            elif isinstance(row, NotificationEvent) and row.id is None:
                row.id = self._next_notification_id
                self._next_notification_id += 1


@pytest.mark.asyncio
async def test_create_thread_from_agent_notifies_teammate_with_unified_notification():
    db = _ExternalAgentSession()
    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id=ORG_ID,
        owner_user_id=OWNER_ID,
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Codex Desktop",
        agent_kind="codex",
    )

    idea, thread, notified = await service.create_thread_from_agent(
        db,
        principal,
        title="Install Illo MCP",
        body="Please install the new MCP bridge.",
        teammate_user_ids=[TEAMMATE_ID],
    )

    assert str(idea.id)
    assert thread.message_type == "agent_share"
    assert notified == [TEAMMATE_ID]

    mention = next(row for row in db.added if isinstance(row, UserMention))
    assert mention.user_id == TEAMMATE_ID
    assert mention.thread_message_id == thread.id

    notification = next(row for row in db.added if isinstance(row, NotificationEvent))
    assert notification.user_id == TEAMMATE_ID
    assert notification.kind == NOTIFICATION_KIND_WORKSPACE_MENTION
    assert notification.idea_id == str(idea.id)
    assert notification.payload["thread_message_id"] == thread.id


def test_fake_bridge_adapter_echoes_task_without_network():
    module = _load_bridge_module()

    result = module.FakeAdapter().run_task(
        {"id": "task-1", "title": "Share work", "instructions": "Summarize the draft", "input_parts": []}
    )

    assert "Share work" in result["result_summary"]
    assert result["artifacts"][0]["content_json"]["task_id"] == "task-1"


def test_bridge_run_once_claims_runs_and_completes_task_without_network():
    module = _load_bridge_module()

    class StubIlloClient:
        def __init__(self):
            self.calls: list[tuple[str, tuple, dict]] = []

        def heartbeat(self, *args, **kwargs):
            self.calls.append(("heartbeat", args, kwargs))

        def claim(self, *args, **kwargs):
            self.calls.append(("claim", args, kwargs))
            return [
                {
                    "id": "task-1",
                    "title": "Share work",
                    "instructions": "Summarize the draft",
                    "source_surface": "cortex",
                    "input_parts": [],
                }
            ]

        def event(self, *args, **kwargs):
            self.calls.append(("event", args, kwargs))

        def complete(self, *args, **kwargs):
            self.calls.append(("complete", args, kwargs))

        def fail(self, *args, **kwargs):
            self.calls.append(("fail", args, kwargs))

    client = StubIlloClient()

    assert module.run_once(client, module.FakeAdapter(), max_tasks=1) == 1

    call_names = [name for name, _args, _kwargs in client.calls]
    assert call_names == ["heartbeat", "claim", "event", "complete"]
    assert client.calls[-1][1][0] == "task-1"
    assert "Fake personal agent completed" in client.calls[-1][1][1]


def test_hermes_adapter_uses_openai_chat_completions_contract(monkeypatch):
    module = _load_bridge_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return {
            "id": "chatcmpl-1",
            "model": "hermes-agent",
            "choices": [{"message": {"content": "Hermes completed it."}}],
            "usage": {"total_tokens": 12},
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setenv("HERMES_BASE_URL", "http://hermes.local")
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.delenv("HERMES_API_MODE", raising=False)
    monkeypatch.delenv("HERMES_RUN_ENDPOINT", raising=False)

    result = module.HermesAdapter().run_task(
        {
            "id": "task-1",
            "connection_id": "conn-1",
            "title": "Share work",
            "instructions": "Summarize the draft",
            "source_surface": "cortex",
            "input_parts": [{"type": "note", "text": "draft"}],
        }
    )

    assert result["result_summary"] == "Hermes completed it."
    assert result["artifacts"][0]["content_json"]["id"] == "chatcmpl-1"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://hermes.local/v1/chat/completions"
    assert calls[0]["token"] == "secret"
    assert calls[0]["headers"] == {
        "X-Hermes-Session-Id": "illo-task-task-1",
        "X-Hermes-Session-Key": "illo-connection-conn-1",
    }
    payload = calls[0]["payload"]
    assert payload["model"] == "hermes-agent"
    assert payload["stream"] is False
    assert "Summarize the draft" in payload["messages"][1]["content"]


def test_hermes_adapter_can_poll_runs_api(monkeypatch):
    module = _load_bridge_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if method == "POST":
            return {"run_id": "run-1", "status": "started"}
        return {
            "run_id": "run-1",
            "status": "completed",
            "session_id": "illo-task-task-1",
            "output": "Run output",
            "usage": {"total_tokens": 8},
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setenv("HERMES_BASE_URL", "http://hermes.local")
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.setenv("HERMES_API_MODE", "runs")
    monkeypatch.delenv("HERMES_RUN_ENDPOINT", raising=False)

    result = module.HermesAdapter().run_task({"id": "task-1", "title": "Share work", "instructions": "Do it"})

    assert result["result_summary"] == "Run output"
    assert result["artifacts"][0]["content_json"]["run_id"] == "run-1"
    assert calls[0]["url"] == "http://hermes.local/v1/runs"
    assert calls[1]["url"] == "http://hermes.local/v1/runs/run-1"


@pytest.mark.live_provider
def test_live_hermes_runs_adapter_smoke(monkeypatch):
    if os.environ.get("ILLO_LIVE_HERMES_SMOKE") != "1":
        pytest.skip("Set ILLO_LIVE_HERMES_SMOKE=1 with HERMES_BASE_URL and HERMES_API_KEY to run live Hermes smoke.")
    if not os.environ.get("HERMES_BASE_URL"):
        pytest.skip("HERMES_BASE_URL is required for live Hermes smoke.")

    module = _load_bridge_module()
    monkeypatch.setenv("HERMES_API_MODE", "runs")
    monkeypatch.setenv("HERMES_RUN_POLL_INTERVAL", os.environ.get("HERMES_RUN_POLL_INTERVAL", "1"))
    monkeypatch.setenv("PERSONAL_AGENT_TIMEOUT", os.environ.get("PERSONAL_AGENT_TIMEOUT", "180"))

    result = module.HermesAdapter().run_task(
        {
            "id": "live-hermes-smoke",
            "connection_id": "live-connection",
            "title": "Hermes live bridge smoke",
            "instructions": "Reply exactly with ILLO_HERMES_LIVE_OK and one short sentence.",
            "source_surface": "ci_live_smoke",
            "input_parts": [{"type": "smoke", "source": "pytest"}],
        }
    )

    assert "ILLO_HERMES_LIVE_OK" in result["result_summary"]
