"""The declare completion boundary owns the Domain-1 chantier guarantee."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus
from brain.systems.slack.chantier_reconciliation import (
    ChantierDeclareGuaranteeError,
    PublishedChantierPrd,
    reconcile_published_chantier_prd,
)
from brain.systems.user_domains.service import AsyncDomainService


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
PRD_URL = "https://illo.example/doc?src=/static/uploads/connector-framework-prd.md"
SLACK_REF = "slack:C_DECLARE:1752800000.000100"


def _patch_sqlite_for_pg_types() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_chantier_guarantee_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._chantier_guarantee_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRelationType.__table__,
            DomainRecord.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
        ]
    )


def _chantier_object_definition() -> dict:
    github_issue_pattern = (
        r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:issue:[1-9][0-9]*"
    )
    return {
        "key": "chantier",
        "name": "Chantier",
        "title_field": "title",
        "fields": [
            {
                "key": "slug",
                "field_type": "text",
                "required": True,
                "validation": {
                    "immutable": True,
                    "max_length": 80,
                    "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
                },
            },
            {"key": "title", "field_type": "text", "required": True},
            {
                "key": "goal",
                "field_type": "long_text",
                "required": True,
                "validation": {"pattern": r"(?is)^done means\s+\S.*$"},
            },
            {
                "key": "kind",
                "field_type": "enum",
                "required": True,
                "options": ["feature", "incident", "quality", "gtm", "sunset"],
            },
            {
                "key": "state",
                "field_type": "enum",
                "required": True,
                "options": [
                    "exploring",
                    "building",
                    "shipping",
                    "verifying",
                    "done",
                    "paused",
                ],
            },
            {"key": "owner", "field_type": "text"},
            {
                "key": "refs",
                "field_type": "json",
                "required": True,
                "validation": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["source", "ref"],
                        "additional_properties": False,
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": ["github", "doc", "slack", "posthog", "url"],
                            },
                            "ref": {"type": "string", "min_length": 1},
                            "title": {"type": "string", "min_length": 1},
                        },
                        "source_ref_patterns": {"github": github_issue_pattern},
                    },
                },
            },
            {"key": "parent_issue", "field_type": "text"},
            {
                "key": "next_step",
                "field_type": "text",
                "required": True,
                "validation": {"pattern": r"^[^\r\n]+$"},
            },
            {"key": "progress_note", "field_type": "long_text"},
        ],
    }


async def _create_tracker(session):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )


def _publication() -> PublishedChantierPrd:
    return PublishedChantierPrd(
        slug="connector-framework",
        title="Connector Framework",
        goal="Done means connectors share one verified declaration contract.",
        kind="sunset",
        next_step="Implement the first connector against the shared contract.",
        prd_ref={"source": "url", "ref": PRD_URL, "title": "Connector Framework PRD"},
        anchor_ref={"source": "slack", "ref": SLACK_REF, "title": "Slack declaration anchor"},
    )


def _record_data() -> dict:
    publication = _publication()
    return {
        "slug": publication.slug,
        "title": publication.title,
        "goal": publication.goal,
        "kind": publication.kind,
        "state": "exploring",
        "refs": publication.linked_refs(),
        "next_step": publication.next_step,
    }


async def _create_chantier_record(session, domain_id, *, slug: str, title: str, refs):
    data = _record_data()
    data.update({"slug": slug, "title": title, "refs": refs})
    return await AsyncDomainService(session).create_record(
        ORG_ID,
        domain_id,
        "chantier",
        data=data,
    )


def _mismatched_publication() -> PublishedChantierPrd:
    publication = _publication()
    return PublishedChantierPrd(
        slug="v3-canonical-agent-mcp-repositioning-automation-builder-chat",
        title="V3 Canonical Agent MCP Repositioning Automation Builder Chat",
        prd_ref=publication.prd_ref,
        anchor_ref=publication.anchor_ref,
    )


async def _running_engine(session, *, message: str):
    engine = AsyncAgentRunEngine(session, recipes={})
    run = await engine.store.create_run(
        AgentRunRequest(
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id="thread-371",
            message=message,
        )
    )
    await engine.store.set_status(run.id, RunStatus.STARTING)
    await engine.store.set_status(run.id, RunStatus.RUNNING)
    return engine, await engine.store.require_run(run.id)


async def _append_prd_and_anchor_events(
    engine,
    run_id: int,
    *,
    declaration: bool = True,
    truncated_results: bool = False,
) -> None:
    prd_result = json.dumps(
        {"ok": True, "viewer_url": PRD_URL, "instruction": "x" * 2_000}
    )
    slack_result = json.dumps(
        {
            "ok": True,
            "channel_id": "C_DECLARE",
            "thread_ts": "1752800000.000100",
            "slack": {
                "channel": "C_DECLARE",
                "ts": "1752800000.000200",
                "message": {"text": "x" * 2_000},
            },
        }
    )
    if truncated_results:
        prd_result = prd_result[:1000]
        slack_result = slack_result[:1000]
    await engine.store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "publish_thread_asset",
                "args": {
                    "file_path": "/tmp/connector-framework-prd.md",
                    "title": "Connector Framework PRD",
                },
                "result": prd_result,
            },
            root_run_id=run_id,
        )
    )
    await engine.store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "post_slack_reply",
                "args": {
                    "body": (
                        "Declared the connector framework chantier. Slug: connector-framework\n"
                        "Done means connectors share one verified declaration contract.\n"
                        "kind: sunset\n"
                        "next_step: Implement the first connector against the shared contract."
                    )
                    if declaration
                    else "The connector framework planning document is ready for review."
                },
                "result": slack_result,
            },
            root_run_id=run_id,
        )
    )


async def test_declare_with_linked_tracker_record_completes_verified(session):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    existing = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=_record_data(),
    )
    engine, run = await _running_engine(
        session,
        message="Declare the connector-framework chantier and publish its PRD.",
    )
    await _append_prd_and_anchor_events(engine, run.id)

    completed = await engine.complete(run.id, output="Connector framework declared.")

    assert completed.status == RunStatus.COMPLETED
    stored = await engine.store.require_run(run.id)
    guarantee = stored.metadata_["chantier_declare_guarantee"]
    assert guarantee == {
        "status": "verified",
        "slug": "connector-framework",
        "domain_id": domain.id,
        "record_id": existing.id,
        "record_ref": f"domain_record:{existing.id}",
        "operation": "verified",
        "drift": [],
        "self_healed": False,
        "source": "published_prd",
    }
    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert [record.id for record in records] == [existing.id]


async def test_declare_missing_tracker_record_self_heals_before_success_and_digest_sees_it(session):
    domain = await _create_tracker(session)
    engine, run = await _running_engine(
        session,
        message="Declare the connector-framework chantier and publish its PRD.",
    )
    await _append_prd_and_anchor_events(engine, run.id, truncated_results=True)

    completed = await engine.complete(run.id, output="Connector framework declared.")

    assert completed.status == RunStatus.COMPLETED
    guarantee = (await engine.store.require_run(run.id)).metadata_[
        "chantier_declare_guarantee"
    ]
    assert guarantee["status"] == "repaired"
    assert guarantee["operation"] == "created_missing_record"
    assert guarantee["drift"] == ["missing_record"]
    assert guarantee["self_healed"] is True

    # This is the same Domain/object/slug query used by chantier-primary sweeps.
    records = await AsyncDomainService(session).list_records(
        ORG_ID,
        domain.id,
        object_key="chantier",
        filters={"slug": "connector-framework"},
    )
    assert len(records) == 1
    assert records[0].data["slug"] == "connector-framework"
    assert records[0].data["kind"] == "sunset"
    refs = {(item["source"], item["ref"]) for item in records[0].data["refs"]}
    assert ("url", PRD_URL) in refs
    assert ("slack", SLACK_REF) in refs


async def test_declare_fails_loudly_when_exact_ref_overlap_is_ambiguous(session):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    candidates = []
    for slug, title, matching_ref in (
        ("connector-framework", "Connector Framework", _publication().prd_ref),
        (
            "automation-builder-chat",
            "Automation Builder Chat",
            _publication().anchor_ref,
        ),
    ):
        candidates.append(
            await _create_chantier_record(
                session,
                domain.id,
                slug=slug,
                title=title,
                refs=[matching_ref],
            )
        )
    engine, run = await _running_engine(
        session,
        message="Declare the connector-framework chantier and publish its PRD.",
    )
    await _append_prd_and_anchor_events(engine, run.id)

    failed = await engine.complete(run.id, output="Connector framework declared.")

    assert failed.status == RunStatus.FAILED
    guarantee = (await engine.store.require_run(run.id)).metadata_[
        "chantier_declare_guarantee"
    ]
    assert guarantee["status"] == "failed"
    assert "multiple active chantier records" in guarantee["error"]
    assert all(str(candidate.id) in guarantee["error"] for candidate in candidates)
    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert {record.id for record in records} == {candidate.id for candidate in candidates}


async def test_reconciliation_fails_when_slug_and_exact_refs_resolve_to_different_records(
    session,
):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    records = []
    for slug, title, refs in (
        (
            "connector-framework",
            "Connector Framework",
            [
                {
                    "source": "github",
                    "ref": "github:Illospace/illospace:issue:371",
                    "title": "Declare guarantee issue",
                }
            ],
        ),
        (
            "agent-mcp-repositioning",
            "Agent MCP Repositioning",
            [_publication().prd_ref],
        ),
    ):
        records.append(
            await _create_chantier_record(
                session,
                domain.id,
                slug=slug,
                title=title,
                refs=refs,
            )
        )

    with pytest.raises(ChantierDeclareGuaranteeError) as raised:
        await reconcile_published_chantier_prd(
            session,
            org_id=ORG_ID,
            publication=_publication(),
            repair=True,
        )

    assert "resolve to different active chantier records" in str(raised.value)
    assert all(str(record.id) in str(raised.value) for record in records)
    remaining = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert {record.id for record in remaining} == {record.id for record in records}


async def test_reconciliation_reports_published_prd_without_record_ref(session):
    domain = await _create_tracker(session)

    report = await reconcile_published_chantier_prd(
        session,
        org_id=ORG_ID,
        publication=_publication(),
        repair=False,
    )

    assert report.domain_id == domain.id
    assert report.record_id is None
    assert report.operation == "missing_record"
    assert report.drift == ("missing_record",)
    assert report.repaired is False


async def test_reconciliation_reuses_active_chantier_when_publication_refs_are_subset(session):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    canonical = await _create_chantier_record(
        session,
        domain.id,
        slug="agent-mcp-repositioning",
        title="Agent MCP Repositioning",
        refs=[
            *_publication().linked_refs(),
            {
                "source": "github",
                "ref": "github:Illospace/illospace:issue:376",
                "title": "Canonical chantier issue",
            },
        ],
    )
    canonical_version = canonical.version

    report = await reconcile_published_chantier_prd(
        session,
        org_id=ORG_ID,
        publication=_mismatched_publication(),
        repair=True,
    )

    assert report.record_id == canonical.id
    assert report.operation == "verified"
    assert report.repaired is False
    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert [record.id for record in records] == [canonical.id]
    assert records[0].data["slug"] == "agent-mcp-repositioning"
    assert records[0].version == canonical_version


async def test_reconciliation_ref_overlap_reuses_canonical_and_merges_missing_refs(
    session,
):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    canonical = await _create_chantier_record(
        session,
        domain.id,
        slug="agent-mcp-repositioning",
        title="Agent MCP Repositioning",
        refs=[
            _publication().prd_ref,
            {
                "source": "github",
                "ref": "github:Illospace/illospace:issue:376",
                "title": "Canonical chantier issue",
            },
        ],
    )

    report = await reconcile_published_chantier_prd(
        session,
        org_id=ORG_ID,
        publication=_mismatched_publication(),
        repair=True,
    )

    assert report.record_id == canonical.id
    assert report.operation == "linked_missing_refs"
    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert [record.id for record in records] == [canonical.id]
    assert records[0].data["slug"] == "agent-mcp-repositioning"
    refs = {(item["source"], item["ref"]) for item in records[0].data["refs"]}
    assert refs == {
        ("url", PRD_URL),
        ("slack", SLACK_REF),
        ("github", "github:Illospace/illospace:issue:376"),
    }


async def test_reconciliation_does_not_match_by_title_or_untyped_ref_value(session):
    domain = await _create_tracker(session)
    service = AsyncDomainService(session)
    canonical = await _create_chantier_record(
        session,
        domain.id,
        slug="agent-mcp-repositioning",
        title=_publication().title,
        refs=[
            {
                "source": "doc",
                "ref": PRD_URL,
                "title": "Different typed identity",
            }
        ],
    )

    report = await reconcile_published_chantier_prd(
        session,
        org_id=ORG_ID,
        publication=_publication(),
        repair=True,
    )

    assert report.record_id != canonical.id
    assert report.operation == "created_missing_record"
    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert {record.data["slug"] for record in records} == {
        "agent-mcp-repositioning",
        "connector-framework",
    }


async def test_non_declare_run_with_prd_and_slack_post_is_unaffected(session):
    domain = await _create_tracker(session)
    engine, run = await _running_engine(
        session,
        message=(
            "Summarize the connector-framework chantier in a planning document "
            "and announce it to Slack."
        ),
    )
    await _append_prd_and_anchor_events(engine, run.id, declaration=False)

    completed = await engine.complete(run.id, output="Chantier planning document published.")

    assert completed.status == RunStatus.COMPLETED
    stored = await engine.store.require_run(run.id)
    assert "chantier_declare_guarantee" not in stored.metadata_
    assert await AsyncDomainService(session).list_records(
        ORG_ID,
        domain.id,
        object_key="chantier",
    ) == []


async def test_declare_fails_loudly_when_tracker_domain_cannot_be_repaired(session):
    engine, run = await _running_engine(
        session,
        message="Declare the connector-framework chantier and publish its PRD.",
    )
    await _append_prd_and_anchor_events(engine, run.id)

    failed = await engine.complete(run.id, output="Connector framework declared.")

    assert failed.status == RunStatus.FAILED
    guarantee = (await engine.store.require_run(run.id)).metadata_[
        "chantier_declare_guarantee"
    ]
    assert guarantee["status"] == "failed"
    assert "github-ticket-tracker" in guarantee["error"]
