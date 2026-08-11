from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.cycle import (
    BehaviorChangeAudit,
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
)
from brain.platform.db.models.org import Org, User
from brain.platform import events as platform_events
from brain.systems.cycles import behavior_policy
from brain.systems.cycles import behavior_policy_contract
from brain.systems.cycles import events as cycle_events
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.behavior_policy import (
    CyclePolicyApplied,
    CyclePolicyConflict,
    CyclePolicyPatch,
    async_apply_cycle_policy_change,
    async_apply_cycle_policy_revert,
    async_list_cycle_policy_history,
    async_preview_cycle_policy_change,
    async_preview_cycle_policy_revert,
)
from brain.systems.cycles.behavior_policy_contract import CyclePolicySnapshot
from brain.systems.cycles.behavior_policy_read_model import (
    async_read_effective_cycle_policy,
)
from brain.systems.cycles.memory import async_prepare_cycle_run_memory_snapshot

pytestmark = pytest.mark.asyncio


@dataclass
class _PolicyWorkspace:
    session: object
    cycle: Cycle
    owner: CycleActor
    teammate: CycleActor
    outsider: CycleActor
    initial_revision: CycleRevision
    initial_guidance: tuple[CycleGuidance, ...]


@pytest.fixture
async def policy_workspace(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Cycle.__table__,
            CycleRevision.__table__,
            CycleGuidance.__table__,
            CycleOutputTarget.__table__,
            CycleRun.__table__,
            BehaviorChangeAudit.__table__,
        ]
    )
    org_id = str(uuid4())
    other_org_id = str(uuid4())
    owner_id = str(uuid4())
    teammate_id = str(uuid4())
    outsider_id = str(uuid4())
    session.add_all(
        [
            Org(id=org_id, name="Policy workspace", slug=f"policy-{org_id[:8]}"),
            Org(
                id=other_org_id,
                name="Other workspace",
                slug=f"other-{other_org_id[:8]}",
            ),
            User(
                id=owner_id,
                org_id=org_id,
                name="Owner",
                email=f"owner-{owner_id[:8]}@example.com",
            ),
            User(
                id=teammate_id,
                org_id=org_id,
                name="Teammate",
                email=f"teammate-{teammate_id[:8]}@example.com",
            ),
            User(
                id=outsider_id,
                org_id=other_org_id,
                name="Outsider",
                email=f"outsider-{outsider_id[:8]}@example.com",
            ),
        ]
    )
    await session.flush()
    cycle = Cycle(
        user_id=owner_id,
        org_id=org_id,
        creator_type="user",
        creator_id=owner_id,
        maintainer_type="user",
        maintainer_id=owner_id,
        name="Morning review",
        prompt="Review the workspace.",
        schedule_expr="0 9 * * *",
        timezone="UTC",
        enabled=True,
        max_concurrency=1,
        retry_policy={},
        execution_mode="reuse_same_idea",
        reopen_archived=True,
    )
    session.add(cycle)
    await session.flush()
    revision = CycleRevision(
        cycle_id=cycle.id,
        revision_number=1,
        source_type="user",
        source_id=owner_id,
        rationale="Initial definition.",
        name=cycle.name,
        prompt=cycle.prompt,
        schedule_expr=cycle.schedule_expr,
        timezone=cycle.timezone,
        enabled=cycle.enabled,
        model_override=None,
        thinking_override=None,
        execution_policy_key=None,
        target_idea_id=None,
        context_policy={"workspace_id": org_id},
    )
    session.add(revision)
    await session.flush()
    guidance = (
        CycleGuidance(
            cycle_id=cycle.id,
            revision_id=revision.id,
            source_type="user",
            source_id=owner_id,
            guidance="Keep this guidance",
            rationale="Initial definition.",
            is_active=True,
        ),
        CycleGuidance(
            cycle_id=cycle.id,
            revision_id=revision.id,
            source_type="user",
            source_id=owner_id,
            guidance="Old wording",
            rationale="Initial definition.",
            is_active=True,
        ),
    )
    session.add_all(guidance)
    await session.flush()
    monkeypatch.setattr(
        behavior_policy,
        "publish_cycle_change_strict",
        lambda **_kwargs: None,
    )
    return _PolicyWorkspace(
        session=session,
        cycle=cycle,
        owner=CycleActor(user_id=owner_id, org_id=org_id),
        teammate=CycleActor(user_id=teammate_id, org_id=org_id),
        outsider=CycleActor(user_id=outsider_id, org_id=other_org_id),
        initial_revision=revision,
        initial_guidance=guidance,
    )


async def _preview_and_apply(
    workspace: _PolicyWorkspace,
    patch: CyclePolicyPatch,
    *,
    actor: CycleActor | None = None,
    rationale: str = "Reviewed behavior change.",
    source_reference: str = "api:cycle-policy-test",
):
    acting = actor or workspace.owner
    preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=acting,
        cycle_id=workspace.cycle.id,
        proposal=patch,
    )
    result = await async_apply_cycle_policy_change(
        workspace.session,
        actor=acting,
        cycle_id=workspace.cycle.id,
        proposal=patch,
        expected_version=preview.before.version,
        preview_digest=preview.preview_digest,
        rationale=rationale,
        source_reference=source_reference,
    )
    assert isinstance(result, CyclePolicyApplied)
    return preview, result


async def test_snapshot_apply_covers_every_scalar_dataclass_field(
    policy_workspace,
    monkeypatch,
):
    workspace = policy_workspace
    snapshot = CyclePolicySnapshot.from_cycle(
        workspace.cycle,
        list(workspace.initial_guidance),
    )
    monkeypatch.setattr(
        behavior_policy_contract,
        "compute_next_run_at",
        lambda *_args: None,
    )

    for snapshot_field in fields(snapshot):
        # Guidance is deliberately excluded because the apply command writes it
        # separately through _replace_active_guidance().
        if snapshot_field.name == "guidance":
            continue

        marker = f"__snapshot_write_coverage__:{snapshot_field.name}"
        current_value = getattr(snapshot, snapshot_field.name)
        changed_value = (
            {marker: True} if isinstance(current_value, dict) else marker
        )
        mutated = replace(
            snapshot,
            **{snapshot_field.name: changed_value},
        )

        mutated.apply_to(workspace.cycle)

        assert getattr(workspace.cycle, snapshot_field.name, None) == changed_value, (
            "CyclePolicySnapshot.apply_to() did not write snapshot field "
            f"{snapshot_field.name!r}"
        )


async def test_read_preview_apply_history_and_human_audit_envelope(policy_workspace):
    workspace = policy_workspace
    effective = await async_read_effective_cycle_policy(
        workspace.session,
        actor=workspace.teammate,
        cycle_id=workspace.cycle.id,
    )
    assert effective.version == 0
    assert effective.revision_id == workspace.initial_revision.id
    assert isinstance(effective.snapshot, CyclePolicySnapshot)
    assert effective.snapshot.name == "Morning review"
    assert effective.snapshot.guidance == [
        "Keep this guidance",
        "Old wording",
    ]

    published = []
    behavior_policy.publish_cycle_change_strict = (
        lambda **payload: published.append(payload)
    )
    patch = CyclePolicyPatch(
        name="Morning policy review",
        guidance=["Keep this guidance", "New wording"],
    )
    preview, applied = await _preview_and_apply(
        workspace,
        patch,
        rationale="Use the reviewed wording.",
        source_reference="api:/cycles/1/behavior-policy",
    )

    assert isinstance(preview.after_snapshot, CyclePolicySnapshot)
    assert preview.changed_fields == ("guidance", "name")
    assert len(preview.preview_digest) == 64
    assert applied.effective_policy.version == 1
    assert workspace.cycle.name == "Morning policy review"
    assert published == [
        {
            "action": "update",
            "org_id": workspace.cycle.org_id,
            "user_id": workspace.cycle.user_id,
            "cycle_id": workspace.cycle.id,
            "target_idea_id": None,
        }
    ]

    history = await async_list_cycle_policy_history(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
    )
    assert len(history) == 1
    change = history[0]
    assert change.workspace_id == str(workspace.cycle.org_id)
    assert change.policy_kind == "cycle"
    assert change.target_type == "cycle"
    assert change.target_id == str(workspace.cycle.id)
    assert change.version == 1
    assert change.actor_type == "user"
    assert change.actor_id == workspace.owner.user_id
    assert change.source_reference == "api:/cycles/1/behavior-policy"
    assert change.rationale == "Use the reviewed wording."
    assert isinstance(change.before_snapshot, CyclePolicySnapshot)
    assert isinstance(change.after_snapshot, CyclePolicySnapshot)
    assert change.before_snapshot == preview.before.snapshot
    assert change.after_snapshot == preview.after_snapshot
    stored_change = await workspace.session.get(BehaviorChangeAudit, change.id)
    assert stored_change.before_snapshot["snapshot_version"] == 1
    assert stored_change.after_snapshot["snapshot_version"] == 1
    assert change.changed_fields == preview.changed_fields
    assert change.cycle_revision_id == applied.revision.id
    assert isinstance(change.applied_at, datetime)
    assert change.applied_at.tzinfo is not None
    assert change.reverted_from_id is None


async def test_stale_version_and_stale_digest_return_latest_policy(policy_workspace):
    workspace = policy_workspace
    first_patch = CyclePolicyPatch(name="First editor")
    second_patch = CyclePolicyPatch(name="Second editor")
    first_preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=first_patch,
    )
    second_preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=second_patch,
    )

    first = await async_apply_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=first_patch,
        expected_version=first_preview.before.version,
        preview_digest=first_preview.preview_digest,
        rationale="First editor won.",
        source_reference="editor:first",
    )
    assert isinstance(first, CyclePolicyApplied)

    stale_version = await async_apply_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=second_patch,
        expected_version=second_preview.before.version,
        preview_digest=second_preview.preview_digest,
        rationale="Second editor tried.",
        source_reference="editor:second",
    )
    assert isinstance(stale_version, CyclePolicyConflict)
    assert stale_version.reason == "stale_version"
    assert stale_version.latest_effective_policy.version == 1
    assert stale_version.latest_effective_policy.snapshot.name == "First editor"

    fresh_preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=second_patch,
    )
    stale_digest = await async_apply_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=second_patch,
        expected_version=fresh_preview.before.version,
        preview_digest="0" * 64,
        rationale="Digest must match.",
        source_reference="editor:second",
    )
    assert isinstance(stale_digest, CyclePolicyConflict)
    assert stale_digest.reason == "stale_preview_digest"
    assert stale_digest.latest_effective_policy.version == 1
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 1


async def test_apply_requires_nonempty_rationale(policy_workspace):
    workspace = policy_workspace
    patch = CyclePolicyPatch(name="Unreviewed name")
    preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=patch,
    )
    with pytest.raises(ValueError, match="rationale is required"):
        await async_apply_cycle_policy_change(
            workspace.session,
            actor=workspace.owner,
            cycle_id=workspace.cycle.id,
            proposal=patch,
            expected_version=preview.before.version,
            preview_digest=preview.preview_digest,
            rationale="   ",
            source_reference="api:test",
        )
    assert workspace.cycle.name == "Morning review"


async def test_guidance_wording_replacement_retires_without_deleting(policy_workspace):
    workspace = policy_workspace
    keep_row, old_row = workspace.initial_guidance
    _, applied = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(guidance=["Keep this guidance", "New wording"]),
    )
    rows = list(
        (
            await workspace.session.scalars(
                select(CycleGuidance)
                .where(CycleGuidance.cycle_id == workspace.cycle.id)
                .order_by(CycleGuidance.id.asc())
            )
        ).all()
    )
    assert [(row.guidance, row.is_active) for row in rows] == [
        ("Keep this guidance", True),
        ("Old wording", False),
        ("New wording", True),
    ]
    assert rows[0].id == keep_row.id
    assert rows[1].id == old_row.id
    assert rows[2].id not in {keep_row.id, old_row.id}
    assert rows[2].revision_id == applied.revision.id


async def test_apply_failure_rolls_back_the_entire_policy_write_set(
    policy_workspace,
    monkeypatch,
):
    workspace = policy_workspace
    cycle_id = workspace.cycle.id
    patch = CyclePolicyPatch(
        name="Must roll back",
        guidance=["Replacement guidance"],
    )
    preview = await async_preview_cycle_policy_change(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        proposal=patch,
    )

    def fail_publish(_event_type, _payload):
        raise RuntimeError("event publish failed")

    monkeypatch.setattr(
        behavior_policy,
        "publish_cycle_change_strict",
        cycle_events.publish_cycle_change_strict,
    )
    monkeypatch.setattr(platform_events, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="event publish failed"):
        await async_apply_cycle_policy_change(
            workspace.session,
            actor=workspace.owner,
            cycle_id=workspace.cycle.id,
            proposal=patch,
            expected_version=preview.before.version,
            preview_digest=preview.preview_digest,
            rationale="Verify atomic rollback.",
            source_reference="test:rollback",
        )

    workspace.session.expire_all()
    cycle = await workspace.session.get(Cycle, cycle_id)
    guidance = list(
        (
            await workspace.session.scalars(
                select(CycleGuidance)
                .where(CycleGuidance.cycle_id == cycle_id)
                .order_by(CycleGuidance.id.asc())
            )
        ).all()
    )
    assert cycle.name == "Morning review"
    assert [(row.guidance, row.is_active) for row in guidance] == [
        ("Keep this guidance", True),
        ("Old wording", True),
    ]
    assert await workspace.session.scalar(
        select(func.count(CycleRevision.id)).where(
            CycleRevision.cycle_id == cycle_id
        )
    ) == 1
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 0


async def test_revert_uses_reviewed_apply_and_creates_a_new_version(policy_workspace):
    workspace = policy_workspace
    _, first = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(name="Changed policy"),
        rationale="Make the first change.",
    )
    revert_preview = await async_preview_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
    )
    assert revert_preview.after_snapshot.name == "Morning review"
    assert revert_preview.reverted_from_id == first.change.id

    reverted = await async_apply_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
        expected_version=revert_preview.before.version,
        preview_digest=revert_preview.preview_digest,
        rationale="Revert the reviewed change.",
        source_reference="api:revert",
    )
    assert isinstance(reverted, CyclePolicyApplied)
    assert reverted.effective_policy.version == 2
    assert reverted.effective_policy.snapshot.name == "Morning review"
    assert reverted.change.reverted_from_id == first.change.id

    history = await async_list_cycle_policy_history(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
    )
    assert [change.version for change in history] == [2, 1]
    assert history[1].after_snapshot.name == "Changed policy"


async def test_revert_decodes_stored_snapshot_across_schema_changes(policy_workspace):
    workspace = policy_workspace
    _, first = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(name="Changed policy"),
    )
    stored_change = await workspace.session.get(BehaviorChangeAudit, first.change.id)
    stored_before = dict(stored_change.before_snapshot)
    stored_before.pop("max_concurrency")
    stored_before["retired_policy_field"] = "legacy value"
    stored_change.before_snapshot = stored_before
    await workspace.session.flush()

    history = await async_list_cycle_policy_history(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
    )
    assert history[0].before_snapshot.max_concurrency == 1
    assert not hasattr(history[0].before_snapshot, "retired_policy_field")

    revert_preview = await async_preview_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
    )
    assert revert_preview.after_snapshot.name == "Morning review"
    assert revert_preview.after_snapshot.max_concurrency == 1
    assert not hasattr(revert_preview.after_snapshot, "retired_policy_field")

    reverted = await async_apply_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
        expected_version=revert_preview.before.version,
        preview_digest=revert_preview.preview_digest,
        rationale="Revert a snapshot written with an older schema.",
        source_reference="api:compat-revert",
    )
    assert isinstance(reverted, CyclePolicyApplied)
    assert reverted.effective_policy.version == 2
    assert isinstance(reverted.effective_policy.snapshot, CyclePolicySnapshot)
    new_stored_change = await workspace.session.get(
        BehaviorChangeAudit,
        reverted.change.id,
    )
    assert new_stored_change.before_snapshot["snapshot_version"] == 1
    assert new_stored_change.after_snapshot["snapshot_version"] == 1


async def test_revert_decodes_snapshot_written_before_versioning(policy_workspace):
    workspace = policy_workspace
    _, first = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(name="Changed policy"),
    )
    stored_change = await workspace.session.get(BehaviorChangeAudit, first.change.id)
    stored_before = dict(stored_change.before_snapshot)
    stored_before.pop("snapshot_version")
    stored_change.before_snapshot = stored_before
    await workspace.session.flush()

    history = await async_list_cycle_policy_history(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
    )
    assert history[0].before_snapshot.name == "Morning review"

    revert_preview = await async_preview_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
    )
    assert revert_preview.after_snapshot.name == "Morning review"

    reverted = await async_apply_cycle_policy_revert(
        workspace.session,
        actor=workspace.owner,
        cycle_id=workspace.cycle.id,
        change_id=first.change.id,
        expected_version=revert_preview.before.version,
        preview_digest=revert_preview.preview_digest,
        rationale="Revert a snapshot written before versioning.",
        source_reference="api:legacy-revert",
    )
    assert isinstance(reverted, CyclePolicyApplied)
    assert reverted.effective_policy.snapshot.name == "Morning review"


@pytest.mark.parametrize("invalid_version", [False, 0, -1, "1"])
async def test_snapshot_decode_rejects_invalid_versions(
    policy_workspace,
    invalid_version,
):
    current = await async_read_effective_cycle_policy(
        policy_workspace.session,
        actor=policy_workspace.owner,
        cycle_id=policy_workspace.cycle.id,
    )
    encoded = current.snapshot.encode()
    encoded["snapshot_version"] = invalid_version

    with pytest.raises(ValueError, match="invalid version"):
        CyclePolicySnapshot.decode(encoded, current=current.snapshot)


async def test_admitted_run_keeps_snapshot_and_next_run_gets_new_policy(policy_workspace):
    workspace = policy_workspace
    first_run = CycleRun(
        cycle_id=workspace.cycle.id,
        scheduled_for=datetime.now(timezone.utc),
        prompt_snapshot=workspace.cycle.prompt,
        status="queued",
        context_snapshot={},
    )
    workspace.session.add(first_run)
    await workspace.session.flush()
    await async_prepare_cycle_run_memory_snapshot(
        workspace.session,
        workspace.cycle,
        first_run,
    )
    first_revision_id = first_run.revision_id
    first_guidance = list(first_run.guidance_snapshot)
    first_context = dict(first_run.context_snapshot)

    _, applied = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(
            prompt="Use the new policy.",
            guidance=["New admission guidance"],
        ),
    )
    second_run = CycleRun(
        cycle_id=workspace.cycle.id,
        scheduled_for=datetime.now(timezone.utc),
        prompt_snapshot=workspace.cycle.prompt,
        status="queued",
        context_snapshot={},
    )
    workspace.session.add(second_run)
    await workspace.session.flush()
    await async_prepare_cycle_run_memory_snapshot(
        workspace.session,
        workspace.cycle,
        second_run,
    )

    assert first_run.revision_id == first_revision_id == workspace.initial_revision.id
    assert first_run.guidance_snapshot == first_guidance
    assert first_run.context_snapshot == first_context
    assert "behavior_change" not in first_run.context_snapshot
    assert second_run.revision_id == applied.revision.id
    assert [row["guidance"] for row in second_run.guidance_snapshot] == [
        "New admission guidance"
    ]
    assert second_run.context_snapshot["revision"]["id"] == applied.revision.id
    assert second_run.context_snapshot["behavior_change"]["id"] == applied.change.id


async def test_workspace_authorization_and_delegated_agent_attribution(policy_workspace):
    workspace = policy_workspace
    same_workspace = await async_read_effective_cycle_policy(
        workspace.session,
        actor=workspace.teammate,
        cycle_id=workspace.cycle.id,
    )
    assert same_workspace.workspace_id == str(workspace.cycle.org_id)

    with pytest.raises(ValueError, match="Cycle not found"):
        await async_read_effective_cycle_policy(
            workspace.session,
            actor=workspace.outsider,
            cycle_id=workspace.cycle.id,
        )

    _, teammate_change = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(enabled=False),
        actor=workspace.teammate,
        rationale="A workspace teammate reviewed this change.",
        source_reference="api:teammate",
    )
    assert teammate_change.change.actor_type == "user"
    assert teammate_change.change.actor_id == workspace.teammate.user_id

    agent = CycleActor(
        user_id=workspace.teammate.user_id,
        org_id=workspace.teammate.org_id,
        principal_type="agent",
        source_id="agent-run-42",
    )
    _, applied = await _preview_and_apply(
        workspace,
        CyclePolicyPatch(enabled=True),
        actor=agent,
        rationale="Agent applied a delegated reviewed change.",
        source_reference="agent_run:42",
    )
    assert applied.change.actor_type == "agent"
    assert applied.change.actor_id == "agent-run-42"
    assert applied.change.source_reference == "agent_run:42"
    assert applied.change.rationale == "Agent applied a delegated reviewed change."
    assert applied.change.before_snapshot.enabled is False
    assert applied.change.after_snapshot.enabled is True
    assert applied.change.version == 2
    assert applied.change.applied_at.tzinfo is not None
