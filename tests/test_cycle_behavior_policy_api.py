from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from brain.app.api.routers import cycles as cycles_router
from brain.app.api.schemas.cycles import (
    CyclePolicyApplyRead,
    CyclePolicyApplyRequest,
    CyclePolicyConfigurationRead,
    CyclePolicyHistoryRead,
    CyclePolicyPreviewRead,
    CyclePolicyPreviewRequest,
    CyclePolicyProposal,
    CyclePolicyRevertApplyRequest,
    EffectiveCyclePolicyRead,
)
from brain.platform.db.models.cycle import (
    BehaviorChangeAudit,
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
)
from brain.platform.db.models.org import Org, User
from brain.systems.cycles import behavior_policy
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.serializers import serialize_cycle_policy_preview

pytestmark = pytest.mark.asyncio


@dataclass
class _ApiWorkspace:
    session: object
    cycle: Cycle
    owner: dict
    outsider: dict
    initial_revision: CycleRevision
    output_target: CycleOutputTarget
    published_events: list[dict]


@pytest.fixture
async def api_workspace(
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
            BehaviorChangeAudit.__table__,
        ]
    )
    org_id = str(uuid4())
    other_org_id = str(uuid4())
    owner_id = str(uuid4())
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
        creator_type="human",
        creator_id=owner_id,
        maintainer_type="human",
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
        source_type="human",
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
    session.add_all(
        [
            CycleGuidance(
                cycle_id=cycle.id,
                revision_id=revision.id,
                source_type="human",
                source_id=owner_id,
                guidance="Keep reports concise",
                rationale="Initial definition.",
                is_active=True,
            ),
            CycleGuidance(
                cycle_id=cycle.id,
                revision_id=revision.id,
                source_type="human",
                source_id=owner_id,
                guidance="Report blockers",
                rationale="Initial definition.",
                is_active=True,
            ),
        ]
    )
    output_target = CycleOutputTarget(
        cycle_id=cycle.id,
        revision_id=revision.id,
        target_type="cycle_ledger",
        target_id=str(cycle.id),
        label="Cycle ledger",
        config={"format": "summary"},
        source_type="system",
        source_id="cycle-defaults",
        rationale="Keep a durable result.",
        is_active=True,
    )
    session.add(output_target)
    await session.flush()
    published_events: list[dict] = []
    monkeypatch.setattr(
        behavior_policy,
        "publish_cycle_change_strict",
        lambda **payload: published_events.append(payload),
    )
    return _ApiWorkspace(
        session=session,
        cycle=cycle,
        owner={
            "id": owner_id,
            "org_id": org_id,
            "principal_type": "human",
        },
        outsider={
            "id": outsider_id,
            "org_id": other_org_id,
            "principal_type": "human",
        },
        initial_revision=revision,
        output_target=output_target,
        published_events=published_events,
    )


def _preview_body(**proposal) -> CyclePolicyPreviewRequest:
    return CyclePolicyPreviewRequest(
        proposal=CyclePolicyProposal(**proposal),
    )


async def _preview_and_apply(
    workspace: _ApiWorkspace,
    *,
    rationale: str = "Apply the reviewed policy.",
    **proposal,
) -> dict:
    preview_body = _preview_body(**proposal)
    preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        preview_body,
        db=workspace.session,
        user=workspace.owner,
    )
    return await cycles_router.apply_cycle_behavior_policy(
        workspace.cycle.id,
        CyclePolicyApplyRequest(
            proposal=preview_body.proposal,
            expected_version=preview["expected_version"],
            preview_digest=preview["preview_digest"],
            rationale=rationale,
        ),
        db=workspace.session,
        user=workspace.owner,
    )


async def test_effective_policy_shape_includes_sources_and_read_only_targets(
    api_workspace,
):
    workspace = api_workspace

    payload = await cycles_router.get_cycle_behavior_policy(
        workspace.cycle.id,
        db=workspace.session,
        user=workspace.owner,
    )

    EffectiveCyclePolicyRead.model_validate(payload)
    assert payload["version"] == 0
    assert payload["configuration"]["prompt"] == "Review the workspace."
    assert payload["configuration"]["schedule_human"]
    assert payload["guidance"] == [
        "Keep reports concise",
        "Report blockers",
    ]
    assert payload["editable_fields"] == list(CyclePolicyProposal.model_fields)
    assert payload["output_targets_read_only"] is True
    assert payload["output_targets"] == [
        {
            "id": workspace.output_target.id,
            "target_type": "cycle_ledger",
            "target_id": str(workspace.cycle.id),
            "label": "Cycle ledger",
            "config": {"format": "summary"},
            "source_type": "system",
            "source_id": "cycle-defaults",
            "rationale": "Keep a durable result.",
            "created_at": payload["output_targets"][0]["created_at"],
            "updated_at": payload["output_targets"][0]["updated_at"],
        }
    ]
    assert payload["source"]["revision_id"] == workspace.initial_revision.id
    assert payload["source"]["actor_type"] == "human"
    assert payload["source"]["source_reference"] is None
    assert payload["field_sources"]["prompt"] == {
        "version": 0,
        "cycle_revision_id": workspace.initial_revision.id,
        "actor_type": "human",
        "actor_id": workspace.owner["id"],
        "source_reference": f"cycle_revision:{workspace.initial_revision.id}",
        "rationale": "Initial definition.",
        "changed_at": payload["field_sources"]["prompt"]["changed_at"],
        "change_id": None,
    }
    assert payload["latest_change"] is None
    for value in (
        payload["output_targets"][0]["created_at"],
        payload["output_targets"][0]["updated_at"],
        payload["source"]["changed_at"],
        payload["field_sources"]["prompt"]["changed_at"],
    ):
        assert value.tzinfo is not None
        assert value.utcoffset() == timedelta(0)


async def test_snapshot_contract_derives_api_field_plumbing():
    snapshot_type = behavior_policy.CyclePolicySnapshot
    assert set(CyclePolicyConfigurationRead.model_fields) == {
        *snapshot_type.configuration_field_names(),
        "schedule_human",
    }
    assert tuple(CyclePolicyProposal.model_fields) == (
        snapshot_type.api_editable_field_names()
    )

    proposal = CyclePolicyProposal(
        prompt="A complete proposal.",
        schedule_expr="0 10 * * *",
        timezone="America/Toronto",
        enabled=False,
        model_override=None,
        thinking_override=None,
        guidance=["Keep the contract derived"],
    )
    proposal_values = proposal.model_dump(exclude_unset=True)
    patch = cycles_router._policy_patch(
        CyclePolicyPreviewRequest(proposal=proposal)
    )

    assert patch.changes == proposal_values


async def test_throwaway_scalar_reaches_preview_history_and_conflict(
    api_workspace,
    monkeypatch,
):
    workspace = api_workspace

    @dataclass(frozen=True)
    class SnapshotWithThrowawayScalar(behavior_policy.CyclePolicySnapshot):
        throwaway_scalar: str = "before"

    workspace.cycle.throwaway_scalar = "before"
    monkeypatch.setattr(
        behavior_policy,
        "CyclePolicySnapshot",
        SnapshotWithThrowawayScalar,
    )
    actor = CycleActor.from_user_payload(workspace.owner)

    stale_body = _preview_body(prompt="A stale editor draft.")
    stale_preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        stale_body,
        db=workspace.session,
        user=workspace.owner,
    )

    patch = behavior_policy.CyclePolicyPatch(
        throwaway_scalar="after",
    )
    preview = await behavior_policy.async_preview_cycle_policy_change(
        workspace.session,
        actor=actor,
        cycle_id=workspace.cycle.id,
        proposal=patch,
    )
    preview_payload = serialize_cycle_policy_preview(preview)
    assert preview_payload["changed_fields"] == ["throwaway_scalar"]
    assert preview_payload["after"]["configuration"]["throwaway_scalar"] == (
        "after"
    )
    assert preview_payload["diff"] == [
        {
            "field": "throwaway_scalar",
            "kind": "value",
            "before": "before",
            "after": "after",
        }
    ]

    result = await behavior_policy.async_apply_cycle_policy_change(
        workspace.session,
        actor=actor,
        cycle_id=workspace.cycle.id,
        proposal=patch,
        expected_version=preview.before.version,
        preview_digest=preview.preview_digest,
        rationale="Prove the derived field contract.",
        source_reference="test:throwaway-policy-field",
    )
    assert isinstance(result, behavior_policy.CyclePolicyApplied)
    assert workspace.cycle.throwaway_scalar == "after"
    assert result.change.after_snapshot.throwaway_scalar == "after"

    history = await cycles_router.list_cycle_behavior_policy_history(
        workspace.cycle.id,
        limit=50,
        offset=0,
        db=workspace.session,
        user=workspace.owner,
    )
    assert history["items"][0]["before_snapshot"]["configuration"][
        "throwaway_scalar"
    ] == "before"
    assert history["items"][0]["after_snapshot"]["configuration"][
        "throwaway_scalar"
    ] == "after"

    with pytest.raises(HTTPException) as conflict:
        await cycles_router.apply_cycle_behavior_policy(
            workspace.cycle.id,
            CyclePolicyApplyRequest(
                proposal=stale_body.proposal,
                expected_version=stale_preview["expected_version"],
                preview_digest=stale_preview["preview_digest"],
                rationale="This editor draft is stale.",
            ),
            db=workspace.session,
            user=workspace.owner,
        )

    assert conflict.value.status_code == 409
    latest = conflict.value.detail["latest_effective_policy"]
    assert latest["configuration"]["throwaway_scalar"] == "after"


async def test_preview_returns_normalized_field_aware_diff_without_writing(
    api_workspace,
):
    workspace = api_workspace
    payload = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        _preview_body(
            prompt="  Review incidents and owners.  ",
            schedule_expr="30 10 * * 1",
            guidance=["New guidance", "Keep reports concise"],
        ),
        db=workspace.session,
        user=workspace.owner,
    )

    CyclePolicyPreviewRead.model_validate(payload)
    assert payload["expected_version"] == 0
    assert len(payload["preview_digest"]) == 64
    assert payload["after"]["configuration"]["prompt"] == (
        "Review incidents and owners."
    )
    assert payload["changed_fields"] == [
        "guidance",
        "prompt",
        "schedule_expr",
    ]
    diff = {entry["field"]: entry for entry in payload["diff"]}
    assert diff["guidance"] == {
        "field": "guidance",
        "kind": "collection",
        "before": ["Keep reports concise", "Report blockers"],
        "after": ["Keep reports concise", "New guidance"],
        "added": ["New guidance"],
        "removed": ["Report blockers"],
    }
    assert diff["schedule_expr"]["kind"] == "schedule"
    assert diff["schedule_expr"]["before"]["schedule_human"]
    assert diff["schedule_expr"]["after"]["schedule_expr"] == "30 10 * * 1"
    assert payload["affected_runs"] == {
        "admitted_runs": "unchanged",
        "future_runs": "use_proposed_policy_after_apply",
    }
    assert payload["warnings"][0]["code"] == "admitted_runs_unchanged"
    assert workspace.cycle.prompt == "Review the workspace."
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 0


@pytest.mark.parametrize(
    ("proposal", "error"),
    [
        ({"schedule_expr": "not a schedule"}, "schedule"),
        ({"prompt": "   "}, "prompt is required"),
        ({"model_override": "openai/not-a-model"}, "Unknown model_override"),
        (
            {"guidance": ["Repeated guidance", "Repeated guidance"]},
            "guidance entries must be unique",
        ),
    ],
)
async def test_preview_rejects_invalid_policy_before_apply(
    api_workspace,
    proposal,
    error,
):
    workspace = api_workspace

    with pytest.raises(HTTPException) as caught:
        await cycles_router.preview_cycle_behavior_policy(
            workspace.cycle.id,
            _preview_body(**proposal),
            db=workspace.session,
            user=workspace.owner,
        )

    assert caught.value.status_code == 400
    assert error.lower() in str(caught.value.detail).lower()
    assert workspace.cycle.prompt == "Review the workspace."
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 0


async def test_apply_updates_policy_audit_event_and_utc_source(api_workspace):
    workspace = api_workspace

    payload = await _preview_and_apply(
        workspace,
        prompt="Review incidents and owners.",
        guidance=["Keep reports concise", "Name every owner"],
        rationale="Make incident ownership explicit.",
    )

    CyclePolicyApplyRead.model_validate(payload)
    assert payload["effective_policy"]["version"] == 1
    assert payload["effective_policy"]["configuration"]["prompt"] == (
        "Review incidents and owners."
    )
    assert payload["effective_policy"]["latest_change"]["id"] == (
        payload["change"]["id"]
    )
    assert payload["effective_policy"]["field_sources"]["prompt"]["version"] == 1
    assert payload["effective_policy"]["field_sources"]["guidance"]["version"] == 1
    assert payload["effective_policy"]["field_sources"]["name"]["version"] == 0
    assert payload["change"]["actor_type"] == "human"
    assert payload["change"]["actor_id"] == workspace.owner["id"]
    assert payload["change"]["source_reference"] == (
        f"api:/cycles/{workspace.cycle.id}/behavior-policy"
    )
    assert payload["change"]["rationale"] == (
        "Make incident ownership explicit."
    )
    assert payload["change"]["applied_at"].utcoffset() == timedelta(0)
    assert payload["effective_policy"]["source"]["changed_at"].utcoffset() == (
        timedelta(0)
    )
    assert workspace.published_events == [
        {
            "action": "update",
            "org_id": workspace.cycle.org_id,
            "user_id": workspace.cycle.user_id,
            "cycle_id": workspace.cycle.id,
            "target_idea_id": None,
        }
    ]
    stored = await workspace.session.get(BehaviorChangeAudit, payload["change"]["id"])
    assert stored is not None
    assert stored.version == 1


async def test_apply_rejects_whitespace_only_rationale(api_workspace):
    workspace = api_workspace
    body = _preview_body(prompt="A reviewed draft.")
    preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        body,
        db=workspace.session,
        user=workspace.owner,
    )

    with pytest.raises(HTTPException) as caught:
        await cycles_router.apply_cycle_behavior_policy(
            workspace.cycle.id,
            CyclePolicyApplyRequest(
                proposal=body.proposal,
                expected_version=preview["expected_version"],
                preview_digest=preview["preview_digest"],
                rationale="   ",
            ),
            db=workspace.session,
            user=workspace.owner,
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == "rationale is required"
    assert workspace.cycle.prompt == "Review the workspace."
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 0


async def test_apply_returns_exact_conflicts_with_latest_effective_policy(
    api_workspace,
):
    workspace = api_workspace
    first_body = _preview_body(prompt="First editor won.")
    stale_body = _preview_body(prompt="Second editor draft.")
    first_preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        first_body,
        db=workspace.session,
        user=workspace.owner,
    )
    stale_preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        stale_body,
        db=workspace.session,
        user=workspace.owner,
    )
    await cycles_router.apply_cycle_behavior_policy(
        workspace.cycle.id,
        CyclePolicyApplyRequest(
            proposal=first_body.proposal,
            expected_version=first_preview["expected_version"],
            preview_digest=first_preview["preview_digest"],
            rationale="Apply the first editor draft.",
        ),
        db=workspace.session,
        user=workspace.owner,
    )

    with pytest.raises(HTTPException) as stale_version:
        await cycles_router.apply_cycle_behavior_policy(
            workspace.cycle.id,
            CyclePolicyApplyRequest(
                proposal=stale_body.proposal,
                expected_version=stale_preview["expected_version"],
                preview_digest=stale_preview["preview_digest"],
                rationale="Try the second editor draft.",
            ),
            db=workspace.session,
            user=workspace.owner,
        )

    assert stale_version.value.status_code == 409
    assert set(stale_version.value.detail) == {
        "reason",
        "latest_effective_policy",
    }
    assert stale_version.value.detail["reason"] == "stale_version"
    latest = stale_version.value.detail["latest_effective_policy"]
    EffectiveCyclePolicyRead.model_validate(latest)
    assert latest["version"] == 1
    assert latest["configuration"]["prompt"] == "First editor won."

    fresh_preview = await cycles_router.preview_cycle_behavior_policy(
        workspace.cycle.id,
        stale_body,
        db=workspace.session,
        user=workspace.owner,
    )
    with pytest.raises(HTTPException) as stale_digest:
        await cycles_router.apply_cycle_behavior_policy(
            workspace.cycle.id,
            CyclePolicyApplyRequest(
                proposal=stale_body.proposal,
                expected_version=fresh_preview["expected_version"],
                preview_digest="0" * 64,
                rationale="Use only the reviewed digest.",
            ),
            db=workspace.session,
            user=workspace.owner,
        )

    assert stale_digest.value.status_code == 409
    assert stale_digest.value.detail["reason"] == "stale_preview_digest"
    assert stale_digest.value.detail["latest_effective_policy"]["version"] == 1


async def test_history_is_paginated_in_descending_effective_version(api_workspace):
    workspace = api_workspace
    for index in range(1, 4):
        await _preview_and_apply(
            workspace,
            prompt=f"Mission version {index}.",
            rationale=f"Apply version {index}.",
        )

    first_page = await cycles_router.list_cycle_behavior_policy_history(
        workspace.cycle.id,
        limit=2,
        offset=0,
        db=workspace.session,
        user=workspace.owner,
    )
    second_page = await cycles_router.list_cycle_behavior_policy_history(
        workspace.cycle.id,
        limit=2,
        offset=2,
        db=workspace.session,
        user=workspace.owner,
    )

    CyclePolicyHistoryRead.model_validate(first_page)
    CyclePolicyHistoryRead.model_validate(second_page)
    assert [item["version"] for item in first_page["items"]] == [3, 2]
    assert first_page["pagination"] == {
        "limit": 2,
        "offset": 0,
        "has_more": True,
        "next_offset": 2,
    }
    assert [item["version"] for item in second_page["items"]] == [1]
    assert second_page["pagination"]["has_more"] is False
    assert second_page["pagination"]["next_offset"] is None
    assert all(
        item["applied_at"].utcoffset() == timedelta(0)
        for item in first_page["items"] + second_page["items"]
    )


async def test_revert_previews_and_applies_as_a_new_version(api_workspace):
    workspace = api_workspace
    changed = await _preview_and_apply(
        workspace,
        prompt="Changed mission.",
        rationale="Make the first change.",
    )
    change_id = changed["change"]["id"]

    preview = await cycles_router.preview_cycle_behavior_policy_revert(
        workspace.cycle.id,
        change_id,
        db=workspace.session,
        user=workspace.owner,
    )

    CyclePolicyPreviewRead.model_validate(preview)
    assert preview["reverted_from_id"] == change_id
    assert preview["after"]["configuration"]["prompt"] == (
        "Review the workspace."
    )
    reverted = await cycles_router.apply_cycle_behavior_policy_revert(
        workspace.cycle.id,
        change_id,
        CyclePolicyRevertApplyRequest(
            expected_version=preview["expected_version"],
            preview_digest=preview["preview_digest"],
            rationale="Restore the earlier mission.",
        ),
        db=workspace.session,
        user=workspace.owner,
    )

    CyclePolicyApplyRead.model_validate(reverted)
    assert reverted["effective_policy"]["version"] == 2
    assert reverted["effective_policy"]["configuration"]["prompt"] == (
        "Review the workspace."
    )
    assert reverted["change"]["reverted_from_id"] == change_id
    assert reverted["change"]["source_reference"] == (
        f"api:/cycles/{workspace.cycle.id}/behavior-policy"
    )


async def test_cross_workspace_read_and_apply_are_denied(api_workspace):
    workspace = api_workspace

    with pytest.raises(HTTPException) as read_denied:
        await cycles_router.get_cycle_behavior_policy(
            workspace.cycle.id,
            db=workspace.session,
            user=workspace.outsider,
        )
    with pytest.raises(HTTPException) as apply_denied:
        await cycles_router.apply_cycle_behavior_policy(
            workspace.cycle.id,
            CyclePolicyApplyRequest(
                proposal=CyclePolicyProposal(prompt="Cross-workspace change."),
                expected_version=0,
                preview_digest="not-a-valid-preview",
                rationale="This must be denied.",
            ),
            db=workspace.session,
            user=workspace.outsider,
        )

    assert read_denied.value.status_code == 404
    assert read_denied.value.detail == "Cycle not found"
    assert apply_denied.value.status_code == 404
    assert apply_denied.value.detail == "Cycle not found"
    assert workspace.cycle.prompt == "Review the workspace."
    assert await workspace.session.scalar(
        select(func.count(BehaviorChangeAudit.id))
    ) == 0
