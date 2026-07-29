"""Restart-safe headless distillation for conversational knowledge drafts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.org import User
from brain.systems.knowledge.connectors.base import KnowledgeDraft
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work


DISTILLATION_CURSOR_KEY = "_distillation_pending"
DISTILLATION_MAX_ATTEMPTS = 2
DISTILLATION_MANIFEST_VERSION = 1
_TERMINAL_FAILURE_STATUSES = {
    "blocked",
    "canceled",
    "cancelled",
    "error",
    "expired",
    "failed",
}
_FENCED_JSON_RE = re.compile(r"\A```(?:json)?\s*(\{.*\})\s*```\Z", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class DistilledFields:
    question: str
    summary: str
    resolution: str | None
    systems: list[str]
    code_references: list[str]


@dataclass(frozen=True)
class DistillationEntry:
    run_id: int
    input_digest: str
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "input_digest": self.input_digest,
            "attempt": self.attempt,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DistillationEntry":
        return cls(
            run_id=int(value["run_id"]),
            input_digest=str(value["input_digest"]),
            attempt=max(1, int(value.get("attempt") or 1)),
        )


@dataclass(frozen=True)
class DistillationOutcome:
    status: str
    entry: DistillationEntry
    draft: KnowledgeDraft | None = None
    error: str | None = None


def _bounded_text(value: Any, *, limit: int, required: bool = False) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("distillation text fields must be strings")
    text = str(value or "").strip()
    if required and not text:
        raise ValueError("required distillation text is empty")
    return text[:limit]


def _bounded_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"distillation field {field} must be a list")
    output: list[str] = []
    for candidate in value[:30]:
        text = _bounded_text(candidate, limit=300)
        if text and text not in output:
            output.append(text)
    return output


def parse_distillation_artifact(text: str) -> DistilledFields:
    """Parse the exact bounded artifact contract, accepting one JSON fence."""

    payload_text = str(text or "").strip()
    fenced = _FENCED_JSON_RE.fullmatch(payload_text)
    if fenced is not None:
        payload_text = fenced.group(1)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError("distillation artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("distillation artifact must be a JSON object")
    required_keys = {
        "question",
        "summary",
        "resolution",
        "systems",
        "code_references",
    }
    if set(payload) != required_keys:
        raise ValueError("distillation artifact fields do not match the contract")
    resolution_value = payload["resolution"]
    if resolution_value is not None and not isinstance(resolution_value, str):
        raise ValueError("distillation field resolution must be a string or null")
    return DistilledFields(
        question=_bounded_text(payload["question"], limit=500, required=True),
        summary=_bounded_text(payload["summary"], limit=6_000, required=True),
        resolution=(
            _bounded_text(resolution_value, limit=6_000) if resolution_value is not None else None
        ),
        systems=_bounded_string_list(payload["systems"], field="systems"),
        code_references=_bounded_string_list(
            payload["code_references"],
            field="code_references",
        ),
    )


def serialize_draft(draft: KnowledgeDraft) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "source": draft.source,
        "kind": draft.kind,
        "source_ref": draft.source_ref,
        "title": draft.title,
        "summary": draft.summary,
        "resolution": draft.resolution,
        "entities": list(draft.entities),
        "raw_text": draft.raw_text,
        "extra": dict(draft.extra),
        "source_created_at": timestamp(draft.source_created_at),
        "source_updated_at": timestamp(draft.source_updated_at),
        "archived_at": timestamp(draft.archived_at),
        "distill": True,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def deserialize_draft(value: Mapping[str, Any]) -> KnowledgeDraft:
    return KnowledgeDraft(
        source=str(value["source"]),
        kind=str(value["kind"]),
        source_ref=str(value["source_ref"]),
        title=str(value["title"]),
        summary=str(value["summary"]),
        resolution=(str(value["resolution"]) if value.get("resolution") is not None else None),
        entities=list(value.get("entities") or []),
        raw_text=str(value.get("raw_text") or ""),
        extra=dict(value.get("extra") or {}),
        source_created_at=_parse_timestamp(value.get("source_created_at")),
        source_updated_at=_parse_timestamp(value.get("source_updated_at")),
        archived_at=_parse_timestamp(value.get("archived_at")),
        distill=True,
    )


def _source_ref_hash(draft: KnowledgeDraft) -> str:
    return hashlib.sha256(
        f"{draft.source}:{draft.source_ref}".encode("utf-8")
    ).hexdigest()[:20]


def _idempotency_key(
    draft: KnowledgeDraft,
    *,
    input_digest: str,
    attempt: int,
) -> str:
    return f"knowledge:distill:{_source_ref_hash(draft)}:{input_digest}:a{attempt}"


def _distillation_prompt(draft: KnowledgeDraft) -> str:
    source_payload = {
        "source": draft.source,
        "kind": draft.kind,
        "source_ref": draft.source_ref,
        "title": draft.title,
        "structural_summary": draft.summary,
        "structural_resolution": draft.resolution,
        "entities": list(draft.entities),
        "raw_text": draft.raw_text,
    }
    return (
        "Distill this source-backed knowledge item. Return exactly one JSON object and no "
        "commentary. Required keys: question (string), summary (string), resolution "
        "(string or null), systems (string array), code_references (string array). "
        "State only facts supported by the source. Preserve concrete identifiers, error "
        "messages, decisions, and the actual resolution when present.\n\nSOURCE:\n"
        + json.dumps(source_payload, ensure_ascii=False, sort_keys=True)
    )


async def _resolve_actor(session: AsyncSession, draft: KnowledgeDraft) -> User:
    extra = dict(draft.extra or {})
    actor_user_id = str(extra.get("actor_user_id") or "").strip()
    org_id = str(extra.get("org_id") or "").strip()
    actor = await session.get(User, actor_user_id) if actor_user_id else None
    if actor is not None and org_id and str(actor.org_id) != org_id:
        raise ValueError("knowledge distillation actor does not belong to draft org")
    if actor is None and org_id:
        actor = (
            await session.scalars(
                select(User)
                .where(User.org_id == org_id)
                .order_by(User.created_at.asc(), User.id.asc())
                .limit(1)
            )
        ).first()
    if actor is None and not org_id:
        actor = (
            await session.scalars(
                select(User).order_by(User.created_at.asc(), User.id.asc()).limit(1)
            )
        ).first()
    if actor is None:
        raise LookupError("No workspace user is available for knowledge distillation")
    return actor


async def admit_distillation(
    session: AsyncSession,
    draft: KnowledgeDraft,
    *,
    input_digest: str,
    attempt: int,
) -> DistillationEntry:
    actor = await _resolve_actor(session, draft)
    scoped_draft = replace(
        draft,
        extra={
            **dict(draft.extra or {}),
            "org_id": str(actor.org_id),
            "actor_user_id": str(actor.id),
        },
    )
    attempt = max(1, int(attempt))
    idempotency_key = _idempotency_key(
        draft,
        input_digest=input_digest,
        attempt=attempt,
    )
    result = await admit_work(
        session,
        WorkIntakeEvent(
            source="knowledge",
            event_type="knowledge.distillation",
            org_id=str(actor.org_id),
            actor={"id": str(actor.id), "org_id": str(actor.org_id)},
            target={
                "kind": "knowledge_distillation",
                "thread_id": f"knowledge-distillation:{_source_ref_hash(scoped_draft)}",
                "headless": True,
                "final_answer_target_surface": "headless",
            },
            payload={
                "message": _distillation_prompt(scoped_draft),
                "workspace_ref": {"source": "knowledge_index", "mode": "headless"},
                "metadata": {
                    "origin": "knowledge_index",
                    "originating_surface": "scheduler",
                    "source_surface": "scheduler",
                    "final_answer_target_surface": "headless",
                    "headless": True,
                    "execution_profile": "fast",
                    "recipe": "fast",
                    "knowledge_distillation": {
                        "input_digest": input_digest,
                        "attempt": attempt,
                        "draft": serialize_draft(scoped_draft),
                    },
                },
            },
            policy={
                "producer": "knowledge_index",
                "idempotency_key": idempotency_key,
                "run_event": "knowledge_distillation",
            },
        ),
    )
    if not result.ok or result.run_id is None:
        raise RuntimeError(result.skipped_reason or "knowledge distillation admission failed")
    return DistillationEntry(
        run_id=int(result.run_id),
        input_digest=input_digest,
        attempt=attempt,
    )


async def _latest_final_answer(session: AsyncSession, run_id: int) -> str:
    artifact = (
        await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type == "final_answer",
            )
            .order_by(
                AgentRunArtifactRow.created_at.desc(),
                AgentRunArtifactRow.id.desc(),
            )
            .limit(1)
        )
    ).first()
    return str(artifact.text or "") if artifact is not None else ""


def _draft_from_run(run: AgentRunRow) -> KnowledgeDraft:
    metadata = dict(run.metadata_ or {})
    distillation = metadata.get("knowledge_distillation")
    if not isinstance(distillation, Mapping) or not isinstance(
        distillation.get("draft"), Mapping
    ):
        raise ValueError("knowledge distillation run is missing its draft snapshot")
    return deserialize_draft(distillation["draft"])


def _apply_fields(
    draft: KnowledgeDraft,
    fields: DistilledFields,
    *,
    input_digest: str,
    attempt: int,
) -> KnowledgeDraft:
    entities: list[Any] = []
    for value in [*draft.entities, *fields.systems, *fields.code_references]:
        if value not in entities:
            entities.append(value)
    extra = dict(draft.extra or {})
    extra["distillation"] = {
        "status": "completed",
        "input_digest": input_digest,
        "attempt": attempt,
        "question": fields.question,
        "systems": fields.systems,
        "code_references": fields.code_references,
    }
    return replace(
        draft,
        summary=fields.summary,
        resolution=fields.resolution or draft.resolution,
        entities=entities,
        extra=extra,
        distill=False,
    )


def fallback_draft(
    draft: KnowledgeDraft,
    *,
    input_digest: str,
    attempt: int,
    error: str,
) -> KnowledgeDraft:
    extra = dict(draft.extra or {})
    extra["distillation"] = {
        "status": "failed",
        "input_digest": input_digest,
        "attempt": attempt,
        "error": _bounded_text(error, limit=1_000),
    }
    return replace(draft, extra=extra, distill=False)


async def inspect_distillation(
    session: AsyncSession,
    entry: DistillationEntry,
) -> DistillationOutcome:
    run = await session.get(AgentRunRow, int(entry.run_id))
    if run is None:
        return DistillationOutcome(
            status="retry",
            entry=entry,
            error="knowledge distillation run is missing",
        )
    try:
        draft = _draft_from_run(run)
    except ValueError as exc:
        return DistillationOutcome(status="retry", entry=entry, error=str(exc))
    status = str(run.status or "").strip().lower()
    if status == "completed":
        try:
            fields = parse_distillation_artifact(
                await _latest_final_answer(session, int(run.id))
            )
        except ValueError as exc:
            return DistillationOutcome(
                status="retry",
                entry=entry,
                draft=draft,
                error=str(exc),
            )
        return DistillationOutcome(
            status="completed",
            entry=entry,
            draft=_apply_fields(
                draft,
                fields,
                input_digest=entry.input_digest,
                attempt=entry.attempt,
            ),
        )
    if status in _TERMINAL_FAILURE_STATUSES:
        return DistillationOutcome(
            status="retry",
            entry=entry,
            draft=draft,
            error=f"knowledge distillation run ended with {status}",
        )
    return DistillationOutcome(status="pending", entry=entry, draft=draft)


__all__ = [
    "DISTILLATION_CURSOR_KEY",
    "DISTILLATION_MANIFEST_VERSION",
    "DISTILLATION_MAX_ATTEMPTS",
    "DistillationEntry",
    "DistillationOutcome",
    "admit_distillation",
    "deserialize_draft",
    "fallback_draft",
    "inspect_distillation",
    "parse_distillation_artifact",
    "serialize_draft",
]
