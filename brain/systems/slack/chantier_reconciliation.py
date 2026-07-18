"""Completion-time guarantee and drift checks for chantier declarations.

The explicit Slack declaration door persists its Domain record before the
agent run starts.  A conversational run can also become declaration-shaped by
publishing a chantier PRD and announcing it in Slack.  This module recognizes
that second shape from durable tool events and makes the same Domain record a
verified precondition of successful completion.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePath
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.models.domain import DomainRecord
from brain.systems.slack.chantier_declare import (
    CHANTIER_OBJECT_KEY,
    MISSING_NEXT_STEP,
    TRACKER_DOMAIN_SLUG,
    _guess_kind,
    _merge_refs,
    _slugify,
)
from brain.systems.user_domains.service import AsyncDomainService


_TOOL_COMPLETED_EVENT = "run.tool_completed"
_PRD_TOOL = "publish_thread_asset"
_SLACK_ANCHOR_TOOL = "post_slack_reply"
_DECLARE_ACTION_RE = re.compile(
    r"\b(?:declare|declared|declaring|declaration)\b",
    re.IGNORECASE,
)
_CHANTIER_RE = re.compile(r"\bchantier\b", re.IGNORECASE)
_PRD_RE = re.compile(r"\b(?:prd|product requirements? document)\b", re.IGNORECASE)
_SLUG_LABEL_RE = re.compile(r"\bslug\s*[:=]\s*`?([a-z0-9]+(?:-[a-z0-9]+)*)", re.IGNORECASE)
_DONE_MEANS_RE = re.compile(r"\bdone\s+means\s*:?[ \t]*(.+?)(?:\r?\n|$)", re.IGNORECASE)
_KIND_RE = re.compile(r"\bkind\s*:\s*(feature|incident|quality|gtm)\b", re.IGNORECASE)
_NEXT_STEP_RE = re.compile(r"\bnext[_ ]step\s*:\s*(.+?)(?:\r?\n|$)", re.IGNORECASE)
_PREVIEW_JSON_SCALAR_RE = re.compile(
    r'"(?P<key>ok|error|viewer_url|public_url|url|channel_id|thread_ts|channel|ts)"'
    r'\s*:\s*(?P<value>"(?:\\.|[^"\\])*"|true|false|null)'
)


class ChantierDeclareGuaranteeError(RuntimeError):
    """A declare-shaped run cannot satisfy its tracker-record precondition."""


@dataclass(frozen=True)
class PublishedChantierPrd:
    """The deterministic identity and links of one published chantier PRD."""

    slug: str
    title: str
    prd_ref: Mapping[str, str] | None = None
    anchor_ref: Mapping[str, str] | None = None
    goal: str | None = None
    kind: str | None = None
    state: str = "exploring"
    next_step: str | None = None
    additional_refs: tuple[Mapping[str, str], ...] = ()
    record_data: Mapping[str, Any] | None = None
    source: str = "published_prd"

    def linked_refs(self) -> list[dict[str, str]]:
        candidates: list[Mapping[str, str]] = list(self.additional_refs)
        if self.prd_ref is not None:
            candidates.append(self.prd_ref)
        if self.anchor_ref is not None:
            candidates.append(self.anchor_ref)
        return _merge_refs([], candidates)


@dataclass(frozen=True)
class ChantierReconciliationReport:
    """Result of checking one published PRD against the Domain tracker."""

    slug: str
    domain_id: int | None
    record_id: int | None
    operation: str
    drift: tuple[str, ...] = ()
    repaired: bool = False
    source: str = "published_prd"

    @property
    def record_ref(self) -> str | None:
        return f"domain_record:{self.record_id}" if self.record_id is not None else None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "status": "repaired" if self.repaired else ("drift" if self.drift else "verified"),
            "slug": self.slug,
            "domain_id": self.domain_id,
            "record_id": self.record_id,
            "record_ref": self.record_ref,
            "operation": self.operation,
            "drift": list(self.drift),
            "self_healed": self.repaired,
            "source": self.source,
        }


async def reconcile_published_chantier_prd(
    session: AsyncSession,
    *,
    org_id: str,
    publication: PublishedChantierPrd,
    repair: bool = False,
    actor_user_id: str | None = None,
    run_id: int | None = None,
) -> ChantierReconciliationReport:
    """Report or repair a published PRD whose exact-slug record has drifted."""

    service = AsyncDomainService(session)
    domain = next(
        (
            item
            for item in await service.list_domains(org_id)
            if item.slug == TRACKER_DOMAIN_SLUG
        ),
        None,
    )
    if domain is None:
        report = ChantierReconciliationReport(
            slug=publication.slug,
            domain_id=None,
            record_id=None,
            operation="missing_domain",
            drift=("missing_domain",),
            source=publication.source,
        )
        if repair:
            raise ChantierDeclareGuaranteeError(
                f"Domain '{TRACKER_DOMAIN_SLUG}' is missing for chantier {publication.slug!r}"
            )
        return report

    await service.get_object_type(domain.id, CHANTIER_OBJECT_KEY, for_update=repair)
    records = await service.list_records(
        org_id,
        domain.id,
        object_key=CHANTIER_OBJECT_KEY,
        limit=500,
        order="updated_asc",
    )
    matches = _records_with_slug(records, publication.slug)
    if len(matches) > 1:
        ids = ", ".join(str(record.id) for record in matches)
        if repair:
            raise ChantierDeclareGuaranteeError(
                f"Chantier slug {publication.slug!r} resolves to duplicate Domain records: {ids}"
            )
        return ChantierReconciliationReport(
            slug=publication.slug,
            domain_id=domain.id,
            record_id=None,
            operation="duplicate_records",
            drift=("duplicate_records",),
            source=publication.source,
        )

    expected_refs = publication.linked_refs()
    record = matches[0] if matches else None
    drift: list[str] = []
    if record is None:
        drift.append("missing_record")
    elif _missing_refs(record, expected_refs):
        drift.append("missing_refs")

    if not repair or not drift:
        return ChantierReconciliationReport(
            slug=publication.slug,
            domain_id=domain.id,
            record_id=record.id if record is not None else None,
            operation="verified" if not drift else drift[0],
            drift=tuple(drift),
            source=publication.source,
        )

    if record is None:
        data = _new_record_data(publication)
        record = await service.create_record(
            org_id,
            domain.id,
            CHANTIER_OBJECT_KEY,
            data=data,
            actor_id=actor_user_id,
            actor_kind="agent",
            run_id=run_id,
            reason="Declare completion guarantee self-heal",
        )
        operation = "created_missing_record"
    else:
        merged_refs = _merge_refs((record.data or {}).get("refs"), expected_refs)
        record = await service.update_record(
            org_id,
            domain.id,
            record.id,
            data_patch={"refs": merged_refs},
            expected_version=record.version,
            actor_id=actor_user_id,
            actor_kind="agent",
            run_id=run_id,
            reason="Declare completion guarantee linked published PRD/Slack anchor",
        )
        operation = "linked_missing_refs"

    verified_records = await service.list_records(
        org_id,
        domain.id,
        object_key=CHANTIER_OBJECT_KEY,
        limit=500,
        order="updated_asc",
    )
    verified_matches = _records_with_slug(verified_records, publication.slug)
    if len(verified_matches) != 1 or _missing_refs(verified_matches[0], expected_refs):
        raise ChantierDeclareGuaranteeError(
            f"Chantier {publication.slug!r} tracker record failed post-repair verification"
        )
    verified = verified_matches[0]
    return ChantierReconciliationReport(
        slug=publication.slug,
        domain_id=domain.id,
        record_id=verified.id,
        operation=operation,
        drift=tuple(drift),
        repaired=True,
        source=publication.source,
    )


async def guarantee_chantier_record_for_run(
    session: AsyncSession,
    *,
    run: Any,
    output: str = "",
) -> ChantierReconciliationReport | None:
    """Verify/self-heal only runs that carry durable declare-shaped evidence."""

    metadata = dict(getattr(run, "metadata_", None) or {})
    explicit = metadata.get("chantier_declare")
    if isinstance(explicit, Mapping):
        operation = str(explicit.get("operation") or "").strip().lower()
        if operation == "failed":
            error = str(explicit.get("error") or "tracker persistence failed").strip()
            raise ChantierDeclareGuaranteeError(error)
        publication = _publication_from_explicit_metadata(explicit)
        if publication is None:
            raise ChantierDeclareGuaranteeError(
                "Explicit chantier declaration metadata has no stable slug"
            )
    else:
        publication = await _publication_from_run_events(session, run=run, output=output)
        if publication is None:
            return None

    try:
        return await reconcile_published_chantier_prd(
            session,
            org_id=str(getattr(run, "org_id", "") or ""),
            publication=publication,
            repair=True,
            actor_user_id=str(getattr(run, "user_id", "") or "") or None,
            run_id=int(getattr(run, "id")),
        )
    except ChantierDeclareGuaranteeError:
        raise
    except Exception as exc:
        raise ChantierDeclareGuaranteeError(
            f"Chantier {publication.slug!r} tracker repair failed: {exc}"
        ) from exc


def _publication_from_explicit_metadata(
    metadata: Mapping[str, Any],
) -> PublishedChantierPrd | None:
    data = metadata.get("data")
    data = dict(data) if isinstance(data, Mapping) else {}
    slug = _slugify(str(data.get("slug") or ""))
    if not slug:
        return None
    title = str(data.get("title") or slug.replace("-", " ")).strip()
    refs = tuple(item for item in data.get("refs", []) if isinstance(item, Mapping))
    return PublishedChantierPrd(
        slug=slug,
        title=title,
        goal=str(data.get("goal") or "").strip() or None,
        kind=str(data.get("kind") or "").strip() or None,
        state=str(data.get("state") or "exploring").strip() or "exploring",
        next_step=str(data.get("next_step") or "").strip() or None,
        additional_refs=refs,
        record_data=data,
        source="explicit_declare",
    )


async def _publication_from_run_events(
    session: AsyncSession,
    *,
    run: Any,
    output: str,
) -> PublishedChantierPrd | None:
    events = list(
        await session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(getattr(run, "id")),
                AgentRunEventRow.event_type == _TOOL_COMPLETED_EVENT,
            )
            .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
        )
    )
    prd_events = [event for event in events if _is_successful_tool_event(event, _PRD_TOOL)]
    anchor_events = [
        event for event in events if _is_successful_tool_event(event, _SLACK_ANCHOR_TOOL)
    ]
    if not prd_events or not anchor_events:
        return None

    prd_event = next(
        (
            event
            for event in reversed(prd_events)
            if _PRD_RE.search(_asset_identity_text(event))
        ),
        None,
    )
    if prd_event is None:
        return None
    anchor_event = anchor_events[-1]

    input_message = str(getattr(run, "input_message", "") or "")
    anchor_body = str(_event_args(anchor_event).get("body") or "")
    asset_identity = _asset_identity_text(prd_event)
    identity_text = "\n".join((input_message, asset_identity, anchor_body, output))
    if (
        _DECLARE_ACTION_RE.search(identity_text) is None
        and _CHANTIER_RE.search(asset_identity) is None
    ):
        return None

    raw_title = _asset_title(prd_event)
    title = _clean_prd_title(raw_title)
    explicit_slug = _SLUG_LABEL_RE.search(identity_text)
    slug = _slugify(explicit_slug.group(1) if explicit_slug else title)
    if not slug or not title:
        raise ChantierDeclareGuaranteeError(
            "Published chantier PRD and Slack anchor do not expose a stable chantier identity"
        )

    goal_match = _DONE_MEANS_RE.search(identity_text)
    goal = f"Done means {goal_match.group(1).strip()}" if goal_match else None
    kind_match = _KIND_RE.search(identity_text)
    next_step_match = _NEXT_STEP_RE.search(identity_text)
    return PublishedChantierPrd(
        slug=slug,
        title=title,
        prd_ref=_prd_ref(prd_event, title=raw_title or f"{title} PRD"),
        anchor_ref=_slack_anchor_ref(anchor_event),
        goal=goal,
        kind=kind_match.group(1).lower() if kind_match else _guess_kind(f"{title} {goal or ''}"),
        next_step=next_step_match.group(1).strip() if next_step_match else None,
    )


def _is_successful_tool_event(event: AgentRunEventRow, tool_name: str) -> bool:
    payload = dict(event.payload or {})
    if str(payload.get("tool_name") or payload.get("tool") or "") != tool_name:
        return False
    result = _event_result(event)
    if not isinstance(result, Mapping):
        return False
    return not result.get("error") and result.get("ok") is not False


def _event_args(event: AgentRunEventRow) -> dict[str, Any]:
    args = dict((event.payload or {}).get("args") or {})
    return {str(key): value for key, value in args.items()}


def _event_result(event: AgentRunEventRow) -> Any:
    value = (event.payload or {}).get("result")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Tool events intentionally retain only a bounded result preview.  The
        # publisher and Slack handlers put identity fields first, so recover
        # those complete scalars even when a later attachment/message body is
        # cut before the JSON object's closing brace.
        recovered: dict[str, Any] = {}
        for match in _PREVIEW_JSON_SCALAR_RE.finditer(value):
            key = match.group("key")
            if key in recovered:
                continue
            try:
                recovered[key] = json.loads(match.group("value"))
            except json.JSONDecodeError:
                continue
        return recovered or None


def _asset_identity_text(event: AgentRunEventRow) -> str:
    args = _event_args(event)
    return " ".join((str(args.get("title") or ""), str(args.get("file_path") or "")))


def _asset_title(event: AgentRunEventRow) -> str:
    args = _event_args(event)
    title = str(args.get("title") or "").strip()
    if title:
        return title
    path = str(args.get("file_path") or "").strip().replace("\\", "/")
    return PurePath(path).stem if path else ""


def _clean_prd_title(value: str) -> str:
    title = PurePath(str(value or "").replace("\\", "/")).stem
    title = re.sub(r"[\[\](){}]", " ", title)
    title = _PRD_RE.sub(" ", title)
    title = re.sub(r"\bchantier\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"[_–—]+", " ", title)
    title = re.sub(r"\s*-\s*", " ", title)
    return re.sub(r"\s+", " ", title).strip(" .:-")[:500]


def _prd_ref(event: AgentRunEventRow, *, title: str) -> dict[str, str]:
    result = _event_result(event)
    result = dict(result) if isinstance(result, Mapping) else {}
    ref = str(
        result.get("viewer_url")
        or result.get("public_url")
        or result.get("url")
        or _event_args(event).get("file_path")
        or ""
    ).strip()
    if not ref:
        raise ChantierDeclareGuaranteeError("Published chantier PRD has no durable reference")
    return {
        "source": "url" if ref.startswith(("http://", "https://", "/")) else "doc",
        "ref": ref,
        "title": title[:500],
    }


def _slack_anchor_ref(event: AgentRunEventRow) -> dict[str, str]:
    args = _event_args(event)
    result = _event_result(event)
    result = dict(result) if isinstance(result, Mapping) else {}
    slack = result.get("slack")
    slack = dict(slack) if isinstance(slack, Mapping) else {}
    channel = str(
        result.get("channel_id")
        or result.get("channel")
        or slack.get("channel")
        or args.get("channel_id")
        or ""
    ).strip()
    anchor = str(
        result.get("thread_ts")
        or result.get("ts")
        or slack.get("thread_ts")
        or slack.get("ts")
        or args.get("thread_ts")
        or ""
    ).strip()
    if not channel or not anchor:
        raise ChantierDeclareGuaranteeError(
            "Published chantier PRD Slack announcement has no durable channel/thread anchor"
        )
    return {
        "source": "slack",
        "ref": f"slack:{channel}:{anchor}",
        "title": "Slack declaration anchor",
    }


def _new_record_data(publication: PublishedChantierPrd) -> dict[str, Any]:
    data = dict(publication.record_data or {})
    data["slug"] = publication.slug
    data.setdefault("title", publication.title)
    data.setdefault("goal", publication.goal or f"Done means {publication.title} reaches its stated outcome.")
    data.setdefault("kind", publication.kind or _guess_kind(publication.title))
    data.setdefault("state", publication.state or "exploring")
    data["refs"] = _merge_refs(data.get("refs"), publication.linked_refs())
    data.setdefault("next_step", publication.next_step or MISSING_NEXT_STEP)
    return data


def _records_with_slug(records: Sequence[DomainRecord], slug: str) -> list[DomainRecord]:
    needle = slug.casefold()
    return [
        record
        for record in records
        if str((record.data or {}).get("slug") or "").casefold() == needle
    ]


def _missing_refs(record: DomainRecord, expected: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    existing = {
        (str(item.get("source") or ""), str(item.get("ref") or ""))
        for item in (record.data or {}).get("refs", [])
        if isinstance(item, Mapping)
    }
    return [
        dict(item)
        for item in expected
        if (str(item.get("source") or ""), str(item.get("ref") or "")) not in existing
    ]


__all__ = [
    "ChantierDeclareGuaranteeError",
    "ChantierReconciliationReport",
    "PublishedChantierPrd",
    "guarantee_chantier_record_for_run",
    "reconcile_published_chantier_prd",
]
