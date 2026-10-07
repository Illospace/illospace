"""Credential expiry races through production transactions and PostgreSQL locks."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain.platform.db.models.org import Org, User, UserCodexConnection
from brain.platform.db.repositories import unit_of_work
from brain.platform.integrations.openai_codex_auth import (
    OpenAICodexCredential,
    encode_codex_auth_payload,
)
from brain.systems import vault
from brain.systems.vault import codex_health

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]


def _credential(token: str, *, expired: bool) -> str:
    return json.dumps(encode_codex_auth_payload(OpenAICodexCredential(
        access_token=token,
        refresh_token=f"fixture-refresh-{token}",
        account_id="fixture-account",
        expires_at=time.time() + (-100 if expired else 3600),
        auth_mode="chatgpt",
    )))


async def _wait_for_row_lock(engine, app_name: str, tasks, *, count: int, blocker=None):
    """Observe actual contention; elapsed time alone cannot establish a race."""
    async with asyncio.timeout(15):
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            while True:
                blocked = await connection.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = :app_name "
                    "AND wait_event_type = 'Lock' "
                    "AND cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND (CAST(:blocker AS integer) IS NULL "
                    "OR CAST(:blocker AS integer) = ANY(pg_blocking_pids(pid))) "
                    "AND query ILIKE '%user_codex_connections%'"
                ), {"app_name": app_name, "blocker": blocker})
                if blocked >= count:
                    return
                for task in tasks:
                    if task.done():
                        task.result()
                        pytest.fail("credential operation finished before the row-lock race")
                await asyncio.sleep(0.05)


async def test_expiry_and_alert_are_serialized_and_stale_failure_preserves_replacement(
    db_engine, monkeypatch,
):
    assert db_engine.dialect.name == "postgresql"
    org_id, user_id = str(uuid4()), str(uuid4())
    app_name = f"codex-health-{uuid4().hex}"
    factory = async_sessionmaker(
        db_engine, expire_on_commit=False, info={"credential_race": app_name},
    )
    # Bind the real UnitOfWork to the test DB; its commit, rollback, and locks
    # remain production behavior. The event only labels our backends for polling.
    monkeypatch.setattr(unit_of_work, "SessionFactory", factory)
    monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())

    def label_backend(session, _transaction, connection):
        if session.info.get("credential_race") == app_name:
            connection.execute(
                text("SELECT set_config('application_name', :app_name, true)"),
                {"app_name": app_name},
            )

    session_class = factory.class_.sync_session_class
    event.listen(session_class, "after_begin", label_backend)
    delivery_entered, release_delivery = asyncio.Event(), asyncio.Event()
    alert_ids = []
    tasks = []

    async def deliver_alert(**kwargs):
        alert_ids.append(kwargs["policy"].client_msg_id)
        delivery_entered.set()
        await release_delivery.wait()

    monkeypatch.setattr(codex_health, "async_deliver_failure_alert", deliver_alert)
    stale = _credential("fixture-stale-access", expired=True)
    replacement = _credential("fixture-replacement-access", expired=False)

    async def connection_state():
        async with factory() as session:
            return (await session.scalars(select(UserCodexConnection).where(
                UserCodexConnection.user_id == user_id,
            ))).one()

    try:
        async with asyncio.timeout(40):
            async with factory.begin() as session:
                session.add(Org(id=org_id, name="Credential race", slug=app_name))
                await session.flush()
                session.add(User(
                    id=user_id, org_id=org_id, name="Fixture", email=f"{user_id}@example.test",
                ))
                await session.flush()
                connection_id = await vault.async_set_user_codex_connection(
                    user_id, stale, session=session,
                )

            # Both public expiry operations must wait on the same committed row.
            async with factory() as holder:
                await holder.scalar(select(UserCodexConnection).where(
                    UserCodexConnection.id == connection_id,
                ).with_for_update())
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                marks = [asyncio.create_task(codex_health.mark_codex_credential_expired(
                    user_id=user_id, credential_payload=stale,
                )) for _ in range(2)]
                tasks.extend(marks)
                await _wait_for_row_lock(db_engine, app_name, marks, count=1, blocker=holder_pid)
                # PostgreSQL can queue B behind A's tuple lock, so B names A
                # rather than the original holder as its immediate blocker.
                await _wait_for_row_lock(db_engine, app_name, marks, count=2)
                await holder.commit()

            await asyncio.wait_for(delivery_entered.wait(), timeout=10)
            during_delivery = await connection_state()
            assert during_delivery.credential_error_code == "credential_expired"
            assert during_delivery.credential_error_at is not None
            assert during_delivery.credential_alerted_at is None
            episode_at = during_delivery.credential_error_at

            retries = [asyncio.create_task(codex_health.retry_codex_credential_alert(
                connection_id=connection_id,
            )) for _ in range(2)]
            tasks.extend(retries)
            await _wait_for_row_lock(db_engine, app_name, retries, count=2)
            release_delivery.set()
            assert await asyncio.gather(*marks) == [True, True]
            await asyncio.gather(*retries)
            assert len(alert_ids) == 1
            assert alert_ids[0]
            settled = await connection_state()
            assert settled.credential_error_at == episode_at
            assert settled.credential_alerted_at is not None
            assert settled.is_active
            assert await codex_health.mark_codex_credential_expired(
                user_id=user_id, credential_payload=stale,
            )
            assert (await connection_state()).credential_error_at == episode_at
            assert len(alert_ids) == 1

            # A failed run has a cached old row. Its independent durable expiry
            # write must wait for sign-in, then compare the committed replacement.
            async with factory() as failed_run, factory() as sign_in:
                cached = await failed_run.get(UserCodexConnection, connection_id)
                assert cached.credential_error_code == "credential_expired"
                assert await vault.async_set_user_codex_connection(
                    user_id, replacement, session=sign_in,
                ) == connection_id
                sign_in_pid = await sign_in.scalar(text("SELECT pg_backend_pid()"))
                stale_failure = asyncio.create_task(codex_health.mark_codex_credential_expired(
                    user_id=user_id, credential_payload=stale, session=failed_run,
                ))
                tasks.append(stale_failure)
                await _wait_for_row_lock(
                    db_engine, app_name, [stale_failure], count=1, blocker=sign_in_pid,
                )
                await sign_in.commit()
                assert await stale_failure is False

            restored = await connection_state()
            assert restored.id == connection_id
            assert vault._decrypt(bytes(restored.encrypted_credential)) == replacement
            assert restored.is_active
            assert restored.credential_error_code is None
            assert restored.credential_error_at is None
            assert restored.credential_alerted_at is None
            assert len(alert_ids) == 1
    finally:
        release_delivery.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            async with factory.begin() as session:
                await session.execute(delete(UserCodexConnection).where(
                    UserCodexConnection.user_id == user_id,
                ))
                await session.execute(delete(User).where(User.id == user_id))
                await session.execute(delete(Org).where(Org.id == org_id))
        finally:
            event.remove(session_class, "after_begin", label_backend)
