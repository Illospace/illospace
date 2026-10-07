"""Credential expiry through the real vault, preflight, and run boundaries."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.org import Org, OrgApiKey, User, UserCodexConnection
from brain.platform.db.models.cycle import Cycle, CycleRun
from tests.test_cycle_failure_guard import FAILURE_GUARD_TABLES
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.integrations import llm, openai_codex_auth
from brain.platform.integrations.openai_codex_auth import (
    CodexCredentialExpiredError, OpenAICodexCredential, encode_codex_auth_payload,
)
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult, ProviderAuthPassedPreflightResult, async_probe_provider_auth,
)
from brain.systems import vault
from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.status import RunStatus
from brain.systems.runs.failures import RunFailureCategory, failure_category_for_error, public_run_failure
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.vault import codex_health
from tests.inbound_preservation_support import _patch_sqlite_for_pg_types

ORG = "11111111-1111-4111-8111-111111111111"
USER_A = "22222222-2222-4222-8222-222222222222"
USER_B = "33333333-3333-4333-8333-333333333333"
MODEL = "openai/gpt-6.1-sol"


def _credential(*, expired=True, token="fixture-access"):
    return json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token=token, refresh_token="fixture-refresh", account_id="fixture-account",
        expires_at=time.time() - 100 if expired else time.time() + 3600,
        auth_mode="chatgpt",
    )))


@pytest.fixture
async def credential_store(tmp_path, monkeypatch):
    _patch_sqlite_for_pg_types()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}")
    async with engine.begin() as connection:
        for table in (
            Org.__table__, User.__table__, OrgApiKey.__table__, UserCodexConnection.__table__,
            AgentRunRow.__table__, AgentRunEventRow.__table__, AgentRunArtifactRow.__table__,
            *FAILURE_GUARD_TABLES,
        ):
            await connection.execute(CreateTable(table))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.SessionFactory", factory)
    monkeypatch.setattr(vault, "_encrypt", lambda value: value.encode())
    monkeypatch.setattr(vault, "_decrypt", lambda value: value.decode())
    monkeypatch.setattr(llm, "_allow_local_codex_auth_fallback", lambda: False)
    monkeypatch.setattr(llm, "OpenAICodexClient", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(llm, "_build_anthropic_client", lambda _key: llm.LLMClient(
        client=object(), provider="anthropic", source="org_main", auth_mode="api_key",
        is_oauth=False, extra_headers={}, token_prefix="",
    ))
    alert = AsyncMock()
    monkeypatch.setattr(codex_health, "async_deliver_failure_alert", alert)
    refresh_request = Mock(return_value=SimpleNamespace(
        status_code=401, json=lambda: {"error": "invalid_grant"},
    ))
    monkeypatch.setattr(openai_codex_auth, "http_post", refresh_request)
    async with factory() as session:
        session.add(Org(id=ORG, name="Fixture", slug="credential-fixture"))
        for user_id, name in ((USER_A, "A"), (USER_B, "B")):
            session.add(User(id=user_id, org_id=ORG, name=name, email=f"{name}@example.test"))
            await vault.async_set_user_codex_connection(
                user_id, _credential(expired=user_id == USER_A), session=session,
            )
        session.add(OrgApiKey(org_id=ORG, provider="anthropic", encrypted_key=b"fixture-org-key"))
        await session.commit()
    yield SimpleNamespace(factory=factory, alert=alert, refresh_request=refresh_request)
    await engine.dispose()


async def _probe(store, user_id=USER_A):
    async with store.factory() as session:
        return await async_probe_provider_auth(
            session, user_id=user_id, org_id=ORG, provider="openai", model=MODEL,
        )


async def _connection(store, user_id=USER_A):
    async with store.factory() as session:
        return (await session.scalars(select(UserCodexConnection).where(
            UserCodexConnection.user_id == user_id,
        ))).one()


async def test_revoked_refresh_is_durable_scoped_and_alerted_once(credential_store):
    store = credential_store
    first = await _probe(store)
    second = await _probe(store)
    assert isinstance(first, ProviderAuthBlockedPreflightResult)
    assert first.error_code == "credential_expired"
    assert second.to_dict() == first.to_dict()
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()
    failed = await _connection(store)
    assert failed.is_active is True  # Keep the identity; never fall back to another key.
    assert failed.credential_error_code == "credential_expired"
    assert failed.credential_error_at is not None
    assert failed.credential_alerted_at is not None
    assert isinstance(await _probe(store, USER_B), ProviderAuthPassedPreflightResult)
    async with store.factory() as session:
        unrelated = await async_probe_provider_auth(
            session, user_id=USER_A, org_id=ORG, provider="anthropic", model="anthropic/claude-sonnet-4-6",
        )
    assert isinstance(unrelated, ProviderAuthPassedPreflightResult)
    assert (await _connection(store, USER_B)).credential_error_code is None


async def test_actual_credential_replacement_restores_queued_work(credential_store):
    store = credential_store
    assert isinstance(await _probe(store), ProviderAuthBlockedPreflightResult)
    async with store.factory() as session:
        queued = await AsyncAgentRunStore(session).create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="fixture-queued", message="Read this",
            model_policy={"model": MODEL},
        ))
        queued_id = queued.id
        await session.commit()
    async with store.factory() as session:
        await vault.async_set_user_codex_connection(
            USER_A, _credential(expired=False, token="replacement-access"), session=session,
        )
        await session.commit()
    restored = await _connection(store)
    assert restored.credential_error_code is None
    assert restored.credential_error_at is None
    assert restored.credential_alerted_at is None
    async with store.factory() as session:
        queued = await session.get(AgentRunRow, queued_id)
        assert queued.status == "queued"
        assert (await AsyncAgentRunStore(session).claim_run(queued_id)).status.value == "starting"
        client = await llm.async_resolve_llm_client(
            user_id=queued.user_id, org_id=queued.org_id, provider="openai", auth_mode="chatgpt", session=session,
        )
    assert client.token_prefix == "replacement-access"[:18]
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()


async def test_expiry_alert_resets_for_a_new_credential(credential_store):
    store = credential_store
    await _probe(store)
    async with store.factory() as session:
        await vault.async_set_user_codex_connection(
            USER_A, _credential(token="second-credential"), session=session,
        )
        await session.commit()
    await _probe(store)
    await _probe(store)
    assert store.refresh_request.call_count == 2
    assert store.alert.await_count == 2


async def test_stale_failure_does_not_disable_replacement(credential_store):
    store = credential_store
    stale = _credential()
    async with store.factory() as session:
        await vault.async_set_user_codex_connection(USER_A, _credential(expired=False, token="stale-failure-replacement"), session=session)
        await session.commit()
    await codex_health.mark_codex_credential_expired(user_id=USER_A, credential_payload=stale)
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)
    assert (await _connection(store)).credential_error_code is None
    store.alert.assert_not_awaited()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_refresh_does_not_trip_circuit(credential_store, status):
    store = credential_store
    store.refresh_request.return_value = SimpleNamespace(status_code=status, json=lambda: {})
    for _ in range(2):
        result = await _probe(store)
        assert result.error_code == "provider_credential_unavailable"
    assert store.refresh_request.call_count == 2
    assert (await _connection(store)).credential_error_code is None
    store.alert.assert_not_awaited()


async def test_timeout_does_not_trip_circuit(credential_store):
    store = credential_store
    store.refresh_request.side_effect = TimeoutError("fixture timeout")
    await _probe(store)
    assert (await _connection(store)).credential_error_code is None
    store.alert.assert_not_awaited()


async def test_failed_alert_retries_without_refreshing_credential(credential_store):
    store = credential_store
    store.alert.side_effect = [RuntimeError("fixture delivery failure"), None]
    await _probe(store)
    assert (await _connection(store)).credential_alerted_at is None
    await _probe(store)
    await _probe(store)
    assert store.alert.await_count == 2
    first_id, second_id = [call.kwargs["policy"].client_msg_id for call in store.alert.await_args_list]
    assert first_id == second_id
    store.refresh_request.assert_called_once()
    assert (await _connection(store)).credential_alerted_at is not None


@pytest.mark.parametrize("blocked_continuation", [False, True])
async def test_watchdog_retries_pending_connection_alert_without_oauth(
    credential_store, monkeypatch, blocked_continuation,
):
    from brain.systems.runs.chantier_continuation import CONTINUATION_AUTH_BLOCKED_EVENT
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.events import run_event

    store = credential_store
    store.alert.side_effect = RuntimeError("fixture delivery unavailable")
    await _probe(store)
    assert (await _connection(store)).credential_alerted_at is None
    async with store.factory() as session:
        runs = AsyncAgentRunStore(session)
        run = await runs.create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="pending-alert", message="Read",
            model_policy={"model": MODEL}, target_ref={"kind": "custom_parent_surface"},
        ))
        for status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.FAILED):
            await runs.set_status(run.id, status)
        if blocked_continuation:
            await runs.append_event(run_event(run.id, CONTINUATION_AUTH_BLOCKED_EVENT, {
                "owner_user_id": USER_A,
                "reason": "credential_expired: provider=openai credential=OpenAI Codex / ChatGPT",
            }))
        run_id = run.id
        await session.commit()

    monkeypatch.setattr(runner, "_queued_backlog_snapshot_async", AsyncMock(return_value=SimpleNamespace(
        queued=0, oldest_queued_at=None,
    )))
    store.alert.side_effect = None
    await runner._nudge_stale_queued_runs_if_due_async(force=True)
    await runner._nudge_stale_queued_runs_if_due_async(force=True)
    store.refresh_request.assert_called_once()
    assert store.alert.await_count == 2
    ids = [call.kwargs["policy"].client_msg_id for call in store.alert.await_args_list]
    assert len(set(ids)) == 1
    assert (await _connection(store)).credential_alerted_at is not None
    assert (await _connection(store, USER_B)).credential_error_code is None
    async with store.factory() as session:
        assert (await session.scalars(select(AgentRunRow))).one().id == run_id
        assert (await session.get(AgentRunRow, run_id)).status == "failed"


async def test_expired_admission_creates_no_run_or_triage_attempt(credential_store, monkeypatch):
    from brain.systems.runs import work_intake

    store = credential_store
    request = AgentRunRequest(
        org_id=ORG, user_id=USER_A, thread_id="fixture-admission", message="Preserve this",
        model_policy={"model": MODEL},
    )
    monkeypatch.setattr(work_intake, "build_agent_run_request", AsyncMock(return_value=request))
    creation = Mock(side_effect=AssertionError("expired auth must stop before a triage run"))
    monkeypatch.setattr(work_intake, "AsyncAgentRunStore", creation)
    event = work_intake.WorkIntakeEvent(
        source="inbound", event_type="inbound.submission_received", org_id=ORG,
        actor={"id": USER_A}, target={"kind": "inbound_submission"},
    )
    for _ in range(2):
        async with store.factory() as session:
            result = await work_intake.admit_work(session, event)
        assert not result.ok
        assert result.run_id is None
        assert result.skipped_reason.startswith("credential_expired:")
    creation.assert_not_called()
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()


def test_expiry_remains_explicit_in_terminal_public_failure():
    error = CodexCredentialExpiredError()
    category = failure_category_for_error(error)
    assert category is RunFailureCategory.CREDENTIAL_EXPIRED
    assert failure_category_for_error(
        "run_execution_failed: CodexCredentialExpiredError", exception_type=type(error),
    ) is category
    failure = public_run_failure("failed", category)
    assert failure["category"] == "credential_expired"
    assert "Sign in again" in failure["message"]


@pytest.mark.parametrize("status,code,expired", [
    (400, "invalid_grant", True), (400, "refresh_token_reused", True),
    (400, "invalid_request", False), (401, None, True), (403, None, True),
    (429, None, False), (500, None, False),
])
def test_refresh_classifies_permanent_rejection_without_echoing_body(monkeypatch, status, code, expired):
    monkeypatch.setattr(openai_codex_auth, "http_post", lambda *_args, **_kwargs: SimpleNamespace(
        status_code=status,
        json=lambda: {"error": {"code": code, "message": "fixture-secret"}},
        text="fixture-secret",
    ))
    with pytest.raises(RuntimeError) as raised:
        openai_codex_auth.refresh_codex_access_token("fixture-refresh")
    assert isinstance(raised.value, CodexCredentialExpiredError) is expired
    assert "fixture-secret" not in str(raised.value)
    assert "fixture-refresh" not in str(raised.value)


async def test_reauth_refreshes_a_preloaded_healthy_connection(credential_store):
    store = credential_store
    async with store.factory() as session:
        preloaded = (await session.scalars(select(UserCodexConnection).where(
            UserCodexConnection.user_id == USER_A,
        ))).one()
        assert preloaded.credential_error_code is None
        await _probe(store)
        await vault.async_set_user_codex_connection(
            USER_A, _credential(expired=False, token="stale-session-replacement"), session=session,
        )
        await session.commit()
    assert (await _connection(store)).credential_error_code is None
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)


async def test_settings_expired_upload_finishes_without_nested_lock(credential_store, monkeypatch):
    from brain.systems.runtime_settings import auth

    store = credential_store
    monkeypatch.setattr(auth, "verify_provider_api_key", lambda *_args: None)
    async with store.factory() as session:
        user = await session.get(User, USER_A)
        status = await asyncio.wait_for(auth.async_store_openai_connection(
            session, user, _credential(token="expired-settings-upload"),
        ), timeout=1)
        assert status.setup_required
        await session.commit()
    assert (await _connection(store)).credential_error_code == "credential_expired"
    store.alert.assert_not_awaited()  # Pending until the credential transaction commits.
    result = await _probe(store)
    assert result.error_code == "credential_expired"
    assert result.credential_alert_owned
    store.alert.assert_awaited_once()
    store.refresh_request.assert_called_once()


async def test_identical_bad_upload_does_not_reopen_circuit(credential_store):
    store = credential_store
    await _probe(store)
    async with store.factory() as session:
        current = await session.scalar(select(UserCodexConnection).where(
            UserCodexConnection.user_id == USER_A,
        ))
        await vault.async_set_user_codex_connection(
            USER_A, vault._decrypt(bytes(current.encrypted_credential)), session=session,
        )
        await session.commit()
    await _probe(store)
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()


@pytest.mark.parametrize("recovery", ["signin", "watchdog"])
async def test_terminal_worker_survives_blocked_continuation_and_recovers_automatically(
    credential_store, monkeypatch, recovery,
):
    from brain.systems.runs import work_intake
    from brain.systems.runtime_settings import auth
    from brain.systems.runs.chantier_continuation import (
        CONTINUATION_AUTH_BLOCKED_EVENT, recover_auth_blocked_continuations,
    )

    store = credential_store
    if recovery == "watchdog":
        store.refresh_request.return_value = SimpleNamespace(status_code=503, json=lambda: {})
    await _probe(store)
    terminal_error = str(CodexCredentialExpiredError()) if recovery == "signin" else "upstream_provider_error: overloaded_error"
    terminal_category = "credential_expired" if recovery == "signin" else "upstream"
    async with store.factory() as session:
        run_store = AsyncAgentRunStore(session)
        anchor = await run_store.create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="parent-thread", message="coordinate",
            model_policy={"model": MODEL},
            target_ref={"kind": "custom_parent_surface", "thread_id": "parent-thread"},
        ))
        for status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.COMPLETED):
            await run_store.set_status(anchor.id, status)
        child = await run_store.create_child_run(
            anchor, recipe=RunRecipe.WORKER, message="child", step_key="spawn_worker:a",
            metadata={"origin": "spawn_worker", "spawned_by_tool": True, "join_parent": True},
            initial_status=RunStatus.STARTING,
        )
        await run_store.set_status(child.id, RunStatus.RUNNING)
        anchor_id, child_id = anchor.id, child.id
        await session.commit()
        monkeypatch.setattr(work_intake, "build_agent_run_request", AsyncMock(return_value=AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="parent-thread", message="join",
            model_policy={"model": MODEL},
        )))
        await AsyncAgentRunEngine(session, recipes={}).fail(child_id, terminal_error)
        await session.commit()
    async with store.factory() as session:
        row = await session.get(AgentRunRow, child_id)
        assert row.status == "failed"
        assert row.metadata_["failure"]["category"] == terminal_category
        assert await AsyncAgentRunStore(session).has_event_type(anchor_id, CONTINUATION_AUTH_BLOCKED_EVENT)
        refresh_calls = store.refresh_request.call_count
        assert await recover_auth_blocked_continuations(session, user_id=USER_B) == 0
        assert store.refresh_request.call_count == refresh_calls
        from brain.systems.runs.cortex import runner
        monkeypatch.setattr(runner, "_queued_backlog_snapshot_async", AsyncMock(return_value=SimpleNamespace(
            queued=0, oldest_queued_at=None,
        )))
        if recovery == "signin":
            assert await recover_auth_blocked_continuations(session, user_id=USER_A) == 0
            assert store.refresh_request.call_count == refresh_calls
            monkeypatch.setattr(auth, "verify_provider_api_key", lambda *_args: None)
            user = await session.get(User, USER_A)
            status = await auth.async_store_openai_connection(
                session, user, _credential(expired=False, token="restored-fanout-credential"),
            )
            assert status.status == "connected"
            rows_before_commit = (await session.scalars(select(AgentRunRow))).all()
            assert len(rows_before_commit) == 2
            await session.commit()
        else:
            store.refresh_request.return_value = SimpleNamespace(
                status_code=200, json=lambda: json.loads(_credential(expired=False, token="provider-restored-access")),
            )
        await runner._nudge_stale_queued_runs_if_due_async(force=True)
        assert await recover_auth_blocked_continuations(session, user_id=USER_A) == 0
        assert await recover_auth_blocked_continuations(session, user_id=USER_A) == 0
        rows = (await session.scalars(select(AgentRunRow))).all()
        assert len(rows) == 3
        await session.commit()
    assert store.alert.await_count == (1 if recovery == "signin" else 0)


async def test_org_expiry_does_not_claim_personal_alert_ownership(credential_store):
    store = credential_store
    async with store.factory() as session:
        personal = await session.scalar(select(UserCodexConnection).where(UserCodexConnection.user_id == USER_A))
        personal.is_active = False
        session.add(OrgApiKey(org_id=ORG, provider="openai", encrypted_key=_credential().encode()))
        await session.commit()
    result = await _probe(store)
    assert result.error_code == "credential_expired"
    assert not result.credential_alert_owned
    store.alert.assert_not_awaited()


async def test_changed_profile_metadata_keeps_same_revoked_identity(credential_store, monkeypatch):
    from brain.systems.runtime_settings import auth

    store = credential_store
    await _probe(store)
    current = await _connection(store)
    payload = json.loads(vault._decrypt(bytes(current.encrypted_credential)))
    payload["last_refresh"] = "2026-10-07T12:00:00Z"
    payload["profile_name"] = "Updated profile"
    monkeypatch.setattr(auth, "verify_provider_api_key", lambda *_args: None)
    async with store.factory() as session:
        user = await session.get(User, USER_A)
        status = await auth.async_store_openai_connection(session, user, json.dumps(payload))
        assert status.setup_required
        await session.commit()
    await _probe(store)
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()


@pytest.mark.parametrize("finalizer", ["completion_hook", "watchdog"])
async def test_queued_cycle_expiry_keeps_connection_alert_owner_after_reauth(credential_store, monkeypatch, finalizer):
    from datetime import datetime, timezone
    from brain.platform.integrations.openai_codex_auth import CodexConnectionExpiredError
    from brain.systems.runs.failure_diagnostic import RunFailureStage
    from brain.systems.cycles import service, cycle_failure_guard
    from brain.platform.db.models.cycle import CycleFailureGuardLatch, CycleFailureGuardTriggerState

    store = credential_store
    await _probe(store)
    cycle_alert = AsyncMock()
    monkeypatch.setattr(cycle_failure_guard, "async_deliver_failure_alert", cycle_alert)
    monkeypatch.setenv("CYCLE_FAILURE_ALERT_THRESHOLD", "1")
    monkeypatch.setattr(service, "async_prepare_cycle_run_visible_finalization", AsyncMock(return_value=None))
    async with store.factory() as session:
        cycle = Cycle(id=7, user_id=USER_A, org_id=ORG, name="Queued expiry", prompt="Read",
                      schedule_expr="*/15 * * * *", timezone="UTC", enabled=True)
        cycle_run = CycleRun(cycle_id=7, scheduled_for=datetime.now(timezone.utc), status="running",
                             prompt_snapshot="Read", context_snapshot={"auth_preflight": {"status": "passed"}})
        session.add_all([cycle, cycle_run])
        await session.flush()
        agent_store = AsyncAgentRunStore(session)
        agent_run = await agent_store.create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="queued-cycle", message="Read",
            model_policy={"model": MODEL}, metadata={"source": "cycle", "cycle_run_id": cycle_run.id},
        ))
        cycle_run.run_id = agent_run.id
        for status in (RunStatus.STARTING, RunStatus.RUNNING):
            await agent_store.set_status(agent_run.id, status)
        await agent_store.fail_run(
            agent_run.id, category=RunFailureCategory.CREDENTIAL_EXPIRED,
            stage=RunFailureStage.RUNNER_SETTLEMENT, reason=str(CodexConnectionExpiredError()),
            exception_type=CodexConnectionExpiredError,
        )
        agent_id, cycle_run_id = agent_run.id, cycle_run.id
        await vault.async_set_user_codex_connection(
            USER_A, _credential(expired=False, token="cycle-restored-access"), session=session,
        )
        await session.commit()
    if finalizer == "completion_hook":
        await service.async_finalize_cycle_run_from_run(agent_id, status="failed", error=str(CodexConnectionExpiredError()))
    else:
        await service.async_recover_stale_cycle_runs_once(stale_after_seconds=0)
    async with store.factory() as session:
        settled = await session.get(CycleRun, cycle_run_id)
        assert settled.status == "failed"
        assert settled.context_snapshot["credential_alert_owned"] is True
        assert (await session.scalars(select(CycleFailureGuardLatch))).all() == []
        assert (await session.scalars(select(CycleFailureGuardTriggerState))).all() == []
    store.alert.assert_awaited_once()
    cycle_alert.assert_not_awaited()


async def test_chantier_recovery_uses_original_scope_owner(credential_store, monkeypatch):
    from brain.systems.runs import work_intake
    from brain.systems.runtime_settings import auth
    from brain.systems.runs.chantier_continuation import (
        CONTINUATION_AUTH_BLOCKED_EVENT, queue_worker_continuation_for_terminal_run,
        recover_auth_blocked_continuations,
    )
    from brain.systems.runs.cortex import runner

    store = credential_store
    await _probe(store, USER_A)
    async def build_request(_session, event):
        return AgentRunRequest(org_id=ORG, user_id=event.actor["id"], thread_id="shared-thread",
                               message="join", model_policy={"model": MODEL})
    monkeypatch.setattr(work_intake, "build_agent_run_request", build_request)
    async with store.factory() as session:
        runs = AsyncAgentRunStore(session)
        await runs.create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_A, thread_id="shared-thread", message="Original scope",
            model_policy={"model": MODEL}, metadata={"chantier_declare": {"record_id": 123}},
        ))
        anchor = await runs.create_run(AgentRunRequest(
            org_id=ORG, user_id=USER_B, thread_id="shared-thread", message="Later fan-out",
            model_policy={"model": MODEL},
        ))
        for status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.COMPLETED):
            await runs.set_status(anchor.id, status)
        worker = await runs.create_child_run(
            anchor, recipe=RunRecipe.WORKER, message="Verify", step_key="spawn_worker:a",
            metadata={"origin": "spawn_worker", "spawned_by_tool": True}, initial_status=RunStatus.STARTING,
        )
        for status in (RunStatus.RUNNING, RunStatus.COMPLETED):
            await runs.set_status(worker.id, status)
        anchor_id, worker_id = anchor.id, worker.id
        await session.commit()
        assert await queue_worker_continuation_for_terminal_run(session, terminal_run_id=worker_id) is None
        await session.commit()
    async with store.factory() as session:
        blocked = await session.scalar(select(AgentRunEventRow).where(
            AgentRunEventRow.run_id == anchor_id, AgentRunEventRow.event_type == CONTINUATION_AUTH_BLOCKED_EVENT,
        ))
        assert blocked.payload["owner_user_id"] == USER_A
        await vault.async_set_user_codex_connection(USER_B, _credential(token="expired-B-token"), session=session)
        await session.commit()
    await _probe(store, USER_B)
    monkeypatch.setattr(auth, "verify_provider_api_key", lambda *_args: None)
    async with store.factory() as session:
        assert await recover_auth_blocked_continuations(session, user_id=USER_B) == 0
        user_a = await session.get(User, USER_A)
        connected = await auth.async_store_openai_connection(session, user_a,
            _credential(expired=False, token="scope-A-restored"))
        assert connected.status == "connected"
        assert len((await session.scalars(select(AgentRunRow))).all()) == 3
        await session.commit()
    monkeypatch.setattr(runner, "_queued_backlog_snapshot_async", AsyncMock(return_value=SimpleNamespace(
        queued=0, oldest_queued_at=None,
    )))
    await runner._nudge_stale_queued_runs_if_due_async(force=True)
    async with store.factory() as session:
        rows = (await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id))).all()
        assert len(rows) == 4
        assert rows[-1].user_id == USER_A
        assert await recover_auth_blocked_continuations(session, user_id=USER_A) == 0
        assert await recover_auth_blocked_continuations(session, user_id=USER_B) == 0
    assert (await _connection(store, USER_B)).credential_error_code == "credential_expired"
    assert store.alert.await_count == 2


async def test_admission_refresh_keeps_event_loop_responsive(credential_store, monkeypatch):
    from brain.systems.runs import work_intake

    started, release = threading.Event(), threading.Event()
    responsive = []

    def refresh(*_args, **_kwargs):
        started.set()
        responsive.append(release.wait(timeout=1.0))
        return SimpleNamespace(status_code=401, json=lambda: {"error": "invalid_grant"})

    monkeypatch.setattr(openai_codex_auth, "http_post", refresh)
    monkeypatch.setattr(work_intake, "build_agent_run_request", AsyncMock(return_value=AgentRunRequest(
        org_id=ORG, user_id=USER_A, thread_id="refresh-thread", message="Read",
        model_policy={"model": MODEL},
    )))
    async with credential_store.factory() as session:
        admission = asyncio.create_task(work_intake.admit_work(session, work_intake.WorkIntakeEvent(
            source="inbound", event_type="inbound.submission_received", org_id=ORG,
            actor={"id": USER_A}, target={"kind": "inbound_submission"},
        )))
        assert await asyncio.to_thread(started.wait, 1.0)
        release.set()
        result = await admission
    assert responsive == [True]
    assert result.skipped_reason.startswith("credential_expired:")


async def test_signin_replaces_unreadable_old_ciphertext(credential_store, monkeypatch):
    from cryptography.fernet import InvalidToken
    from brain.systems.runtime_settings import auth

    store = credential_store
    await _probe(store)
    previous = bytes((await _connection(store)).encrypted_credential)

    def decrypt(encrypted):
        if encrypted == previous:
            raise InvalidToken()
        return encrypted.decode()

    monkeypatch.setattr(vault, "_decrypt", decrypt)
    monkeypatch.setattr(auth, "verify_provider_api_key", lambda *_args: None)
    replacement = _credential(expired=False, token="readable-replacement")
    async with store.factory() as session:
        user = await session.get(User, USER_A)
        result = await auth.async_store_openai_connection(session, user, replacement)
        assert result.status == "connected"
        await session.commit()
    connection = await _connection(store)
    assert connection.encrypted_credential == replacement.encode()
    assert connection.credential_error_code is None
    assert connection.credential_alerted_at is None
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)


@pytest.mark.parametrize("source", ["codex_subscription", "org_main"])
async def test_cancelled_rotation_commits_before_cancellation(credential_store, monkeypatch, source):
    store = credential_store
    if source == "org_main":
        async with store.factory() as session:
            personal = await session.scalar(select(UserCodexConnection).where(UserCodexConnection.user_id == USER_A))
            personal.is_active = False
            session.add(OrgApiKey(org_id=ORG, provider="openai", encrypted_key=_credential().encode()))
            await session.commit()
    started, release = threading.Event(), threading.Event()
    rotated = json.loads(_credential(expired=False, token="rotated-access"))
    rotated["tokens"]["refresh_token"] = "rotated-refresh"

    def refresh(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return SimpleNamespace(status_code=200, json=lambda: rotated)

    monkeypatch.setattr(openai_codex_auth, "http_post", refresh)
    async with store.factory() as session:
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.rollback()
    if source == "codex_subscription":
        payload = vault._decrypt(bytes((await _connection(store)).encrypted_credential))
    else:
        async with store.factory() as session:
            key = await session.scalar(select(OrgApiKey).where(OrgApiKey.provider == "openai"))
            payload = vault._decrypt(bytes(key.encrypted_key))
    assert json.loads(payload)["tokens"]["refresh_token"] == "rotated-refresh"
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)
    store.alert.assert_not_awaited()


@pytest.mark.parametrize("tentative_upload", [False, True])
async def test_cancelled_permanent_refresh_rejection_closes_connection_circuit(
    credential_store, monkeypatch, tentative_upload,
):
    store = credential_store
    started, release = threading.Event(), threading.Event()

    def refresh(*_args, **_kwargs):
        store.refresh_request()
        started.set()
        assert release.wait(timeout=2.0)
        return SimpleNamespace(status_code=401, json=lambda: {"error": "invalid_grant"})

    monkeypatch.setattr(openai_codex_auth, "http_post", refresh)
    async with store.factory() as session:
        if tentative_upload:
            await vault.async_set_user_codex_connection(USER_A, _credential(), session=session)
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        await session.rollback()

    connection = await _connection(store)
    assert connection.credential_error_code == "credential_expired"
    assert connection.credential_alerted_at is not None
    assert (await _probe(store)).error_code == "credential_expired"
    store.refresh_request.assert_called_once()
    store.alert.assert_awaited_once()
    assert isinstance(await _probe(store, USER_B), ProviderAuthPassedPreflightResult)
    async with store.factory() as session:
        unrelated = await async_probe_provider_auth(
            session, user_id=USER_A, org_id=ORG, provider="anthropic", model="anthropic/claude-sonnet-4-6",
        )
    assert isinstance(unrelated, ProviderAuthPassedPreflightResult)


async def test_successful_rotation_survives_caller_rollback(credential_store):
    store = credential_store
    store.refresh_request.return_value = SimpleNamespace(
        status_code=200, json=lambda: json.loads(_credential(expired=False, token="rollback-safe-access")),
    )
    async with store.factory() as session:
        client = await llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        )
        assert client.token_prefix == "rollback-safe-access"[:18]
        await session.rollback()
    assert "rollback-safe-access" in vault._decrypt(bytes((await _connection(store)).encrypted_credential))
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)
    store.refresh_request.assert_called_once()


async def test_stale_successful_rotation_does_not_overwrite_signin(credential_store, monkeypatch):
    store = credential_store
    started, release = threading.Event(), threading.Event()

    def refresh(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return SimpleNamespace(status_code=200, json=lambda: json.loads(
            _credential(expired=False, token="stale-rotation-access"),
        ))

    monkeypatch.setattr(openai_codex_auth, "http_post", refresh)
    async with store.factory() as session:
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        assert await asyncio.to_thread(started.wait, 1.0)
        async with store.factory() as signin:
            await vault.async_set_user_codex_connection(
                USER_A, _credential(expired=False, token="new-signin-access"), session=signin,
            )
            await signin.commit()
        release.set()
        await task
        await session.rollback()
    assert "new-signin-access" in vault._decrypt(bytes((await _connection(store)).encrypted_credential))
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)
    store.alert.assert_not_awaited()


async def test_cancelled_rotation_releases_tentative_same_identity_upload(credential_store, monkeypatch):
    store = credential_store
    started, release = threading.Event(), threading.Event()

    def refresh(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return SimpleNamespace(status_code=200, json=lambda: json.loads(
            _credential(expired=False, token="tentative-rotation-access"),
        ))

    monkeypatch.setattr(openai_codex_auth, "http_post", refresh)
    async with store.factory() as session:
        await vault.async_set_user_codex_connection(USER_A, _credential(), session=session)
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        await session.rollback()
    assert "tentative-rotation-access" in vault._decrypt(bytes((await _connection(store)).encrypted_credential))
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)


async def test_cancellation_during_tentative_writeback_preserves_rotation(credential_store, monkeypatch):
    store = credential_store
    store.refresh_request.return_value = SimpleNamespace(status_code=200, json=lambda: json.loads(
        _credential(expired=False, token="writeback-rotation-access"),
    ))
    entered, release = asyncio.Event(), asyncio.Event()
    persist = llm._async_persist_refreshed_openai_codex_db_credential
    calls = []

    async def controlled_writeback(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        await persist(**kwargs)

    monkeypatch.setattr(llm, "_async_persist_refreshed_openai_codex_db_credential", controlled_writeback)
    async with store.factory() as session:
        await vault.async_set_user_codex_connection(USER_A, _credential(), session=session)
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        await session.rollback()
    assert len(calls) == 2
    assert "writeback-rotation-access" in vault._decrypt(bytes((await _connection(store)).encrypted_credential))
    assert isinstance(await _probe(store), ProviderAuthPassedPreflightResult)
    store.refresh_request.assert_called_once()


async def test_cancellation_still_propagates_when_writeback_fails(credential_store, monkeypatch):
    store = credential_store
    store.refresh_request.return_value = SimpleNamespace(status_code=200, json=lambda: json.loads(
        _credential(expired=False, token="failed-writeback-access"),
    ))
    entered, release = asyncio.Event(), asyncio.Event()

    async def failing_writeback(**_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("fixture database unavailable")

    monkeypatch.setattr(llm, "_async_persist_refreshed_openai_codex_db_credential", failing_writeback)
    async with store.factory() as session:
        task = asyncio.create_task(llm.async_resolve_llm_client(
            user_id=USER_A, org_id=ORG, provider="openai", auth_mode="chatgpt", session=session,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_deleted_target_does_not_rollback_or_starve_recovery(credential_store, monkeypatch):
    from brain.systems.runs import work_intake
    from brain.systems.runs.chantier_continuation import (
        CONTINUATION_AUTH_BLOCKED_EVENT, CONTINUATION_RECOVERY_FAILED_EVENT,
        recover_auth_blocked_continuations,
    )
    from brain.systems.runs.events import run_event

    anchor_ids = []
    async with credential_store.factory() as session:
        runs = AsyncAgentRunStore(session)
        for index, deleted in enumerate((False, True, False)):
            anchor = await runs.create_run(AgentRunRequest(
                org_id=ORG, user_id=USER_B, thread_id=f"recovery-{index}", message="coordinate",
                model_policy={"model": MODEL},
                target_ref=({"kind": "cortex_idea", "idea_id": "deleted"} if deleted
                            else {"kind": "custom_parent_surface"}),
                metadata={"chantier_declare": {"record_id": 999}} if deleted else {},
            ))
            for status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.COMPLETED):
                await runs.set_status(anchor.id, status)
            worker = await runs.create_child_run(
                anchor, recipe=RunRecipe.WORKER, message="Verify", step_key="spawn_worker:a",
                metadata={"spawned_by_tool": True, "join_parent": True}, initial_status=RunStatus.STARTING,
            )
            for status in (RunStatus.RUNNING, RunStatus.COMPLETED):
                await runs.set_status(worker.id, status)
            await runs.append_event(run_event(anchor.id, CONTINUATION_AUTH_BLOCKED_EVENT, {
                "owner_user_id": USER_B,
                "reason": "provider_credential_unavailable: provider=openai credential=OpenAI Codex / ChatGPT",
            }))
            anchor_ids.append(anchor.id)
        await session.commit()
    monkeypatch.setattr(work_intake, "_a_get_idea_for_intake", AsyncMock(return_value=None))
    async with credential_store.factory() as session:
        assert await recover_auth_blocked_continuations(session) == 2
        await session.commit()
    async with credential_store.factory() as session:
        assert await recover_auth_blocked_continuations(session) == 0
        assert len((await session.scalars(select(AgentRunRow))).all()) == 8
        failures = (await session.scalars(select(AgentRunEventRow).where(
            AgentRunEventRow.event_type == CONTINUATION_RECOVERY_FAILED_EVENT,
        ))).all()
        assert len(failures) == 1
        assert failures[0].run_id == anchor_ids[1]
        assert failures[0].payload == {"reason": "continuation_target_unavailable"}
        for anchor_id in anchor_ids:
            assert (await session.get(AgentRunRow, anchor_id)).status == "completed"
