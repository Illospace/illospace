"""Add tracker projections beside the existing GitHub feed projection."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import Domain
from brain.platform.db.models.inbound import InboundDomainProjectionRow
from brain.systems.inbound import admin
from brain.systems.user_domains.service import AsyncDomainService, DomainError


def tracker_projection_specs() -> dict[str, dict[str, Any]]:
    """Source fields and conditions for the canonical tracker schema."""
    identity = {"repo": "hints.repo", "number": "hints.number", "url": "hints.url"}
    return {
        "ticket": {
            "external_id_path": "github:{hints.repo}:issue:{hints.number}",
            "title_path": "payload.issue.title",
            "metadata": {"when": {"path": "hints.event", "equals": "issues"}},
            "field_mapping": {
                **identity, "github_state": "hints.issue_outcome",
                "created_at": "payload.issue.created_at", "updated_at": "hints.source_updated_at",
                "closed_at": "hints.closed_at", "synced_at": {"now": True}, "body": "payload.issue.body",
                # Source state is independent of editorial status. Triage and
                # deploy reconciliation own Done and production evidence.
            },
        },
        "pull_request": {
            "external_id_path": "github:{hints.repo}:pr:{hints.number}",
            "title_path": "payload.pull_request.title",
            "metadata": {"when": {"path": "hints.event", "equals": "pull_request"}},
            "field_mapping": {
                **identity, "author": "payload.pull_request.user.login",
                "updated_at": "hints.source_updated_at",
                "state": {
                    "if": {"path": "hints.merged", "equals": True}, "then": "merged",
                    "else": {
                        "if": {"path": "hints.state", "equals": "closed"}, "then": "closed",
                        "else": {
                            "if": {"path": "payload.pull_request.draft", "equals": True},
                            "then": "draft", "else": "open",
                        },
                    },
                },
            },
        },
    }


async def configure_github_tracker(
    session: AsyncSession, *, org_id: str, apply: bool = False,
) -> dict[str, Any]:
    """Resolve current org configuration and add missing projections only."""
    feeds = (await session.scalars(select(InboundDomainProjectionRow).where(
        InboundDomainProjectionRow.org_id == org_id,
        InboundDomainProjectionRow.enabled.is_(True),
        InboundDomainProjectionRow.object_key == "github_event",
        InboundDomainProjectionRow.external_id_path == "hints.node_id",
    ).with_for_update())).all()
    if len(feeds) != 1 or feeds[0].policy_id is None:
        raise DomainError("Expected one enabled GitHub feed projection with a source policy")
    feed = feeds[0]
    await admin.require_connection_for_org(session, org_id=org_id, connection_id=feed.connection_id)
    await admin.require_policy_for_org(session, org_id=org_id, policy_id=feed.policy_id)
    domain = await session.scalar(select(Domain).where(
        Domain.org_id == org_id, Domain.slug == "github-ticket-tracker", Domain.archived_at.is_(None),
    ))
    if domain is None:
        raise DomainError("Canonical GitHub ticket tracker domain was not found")
    service = AsyncDomainService(session)
    pending = []
    for object_key, spec in tracker_projection_specs().items():
        obj = await service.get_object_type(domain.id, object_key)
        fields = await service.list_fields(obj.id)
        valid_keys = {field.key for field in fields}
        for title_key in {"title", obj.title_field} & valid_keys:
            spec["field_mapping"][title_key] = spec["title_path"]
        # State is the original PR contract. Some user schemas also expose
        # the source boolean; retain it only where that field is declared.
        if object_key == "pull_request" and "merged" in valid_keys:
            spec["field_mapping"]["merged"] = "hints.merged"
        if not {"external_id", *spec["field_mapping"]}.issubset(valid_keys):
            raise DomainError(f"Canonical tracker schema is missing fields for {object_key}")
        existing = (await session.scalars(select(InboundDomainProjectionRow).where(
            InboundDomainProjectionRow.org_id == org_id,
            InboundDomainProjectionRow.domain_id == domain.id,
            InboundDomainProjectionRow.object_key == object_key,
        ).with_for_update())).all()
        if len(existing) > 1:
            raise DomainError(f"Multiple tracker projections exist for {object_key}; review configuration")
        row = existing[0] if existing else None
        if row is not None and (
            not row.enabled or row.connection_id != feed.connection_id or row.policy_id != feed.policy_id
            or row.external_id_field != "external_id" or row.upsert_mode != "upsert"
            or any(getattr(row, "metadata_" if key == "metadata" else key) != value for key, value in spec.items())
        ):
            raise DomainError(f"Existing {object_key} projection differs; review it instead of replacing it")
        pending.append((object_key, spec, row))
    output = []
    for object_key, spec, row in pending:
        operation = "existing" if row else "create"
        if row is None and apply:
            row = await admin.create_projection(
                session, org_id=org_id, connection_id=feed.connection_id, policy_id=feed.policy_id,
                domain_id=domain.id, object_key=object_key, external_id_field="external_id",
                upsert_mode="upsert", **spec,
            )
        output.append({"object_key": object_key, "projection_id": row.id if row else None, "operation": operation})
    return {"applied": apply, "tracker_domain_id": domain.id, "preserved_feed_projection_id": feed.id, "projections": output}
