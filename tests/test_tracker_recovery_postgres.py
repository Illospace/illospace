"""Recovery and live fanout use a consistent PostgreSQL lock order."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.org import Org, User
from brain.systems.inbound import admin, service as inbound
from brain.systems.inbound.github_webhook import github_event_to_envelope
from brain.systems.inbound.tracker_recovery import recover_tracker_snapshot
from brain.systems.user_domains.service import AsyncDomainService


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_recovery_overlaps_secondary_projection_webhook_without_deadlock(db_engine, monkeypatch):
    if db_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    org_id, user_id = str(uuid4()), str(uuid4())
    tasks = []
    key_held, release_webhook, recovery_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    now = datetime.now(timezone.utc)
    subject = {
        "number": 1, "title": "Current issue", "state": "closed", "node_id": "test-recovery-node",
        "html_url": "https://github.com/owner/repo/issues/1", "user": {"login": "original-author"},
        "updated_at": (now - timedelta(minutes=1)).isoformat(),
    }
    snapshot = {"captured_at": now.isoformat(), "items": [{"event": "issues", "repository": "owner/repo", "subject": subject}]}
    envelope = github_event_to_envelope("issues", {"repository": {"full_name": "owner/repo"}, "issue": subject})

    def observe_key_query(_conn, _cursor, statement, _parameters, _context, _many):
        if "inbound_domain_projection_keys" in statement and " IN (" in statement and "FOR UPDATE" in statement:
            recovery_waiting.set()

    try:
        async with sessions() as session:
            session.add_all([
                Org(id=org_id, name="Recovery test", slug=f"recovery-{uuid4().hex}"),
                User(id=user_id, org_id=org_id, name="Recovery test", email=f"{uuid4().hex}@example.com", approved=True),
            ])
            await session.flush()
            connection = await admin.create_connection(session, org_id=org_id, owner_user_id=user_id, display_name="GitHub", agent_kind="github")
            policy = await admin.create_policy(session, org_id=org_id, connection_id=connection.id, name="GitHub", origin_patterns=["github:*"], envelope_kinds=["github_event"])
            projections = []
            for name in ("Feed", "Tracker"):
                domain = await AsyncDomainService(session).create_domain(org_id, name=name, objects=[{
                    "key": "issue", "fields": [{"key": key, "field_type": "text"} for key in ("external_id", "summary", "status")],
                }])
                projections.append(await admin.create_projection(
                    session, org_id=org_id, connection_id=connection.id, policy_id=policy.id,
                    domain_id=domain.id, object_key="issue", external_id_field="external_id",
                    external_id_path="github:{hints.repo}:issue:{hints.number}",
                    field_mapping={"summary": "payload.issue.title", "status": "hints.issue_outcome"},
                ))
            target = projections[1]
            projections[0].created_at = now - timedelta(seconds=2)
            target.created_at = now - timedelta(seconds=1)
            principal = {"connection_id": connection.id, "org_id": org_id, "owner_user_id": user_id, "scopes": ["signal:submit"]}
            opened = github_event_to_envelope("issues", {"repository": {"full_name": "owner/repo"}, "issue": {**subject, "state": "open"}})
            await inbound.submit_inbound_envelope(session, connection=principal, envelope=opened)
            plan = await recover_tracker_snapshot(session, org_id=org_id, projection_id=target.id, snapshot=snapshot)
            await session.commit()

        original_apply = inbound._apply_domain_projection

        async def pause_secondary_projection(session, **kwargs):
            if kwargs["projection"].id == target.id:
                await inbound._get_projection_key(session, target, external_id="github:owner/repo:issue:1")
                key_held.set()
                await release_webhook.wait()
            return await original_apply(session, **kwargs)

        monkeypatch.setattr(inbound, "_apply_domain_projection", pause_secondary_projection)
        event.listen(db_engine.sync_engine, "before_cursor_execute", observe_key_query)

        async def webhook():
            async with sessions() as session:
                await session.execute(text("SET LOCAL lock_timeout = '5s'"))
                result = await inbound.submit_inbound_envelope(session, connection=principal, envelope=envelope)
                assert result["status"] == inbound.STATUS_PROCESSED
                await session.commit()

        async def recovery():
            async with sessions() as session:
                await session.execute(text("SET LOCAL lock_timeout = '5s'"))
                result = await recover_tracker_snapshot(session, org_id=org_id, projection_id=target.id, snapshot=snapshot, apply=True, reviewed_plan=plan)
                assert result["items"][0]["operation"] == "unchanged"
                await session.commit()

        tasks.append(asyncio.create_task(webhook()))
        await asyncio.wait_for(key_held.wait(), 5)
        tasks.append(asyncio.create_task(recovery()))
        await asyncio.wait_for(recovery_waiting.wait(), 5)
        release_webhook.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        async with sessions() as session:
            records = (await session.scalars(select(DomainRecord).where(DomainRecord.org_id == org_id))).all()
            assert len(records) == 2 and all(row.data["status"] == "closed" for row in records)
    finally:
        release_webhook.set()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if event.contains(db_engine.sync_engine, "before_cursor_execute", observe_key_query):
            event.remove(db_engine.sync_engine, "before_cursor_execute", observe_key_query)
        async with sessions() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.execute(delete(Org).where(Org.id == org_id))
            await session.commit()
