"""Inbound admission for completed meetbot transcript envelopes."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as brain_config
from brain.kernel.common.coercion import coerce_datetime
from brain.platform.async_io import run_blocking
from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.inbound.handlers import (
    InboundEventCompleter,
    InboundHandlerContext,
)
from brain.systems.inbound.surface_admission import (
    SurfaceAdmissionSpec,
    SurfaceIdentity,
    SurfaceTarget,
    admit_surface_envelope,
)
from brain.systems.meetings.message import (
    MAX_TRANSCRIPT_INLINE_CHARS,
    compose_failed_meeting_run_message,
    compose_post_meeting_run_message,
)
from brain.systems.slack.delivery_routes import (
    SlackDeliveryRoute,
    build_delivery_trigger,
    resolve_delivery_route,
)


MEETING_TRANSCRIPT_ENVELOPE_KIND = "meeting_transcript"
ACTION_MEETING_RUN_ADMITTED = "meeting.run_admitted"
MEETING_UPLOAD_ROOT = brain_config.BRAIN_DIR / "brain" / "uploads" / "meetings"
_MEET_URL = re.compile(
    r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:[?#].*)?$",
    re.IGNORECASE,
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class MeetingTranscriptValidationError(ValueError):
    """The completion callback does not match the meetbot v1 envelope."""


async def process_meeting_transcript_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
) -> dict[str, Any]:
    """Admit one post-meeting run, preserving its Slack route when possible."""

    return await admit_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=MEETING_TRANSCRIPT_ADMISSION,
    )


async def _resolve_meeting_identity(
    _session: AsyncSession,
    context: InboundHandlerContext,
    _normalized: Mapping[str, Any],
) -> SurfaceIdentity:
    authority_user_id = str(context.owner_user_id or "").strip() or None
    return SurfaceIdentity(authority_user_id=authority_user_id)


async def _build_meeting_payload(
    session: AsyncSession,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    identity: SurfaceIdentity,
) -> dict[str, Any]:
    payload = _validate_payload(normalized.get("payload"))

    if payload["status"] == "failed":
        run_message = compose_failed_meeting_run_message(payload)
    else:
        transcript_text, source_truncated, read_error = await _read_transcript_excerpt(
            payload["_resolved_transcript_md_path"]
        )
        if read_error:
            transcript_text = (
                "The transcript file could not be read by the brain runtime. "
                f"Expected full transcript at {payload['transcript_md_path']}. "
                f"Read error: {read_error}"
            )
        run_message = compose_post_meeting_run_message(
            payload,
            transcript_text,
            source_truncated=source_truncated,
        )

    origin = payload["origin"]
    slack_route = await resolve_delivery_route(
        session,
        org_id=context.org_id,
        channel=origin.get("channel"),
        thread_ts=origin.get("thread_ts"),
    )
    trigger_payload = _build_meeting_work_intake_payload(
        context=context,
        event=event,
        normalized=normalized,
        payload=payload,
        run_message=run_message,
        slack_route=slack_route,
        authority_user_id=str(identity.authority_user_id),
    )
    trigger_payload["_meeting_admission"] = {
        "payload": payload,
        "slack_route": slack_route,
    }
    return trigger_payload


def _meeting_target(
    trigger_payload: Mapping[str, Any],
    _normalized: Mapping[str, Any],
) -> SurfaceTarget:
    admission_context = dict(trigger_payload.get("_meeting_admission") or {})
    payload = dict(admission_context.get("payload") or {})
    slack_route = admission_context.get("slack_route")
    target = {
        "kind": MEETING_TRANSCRIPT_ENVELOPE_KIND,
        "session_id": payload["session_id"],
        "routing": slack_route.routing if slack_route else "run_inbox",
    }
    if slack_route:
        target.update(
            {
                "channel": slack_route.channel,
                "thread_ts": slack_route.thread_ts,
            }
        )
    return SurfaceTarget(value=target)


def _meeting_ack(
    _event_id: str,
    _normalized: Mapping[str, Any],
    trigger_payload: Mapping[str, Any],
    target: Mapping[str, Any],
) -> Mapping[str, Any]:
    admission_context = dict(trigger_payload.get("_meeting_admission") or {})
    payload = dict(admission_context.get("payload") or {})
    return {
        "meeting_status": payload["status"],
        "routing": target["routing"],
    }


def _validate_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, Mapping):
        raise MeetingTranscriptValidationError("meeting_transcript payload must be an object")
    payload = dict(raw_payload)
    session_id = _required_text(payload, "session_id")
    if not _SESSION_ID.fullmatch(session_id):
        raise MeetingTranscriptValidationError(
            "session_id must contain only letters, numbers, underscores, and hyphens"
        )
    meeting_url = _required_text(payload, "meeting_url")
    if not _MEET_URL.fullmatch(meeting_url):
        raise MeetingTranscriptValidationError("meeting_url must be a valid Google Meet URL")
    status = _required_text(payload, "status").lower()
    if status not in {"ended", "failed"}:
        raise MeetingTranscriptValidationError("status must be ended or failed")

    caption_lines = payload.get("caption_lines")
    if isinstance(caption_lines, bool):
        raise MeetingTranscriptValidationError("caption_lines must be a non-negative integer")
    try:
        caption_lines = int(caption_lines)
    except (TypeError, ValueError) as exc:
        raise MeetingTranscriptValidationError(
            "caption_lines must be a non-negative integer"
        ) from exc
    if caption_lines < 0:
        raise MeetingTranscriptValidationError("caption_lines must be a non-negative integer")

    participants_raw = payload.get("participants")
    if not isinstance(participants_raw, list):
        raise MeetingTranscriptValidationError("participants must be an array")
    participants = [
        str(item).strip()
        for item in participants_raw
        if str(item or "").strip()
    ]
    origin_raw = payload.get("origin")
    if not isinstance(origin_raw, Mapping):
        raise MeetingTranscriptValidationError("origin must be an object")
    origin = {
        key: value
        for key in ("channel", "thread_ts")
        if (value := str(origin_raw.get(key) or "").strip())
    }
    if origin.get("thread_ts") and not origin.get("channel"):
        raise MeetingTranscriptValidationError("origin.thread_ts requires origin.channel")

    started_at = str(payload.get("started_at") or "").strip() or None
    ended_at = str(payload.get("ended_at") or "").strip() or None
    if status == "ended":
        if coerce_datetime(started_at) is None or coerce_datetime(ended_at) is None:
            raise MeetingTranscriptValidationError(
                "ended meetings require valid started_at and ended_at timestamps"
            )
        transcript_path = _validated_session_file(
            payload.get("transcript_path"),
            session_id=session_id,
            filename="transcript.jsonl",
            field="transcript_path",
        )
        transcript_md_path = _validated_session_file(
            payload.get("transcript_md_path"),
            session_id=session_id,
            filename="transcript.md",
            field="transcript_md_path",
        )
    else:
        transcript_path = _optional_session_file(
            payload.get("transcript_path"),
            session_id=session_id,
            filename="transcript.jsonl",
            field="transcript_path",
        )
        transcript_md_path = _optional_session_file(
            payload.get("transcript_md_path"),
            session_id=session_id,
            filename="transcript.md",
            field="transcript_md_path",
        )

    return {
        "session_id": session_id,
        "meeting_url": meeting_url,
        "status": status,
        "transcript_path": str(payload.get("transcript_path") or "").strip() or None,
        "transcript_md_path": str(payload.get("transcript_md_path") or "").strip() or None,
        "_resolved_transcript_path": str(transcript_path) if transcript_path else None,
        "_resolved_transcript_md_path": str(transcript_md_path) if transcript_md_path else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "caption_lines": caption_lines,
        "participants": participants,
        "origin": origin,
        "requested_by": str(payload.get("requested_by") or "").strip() or None,
        "warning": str(payload.get("warning") or "").strip() or None,
        "error": str(payload.get("error") or "").strip() or None,
    }


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise MeetingTranscriptValidationError(f"{field} is required")
    return value


def _validated_session_file(
    value: Any,
    *,
    session_id: str,
    filename: str,
    field: str,
) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise MeetingTranscriptValidationError(f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        path = brain_config.BRAIN_DIR / path
    resolved = path.resolve(strict=False)
    expected = (Path(MEETING_UPLOAD_ROOT) / session_id / filename).resolve(strict=False)
    if resolved != expected:
        raise MeetingTranscriptValidationError(
            f"{field} must point to brain/uploads/meetings/{session_id}/{filename}"
        )
    return resolved


def _optional_session_file(
    value: Any,
    *,
    session_id: str,
    filename: str,
    field: str,
) -> Path | None:
    if not str(value or "").strip():
        return None
    return _validated_session_file(
        value,
        session_id=session_id,
        filename=filename,
        field=field,
    )


async def _read_transcript_excerpt(path: str) -> tuple[str, bool, str | None]:
    def read() -> tuple[str, bool]:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_TRANSCRIPT_INLINE_CHARS + 1)
        return text, len(text) > MAX_TRANSCRIPT_INLINE_CHARS

    try:
        text, truncated = await run_blocking(read)
    except OSError as exc:
        return "", False, str(exc)
    return text[:MAX_TRANSCRIPT_INLINE_CHARS], truncated, None


def _build_meeting_work_intake_payload(
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    payload: Mapping[str, Any],
    run_message: str,
    slack_route: SlackDeliveryRoute | None,
    authority_user_id: str,
) -> dict[str, Any]:
    metadata = {
        "execution_profile": "fast",
        "origin": "meeting_transcript",
        "obligation": "none",
        "inbound_event": {
            "event_id": str(event.id),
            "origin": normalized.get("origin"),
            "kind": MEETING_TRANSCRIPT_ENVELOPE_KIND,
            "connection_id": context.connection_id,
        },
        "meeting": {
            key: value
            for key, value in payload.items()
            if not key.startswith("_")
        },
    }
    policy = {
        "producer": "meetbot",
        "idempotency_key": (
            str(normalized.get("idempotency_key") or "").strip()
            or f"meeting-{payload['session_id']}"
        ),
        "run_event": "meeting_transcript_received",
    }
    actor = {
        "id": authority_user_id,
        "org_id": context.org_id,
        "principal_type": "external_source_authority",
        "name": context.display_name or "Meetbot",
    }
    if slack_route:
        delivery_trigger = build_delivery_trigger(
            slack_route,
            message_ts=slack_route.thread_ts,
            slack_user_id=payload.get("requested_by"),
            text="Meetbot transcript completion",
            triggering_surface="meeting",
        )
        metadata.update(delivery_trigger.metadata)
        target = delivery_trigger.target
        source = "slack"
    else:
        target = {
            "kind": "inbound_submission",
            "event_id": str(event.id),
            "connection_id": context.connection_id,
            "thread_id": f"meeting:{payload['session_id']}",
        }
        source = "meetbot"

    return {
        "source": source,
        "event_type": "meeting.transcript",
        "org_id": context.org_id,
        "actor": actor,
        "target": target,
        "payload": {
            "run_message": run_message,
            "metadata": metadata,
            "user_id": authority_user_id,
        },
        "policy": policy,
    }


MEETING_TRANSCRIPT_ADMISSION = SurfaceAdmissionSpec(
    kind=MEETING_TRANSCRIPT_ENVELOPE_KIND,
    action_type=ACTION_MEETING_RUN_ADMITTED,
    success_operation="meeting_run_admitted",
    failure_operation="meeting_run_admission_failed",
    tool_type="meeting_transcript_intake",
    resolve_identity=_resolve_meeting_identity,
    build_payload=_build_meeting_payload,
    build_target=_meeting_target,
    build_ack=_meeting_ack,
    success_tool_status="accepted",
    success_reasoning=(
        "The meetbot completion was admitted as a post-meeting AgentRun with "
        "the available Slack origin preserved."
    ),
    admission_failure_reasoning=(
        "The post-meeting AgentRun could not be admitted."
    ),
    missing_authority_error="Meetbot connection has no authority user",
    missing_authority_reasoning="Meetbot connection has no authority user",
    payload_error_types=(MeetingTranscriptValidationError,),
    invalid_payload_reason="invalid_meeting_transcript_payload",
    payload_failure_operation="meeting_payload_rejected",
    missing_authority_reason="invalid_meeting_transcript_payload",
    missing_authority_failure_operation="meeting_payload_rejected",
    include_origin_in_outcome=False,
    action_result_target_fields=("session_id",),
)


__all__ = [
    "ACTION_MEETING_RUN_ADMITTED",
    "MEETING_TRANSCRIPT_ADMISSION",
    "MEETING_TRANSCRIPT_ENVELOPE_KIND",
    "MEETING_UPLOAD_ROOT",
    "MeetingTranscriptValidationError",
    "process_meeting_transcript_envelope",
]
