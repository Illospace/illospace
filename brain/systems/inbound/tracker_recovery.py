"""Operator recovery of tracker rows from a reviewed GitHub snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.inbound import (
    InboundDomainProjectionKeyRow, InboundDomainProjectionRow,
)
from brain.systems.inbound.admin import require_connection_for_org
from brain.systems.inbound.github_webhook import github_event_to_envelope
from brain.systems.inbound.service import (
    _policy_allows_domain_projection, _validate_schema_config,
    domain_projection_values, match_source_policy, projection_applies,
)
from brain.systems.user_domains.service import (
    AsyncDomainService, DomainError, _is_open_vocabulary_field, _record_natural_key,
    _normalize_repo_identity, _normalize_record_number,
    merge_record_observation, with_record_creation_defaults,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise DomainError("Snapshot timestamps must include a timezone") from exc


def _envelopes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        raise DomainError("Snapshot must be a JSON object")
    captured_at = _timestamp(snapshot.get("captured_at"))
    if captured_at > datetime.now(timezone.utc):
        raise DomainError("Snapshot capture time is in the future")
    items = snapshot.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise DomainError("Snapshot must contain between 1 and 100 explicit items")
    envelopes = []
    for item in items:
        event = item.get("event") if isinstance(item, dict) else None
        if event not in {"issues", "pull_request"}:
            raise DomainError("Recovery accepts issue and pull request snapshots only")
        repo, subject = item.get("repository"), item.get("subject")
        if (
            not isinstance(repo, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not isinstance(subject, dict)
        ):
            raise DomainError("Snapshot item requires repository and subject")
        number = subject.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise DomainError("Snapshot subject requires a positive GitHub number")
        noun = "issues" if event == "issues" else "pull"
        if subject.get("html_url") != f"https://github.com/{repo}/{noun}/{number}":
            raise DomainError("Snapshot subject URL does not match its repository and number")
        if subject.get("state") not in {"open", "closed"} or not subject.get("title"):
            raise DomainError("Snapshot subject requires title and open or closed state")
        author = subject.get("user")
        if not isinstance(author, dict) or not isinstance(author.get("login"), str) or not author["login"].strip():
            raise DomainError("Snapshot subject requires its original GitHub author")
        if event == "issues" and "pull_request" in subject:
            raise DomainError("Fetch pull requests from the GitHub pulls endpoint")
        if event == "pull_request" and not isinstance(subject.get("merged"), bool):
            raise DomainError("Pull request snapshots require an explicit merged boolean")
        if _timestamp(subject.get("updated_at")) > captured_at:
            raise DomainError("Snapshot subject was updated after its capture time")
        key = "issue" if event == "issues" else "pull_request"
        envelopes.append(github_event_to_envelope(event, {
            "repository": {"full_name": repo}, key: subject, "action": "snapshot",
        }))
    return envelopes


async def recover_tracker_snapshot(
    session: AsyncSession,
    *,
    org_id: str,
    projection_id: str,
    snapshot: dict[str, Any],
    apply: bool = False,
    reviewed_plan: dict[str, Any] | None = None,
    deduplicate_only: bool = False,
) -> dict[str, Any]:
    """Plan or atomically apply one explicit projection, without dispatching runs.

    The caller owns the transaction. Applying requires the unchanged dry-run
    plan, except that an already completed item is an idempotent no-op.
    """
    envelopes = _envelopes(snapshot)
    projection = await session.scalar(select(InboundDomainProjectionRow).where(
        InboundDomainProjectionRow.id == projection_id,
        InboundDomainProjectionRow.org_id == org_id,
    ).with_for_update())
    if projection is None or not projection.enabled or projection.upsert_mode != "upsert":
        raise DomainError("Recovery requires an enabled upsert projection in this organization")
    if projection.external_id_field != "external_id":
        raise DomainError("Recovery requires the tracker's external_id identity field")
    connection = await require_connection_for_org(
        session, org_id=org_id, connection_id=projection.connection_id,
    )
    rendered = []
    for envelope in envelopes:
        if not projection_applies(envelope=envelope, projection=projection, clock=lambda: snapshot["captured_at"]):
            raise DomainError("Snapshot does not match the selected projection's condition")
        freshness_fields = set()
        values = domain_projection_values(
            envelope=envelope, projection=projection, clock=lambda: snapshot["captured_at"],
            freshness_fields=freshness_fields,
        )
        rendered.append((envelope, *values, freshness_fields))
    # Normal ingress locks projection keys before records. Keep that order and
    # retain only these keys; re-querying new keys after taking object/record
    # locks could deadlock with an in-flight webhook creating a key.
    projection_keys = (await session.scalars(select(InboundDomainProjectionKeyRow).where(
        InboundDomainProjectionKeyRow.org_id == org_id,
        InboundDomainProjectionKeyRow.projection_id == projection_id,
        InboundDomainProjectionKeyRow.domain_id == projection.domain_id,
        InboundDomainProjectionKeyRow.external_id.in_([item[1] for item in rendered]),
    ).order_by(InboundDomainProjectionKeyRow.external_id).with_for_update())).all()
    service = AsyncDomainService(session)
    domain = await service.get_domain(org_id, projection.domain_id)
    obj = await service.get_object_type(projection.domain_id, projection.object_key, for_update=True)
    fields = await service.list_fields(obj.id)
    config = {
        "connection_id": projection.connection_id, "policy_id": projection.policy_id,
        "domain_id": projection.domain_id, "object_key": projection.object_key,
        "external_id_path": projection.external_id_path, "field_mapping": projection.field_mapping,
        "title_path": projection.title_path, "metadata": projection.metadata_,
        "fields": [service.serialize_field(field) for field in fields],
    }
    # Schema serialization contains datetimes; use its stable public values.
    config["fields"] = [
        {key: value for key, value in field.items() if key not in {"created_at", "updated_at"}}
        for field in config["fields"]
    ]
    for field, serialized in zip(fields, config["fields"]):
        if _is_open_vocabulary_field(field):
            # Writes extend these options; they do not change the schema contract.
            serialized.pop("options", None)
    plan = {
        "org_id": org_id, "projection_id": projection_id,
        "deduplicate_only": deduplicate_only,
        "snapshot_digest": _digest(snapshot), "projection_digest": _digest(config), "items": [],
    }
    prepared = []
    seen = set()
    for envelope, external_id, data, title, freshness_fields in rendered:
        policy = await match_source_policy(
            session, org_id=org_id, connection_id=projection.connection_id,
            kind=envelope["kind"], origin=envelope["origin"],
        )
        if policy is None or str(policy.id) != str(projection.policy_id):
            raise DomainError("Snapshot does not match the selected projection's source policy")
        if not _policy_allows_domain_projection(policy):
            raise DomainError("Source policy does not allow projection writes")
        _validate_schema_config(policy.schema_config or {}, envelope)
        hints = envelope["hints"]
        kind = "issue" if hints["event"] == "issues" else "pr"
        expected_id = f"github:{hints['repo']}:{kind}:{hints['number']}"
        if external_id.lower() != expected_id.lower() or external_id.lower() in seen:
            raise DomainError("Snapshot identities must be unique and match the selected projection")
        data, _warnings = await service.prepare_record_write(fields, data)
        if str(data.get(projection.external_id_field) or "").lower() != external_id.lower():
            raise DomainError("Rendered record identity must match the snapshot projection identity")
        if "repo" in data and _normalize_repo_identity(data["repo"]) != hints["repo"].casefold():
            raise DomainError("Rendered repository must match the snapshot identity")
        for number_key in ("number", "pr_number"):
            if number_key in data and _normalize_record_number(data[number_key]) != str(hints["number"]):
                raise DomainError("Rendered number must match the snapshot identity")
        for url_key in ("url", "pr_url"):
            if url_key in data and str(data[url_key] or "").casefold() != hints["url"].casefold():
                raise DomainError("Rendered URL must match the snapshot identity")
        seen.add(external_id.lower())
        natural_key = _record_natural_key(fields, data)
        records = await service._find_active_records_by_natural_key(
            org_id, projection.domain_id, obj.id, fields, natural_key,
        )
        if apply and records:
            (await session.scalars(select(DomainRecord).where(
                DomainRecord.id.in_([record.id for record in records]),
            ).with_for_update().execution_options(populate_existing=True))).all()
            records = await service._find_active_records_by_natural_key(
                org_id, projection.domain_id, obj.id, fields, natural_key,
            )
        canonical = records[0] if records else None
        if canonical is None and not deduplicate_only:
            data = with_record_creation_defaults(domain, obj, fields, data)
        if deduplicate_only:
            # A duplicate repair observes the canonical row itself. Supplying
            # every schema field also protects its intentionally empty values
            # from being filled from a stale copy.
            data = {field.key: (canonical.data or {}).get(field.key) for field in fields} if canonical else {}
            title = canonical.title if canonical else None
            freshness_fields = set()
        for record in records:
            for key in freshness_fields:
                incoming = data.get(key)
                stored = (record.data or {}).get(key)
                if not stored or not incoming:
                    continue
                try:
                    stored_at, incoming_at = _timestamp(stored), _timestamp(incoming)
                except DomainError:
                    # A condition can inspect a timestamp and return a
                    # non-timestamp value. Only its rendered date is freshness.
                    if any(field.key == key and field.field_type == "datetime" for field in fields):
                        raise
                    continue
                if stored_at > incoming_at:
                    raise DomainError("Snapshot is older than an existing tracker observation")
        # Validate the complete resulting record before any item is written.
        merged = merge_record_observation(records, data)
        normalized = service.validate_record_data(fields, merged) if canonical or not deduplicate_only else {}
        data = {key: normalized[key] for key in data}
        unchanged = (
            len(records) == 1
            and all(canonical.data.get(key) == value for key, value in data.items())
            and (title is None or title == canonical.title)
        )
        if deduplicate_only and canonical is None:
            unchanged = True
        item_plan = {
            "external_id": external_id, "record_versions": {str(row.id): row.version for row in records},
            "operation": "missing" if deduplicate_only and canonical is None else "unchanged" if unchanged else "deduplicate" if deduplicate_only else "upsert",
            "archive_ids": [row.id for row in records[1:]], "fields": sorted(data),
        }
        plan["items"].append(item_plan)
        prepared.append((external_id, data, title, canonical, unchanged, records))
    if not apply:
        return {**plan, "applied": False}
    if not reviewed_plan or any(
        reviewed_plan.get(key) != plan[key] for key in plan if key != "items"
    ):
        raise DomainError("Recovery requires the matching reviewed dry-run plan")
    expected_items = {item["external_id"]: item for item in reviewed_plan.get("items", [])}
    for item, (_, _, _, _, unchanged, _records) in zip(plan["items"], prepared):
        expected = expected_items.get(item["external_id"])
        if expected is None or (not unchanged and expected != item):
            raise DomainError("Tracker changed after planning; capture a fresh snapshot and plan again")
    for item, (external_id, data, title, canonical, unchanged, records) in zip(plan["items"], prepared):
        if deduplicate_only and canonical is None:
            item["record_id"] = None
            continue
        if not unchanged:
            observation = {
                "data": data, "title": title, "actor_id": connection.owner_user_id,
                "actor_kind": "system", "reason": f"github_tracker_recovery:{plan['snapshot_digest']}",
            }
            if records:
                # Merge exactly the reviewed, locked identity set. Re-running
                # natural-key selection with an ID-less canonical payload can
                # select a different external identity with the same repo/number.
                canonical = await service._merge_duplicate_records(
                    org_id, projection.domain_id, records, run_id=None, idea_id=None, **observation,
                )
            else:
                canonical = await service.create_record(org_id, projection.domain_id, projection.object_key, **observation)
        # Repair selected projection keys that referenced an archived duplicate.
        for key in projection_keys:
            if key.external_id == external_id:
                key.record_id = canonical.id
        item["record_id"] = canonical.id
    await session.flush()
    return {**plan, "applied": True}
