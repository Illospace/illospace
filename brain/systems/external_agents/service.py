"""Domain service for Illo-to-personal-agent bridge workflows."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
    ExternalAgentTaskArtifactRow,
    ExternalAgentTaskEventRow,
    ExternalAgentTaskRow,
)
from brain.platform.db.models.idea import Idea, IdeaStateLog, IdeaThread, UserMention
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
    NOTIFICATION_SOURCE_WORKSPACE,
    NotificationEvent,
)
from brain.platform.db.models.org import User
from brain.systems.runs.domain import AgentRunEvent, AgentRunRequest, EventVisibility, RunProfile, RunRecipe
from brain.systems.runs.events import run_event
from brain.systems.runs.ids import trace_id_for_run_id
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status


TOKEN_PREFIX = "illo_conn_"

SCOPE_CONNECTION_HEARTBEAT = "connection:heartbeat"
SCOPE_TASK_CLAIM = "task:claim"
SCOPE_TASK_UPDATE = "task:update"
SCOPE_TASK_COMPLETE = "task:complete"
SCOPE_ARTIFACT_WRITE = "artifact:write"
SCOPE_WORKSPACE_READ = "workspace:read"
SCOPE_ILLO_ASK = "illo:ask"
SCOPE_ILLO_THREAD_CREATE = "illo:thread:create"
SCOPE_ILLO_THREAD_WRITE = "illo:thread:write"

DEFAULT_BRIDGE_SCOPES = (
    SCOPE_CONNECTION_HEARTBEAT,
    SCOPE_TASK_CLAIM,
    SCOPE_TASK_UPDATE,
    SCOPE_TASK_COMPLETE,
    SCOPE_ARTIFACT_WRITE,
    SCOPE_WORKSPACE_READ,
    SCOPE_ILLO_ASK,
    SCOPE_ILLO_THREAD_CREATE,
    SCOPE_ILLO_THREAD_WRITE,
)

HEADLESS_ASK_BLOCKED_TOOLS = (
    "cortex_reply",
    "cortex_visual_reply",
    "manage_idea",
    "manage_workspace_app",
    "post_chat_message",
)

TASK_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
CONNECTION_ADMIN_ROLES = {"owner", "admin"}


@dataclass(frozen=True)
class AgentBridgePrincipal:
    connection_id: str
    org_id: str
    owner_user_id: str
    token_id: str
    scopes: frozenset[str]
    connection_display_name: str
    agent_kind: str


class ExternalAgentError(RuntimeError):
    """Base class for external-agent bridge errors."""


class ExternalAgentAuthError(ExternalAgentError):
    """Raised when bridge token authentication fails."""


class ExternalAgentPermissionError(ExternalAgentError):
    """Raised when a bridge token lacks a required scope."""


class ExternalAgentNotFound(ExternalAgentError):
    """Raised when a bridge-owned resource cannot be found."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_comparable(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def generate_connection_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def hash_connection_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return str(token)[:18]


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _ensure_org_user(session: Session, *, org_id: str, user_id: str) -> User:
    user = session.get(User, str(user_id))
    if user is None or str(user.org_id) != str(org_id):
        raise ExternalAgentNotFound("User not found in organization")
    return user


def _require_connection(
    session: Session,
    *,
    connection_id: str,
    org_id: str | None = None,
) -> ExternalAgentConnectionRow:
    connection = session.get(ExternalAgentConnectionRow, str(connection_id))
    if connection is None:
        raise ExternalAgentNotFound("External agent connection not found")
    if org_id is not None and str(connection.org_id) != str(org_id):
        raise ExternalAgentNotFound("External agent connection not found")
    return connection


def require_connection(
    session: Session,
    *,
    connection_id: str,
    org_id: str | None = None,
) -> ExternalAgentConnectionRow:
    return _require_connection(session, connection_id=connection_id, org_id=org_id)


def user_can_manage_connection(
    connection: ExternalAgentConnectionRow,
    *,
    user_id: str,
    role: str | None,
) -> bool:
    return str(role or "").lower() in CONNECTION_ADMIN_ROLES or str(connection.owner_user_id) == str(user_id)


def require_connection_for_user(
    session: Session,
    *,
    connection_id: str,
    org_id: str,
    user_id: str,
    role: str | None,
    require_manage: bool = False,
) -> ExternalAgentConnectionRow:
    connection = require_connection(session, connection_id=connection_id, org_id=org_id)
    if require_manage and not user_can_manage_connection(connection, user_id=user_id, role=role):
        raise ExternalAgentPermissionError("Permission denied for external agent connection")
    return connection


def _connection_disabled(connection: ExternalAgentConnectionRow) -> bool:
    return bool(connection.disabled_at or str(connection.status or "").lower() == "disabled")


def _require_task_for_principal(
    session: Session,
    principal: AgentBridgePrincipal,
    task_id: str,
) -> ExternalAgentTaskRow:
    task = session.get(ExternalAgentTaskRow, str(task_id))
    if (
        task is None
        or str(task.connection_id) != principal.connection_id
        or str(task.org_id) != principal.org_id
    ):
        raise ExternalAgentNotFound("External agent task not found")
    return task


def require_task_for_principal(
    session: Session,
    principal: AgentBridgePrincipal,
    task_id: str,
) -> ExternalAgentTaskRow:
    return _require_task_for_principal(session, principal, task_id)


def serialize_connection(row: ExternalAgentConnectionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "owner_user_id": str(row.owner_user_id),
        "display_name": row.display_name,
        "agent_kind": row.agent_kind,
        "transport": row.transport,
        "status": row.status,
        "endpoint_url": row.endpoint_url,
        "remote_agent_id": row.remote_agent_id,
        "remote_session_key": row.remote_session_key,
        "remote_agent_card": _json_dict(row.remote_agent_card),
        "capabilities": _json_dict(row.capabilities),
        "last_seen_at": _iso(row.last_seen_at),
        "last_tested_at": _iso(row.last_tested_at),
        "last_error": row.last_error,
        "metadata": _json_dict(row.metadata_),
        "disabled_at": _iso(row.disabled_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def serialize_token(row: ExternalAgentConnectionTokenRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "connection_id": str(row.connection_id),
        "token_prefix": row.token_prefix,
        "name": row.name,
        "scopes": sorted(str(scope) for scope in _json_list(row.scopes)),
        "created_at": _iso(row.created_at),
        "last_used_at": _iso(row.last_used_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
    }


def serialize_event(row: ExternalAgentTaskEventRow) -> dict[str, Any]:
    return {
        "id": int(row.id) if row.id is not None else None,
        "task_id": str(row.task_id),
        "sequence_no": row.sequence_no,
        "event_type": row.event_type,
        "status": row.status,
        "message": row.message,
        "payload": _json_dict(row.payload),
        "remote_event_id": row.remote_event_id,
        "producer": row.producer,
        "visibility": row.visibility,
        "created_at": _iso(row.created_at),
    }


def serialize_artifact(row: ExternalAgentTaskArtifactRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "task_id": str(row.task_id),
        "kind": row.kind,
        "title": row.title,
        "mime_type": row.mime_type,
        "content_text": row.content_text,
        "content_json": row.content_json,
        "uri": row.uri,
        "upload_id": row.upload_id,
        "metadata": _json_dict(row.metadata_),
        "created_at": _iso(row.created_at),
    }


def serialize_task(
    row: ExternalAgentTaskRow,
    *,
    include_events: bool = False,
    include_artifacts: bool = False,
    session: Session | None = None,
) -> dict[str, Any]:
    data = {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "connection_id": str(row.connection_id),
        "created_by_user_id": str(row.created_by_user_id) if row.created_by_user_id else None,
        "source_surface": row.source_surface,
        "source_idea_id": str(row.source_idea_id) if row.source_idea_id else None,
        "source_thread_message_id": row.source_thread_message_id,
        "source_chat_conversation_id": str(row.source_chat_conversation_id) if row.source_chat_conversation_id else None,
        "source_chat_message_id": row.source_chat_message_id,
        "title": row.title,
        "instructions": row.instructions,
        "input_parts": _json_list(row.input_parts),
        "status": row.status,
        "remote_task_id": row.remote_task_id,
        "remote_run_id": row.remote_run_id,
        "remote_session_id": row.remote_session_id,
        "illo_run_id": row.illo_run_id,
        "idempotency_key": row.idempotency_key,
        "deadline_at": _iso(row.deadline_at),
        "claimed_at": _iso(row.claimed_at),
        "submitted_at": _iso(row.submitted_at),
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "failed_at": _iso(row.failed_at),
        "cancelled_at": _iso(row.cancelled_at),
        "result_summary": row.result_summary,
        "error": row.error,
        "metadata": _json_dict(row.metadata_),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if session is not None and include_events:
        data["events"] = [
            serialize_event(event)
            for event in session.scalars(
                select(ExternalAgentTaskEventRow)
                .where(ExternalAgentTaskEventRow.task_id == str(row.id))
                .order_by(ExternalAgentTaskEventRow.sequence_no.asc(), ExternalAgentTaskEventRow.id.asc())
            ).all()
        ]
    if session is not None and include_artifacts:
        data["artifacts"] = [
            serialize_artifact(artifact)
            for artifact in session.scalars(
                select(ExternalAgentTaskArtifactRow)
                .where(ExternalAgentTaskArtifactRow.task_id == str(row.id))
                .order_by(ExternalAgentTaskArtifactRow.created_at.asc(), ExternalAgentTaskArtifactRow.id.asc())
            ).all()
        ]
    return data


def create_connection(
    session: Session,
    *,
    org_id: str,
    owner_user_id: str,
    display_name: str,
    agent_kind: str,
    transport: str = "bridge_pull",
    endpoint_url: str | None = None,
    remote_agent_id: str | None = None,
    remote_agent_card: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentConnectionRow:
    _ensure_org_user(session, org_id=str(org_id), user_id=str(owner_user_id))
    row = ExternalAgentConnectionRow(
        org_id=str(org_id),
        owner_user_id=str(owner_user_id),
        display_name=str(display_name).strip(),
        agent_kind=str(agent_kind or "custom").strip().lower(),
        transport=str(transport or "bridge_pull").strip().lower(),
        status="pending",
        endpoint_url=endpoint_url,
        remote_agent_id=remote_agent_id,
        remote_agent_card=dict(remote_agent_card or {}),
        capabilities=dict(capabilities or {}),
        metadata_=dict(metadata or {}),
    )
    session.add(row)
    session.flush()
    return row


def list_connections(
    session: Session,
    *,
    org_id: str,
    owner_user_id: str | None = None,
) -> list[ExternalAgentConnectionRow]:
    stmt = (
        select(ExternalAgentConnectionRow)
        .where(ExternalAgentConnectionRow.org_id == str(org_id))
        .order_by(ExternalAgentConnectionRow.created_at.desc(), ExternalAgentConnectionRow.id.desc())
    )
    if owner_user_id:
        stmt = stmt.where(ExternalAgentConnectionRow.owner_user_id == str(owner_user_id))
    return list(session.scalars(stmt).all())


def mint_connection_token(
    session: Session,
    *,
    connection_id: str,
    org_id: str,
    name: str = "Bridge token",
    scopes: Sequence[str] | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, ExternalAgentConnectionTokenRow]:
    connection = _require_connection(session, connection_id=str(connection_id), org_id=str(org_id))
    if _connection_disabled(connection):
        raise ExternalAgentPermissionError("External agent connection is disabled")
    raw_token = generate_connection_token()
    scope_values = [str(scope).strip() for scope in (scopes or DEFAULT_BRIDGE_SCOPES) if str(scope).strip()]
    row = ExternalAgentConnectionTokenRow(
        connection_id=str(connection.id),
        org_id=str(connection.org_id),
        owner_user_id=str(connection.owner_user_id),
        token_hash=hash_connection_token(raw_token),
        token_prefix=token_prefix(raw_token),
        name=str(name or "Bridge token"),
        scopes=scope_values,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return raw_token, row


def authenticate_bridge_token(
    session: Session,
    token: str,
    *,
    required_scope: str | None = None,
) -> AgentBridgePrincipal:
    value = str(token or "").strip()
    if not value:
        raise ExternalAgentAuthError("Bridge token is required")
    row = session.scalars(
        select(ExternalAgentConnectionTokenRow)
        .where(ExternalAgentConnectionTokenRow.token_hash == hash_connection_token(value))
        .limit(1)
    ).first()
    if row is None or row.revoked_at is not None:
        raise ExternalAgentAuthError("Invalid bridge token")
    if row.expires_at is not None and _utc_comparable(row.expires_at) < utcnow():
        raise ExternalAgentAuthError("Bridge token expired")
    connection = session.get(ExternalAgentConnectionRow, str(row.connection_id))
    if connection is None or _connection_disabled(connection):
        raise ExternalAgentAuthError("External agent connection disabled")
    scopes = frozenset(str(scope) for scope in _json_list(row.scopes))
    if required_scope and "*" not in scopes and required_scope not in scopes:
        raise ExternalAgentPermissionError(f"Bridge token is missing scope: {required_scope}")
    row.last_used_at = utcnow()
    return AgentBridgePrincipal(
        connection_id=str(row.connection_id),
        org_id=str(row.org_id),
        owner_user_id=str(row.owner_user_id),
        token_id=str(row.id),
        scopes=scopes,
        connection_display_name=str(connection.display_name),
        agent_kind=str(connection.agent_kind),
    )


def record_heartbeat(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    status: str | None = None,
    capabilities: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentConnectionRow:
    connection = _require_connection(session, connection_id=principal.connection_id, org_id=principal.org_id)
    connection.last_seen_at = utcnow()
    connection.status = str(status or "online")
    connection.last_error = None
    if capabilities is not None:
        connection.capabilities = dict(capabilities)
    if metadata is not None:
        existing = _json_dict(connection.metadata_)
        existing.update(dict(metadata))
        connection.metadata_ = existing
    session.flush()
    return connection


def _next_event_sequence(session: Session, task_id: str) -> int:
    current = session.scalar(
        select(func.max(ExternalAgentTaskEventRow.sequence_no)).where(
            ExternalAgentTaskEventRow.task_id == str(task_id)
        )
    )
    return int(current or 0) + 1


def append_task_event(
    session: Session,
    task: ExternalAgentTaskRow,
    *,
    event_type: str,
    status: str | None = None,
    message: str | None = None,
    payload: Mapping[str, Any] | None = None,
    remote_event_id: str | None = None,
    producer: str = "external_agent_bridge",
    visibility: str = "public",
) -> ExternalAgentTaskEventRow:
    event = ExternalAgentTaskEventRow(
        task_id=str(task.id),
        org_id=str(task.org_id),
        connection_id=str(task.connection_id),
        sequence_no=_next_event_sequence(session, str(task.id)),
        event_type=str(event_type),
        status=status,
        message=message,
        payload=dict(payload or {}),
        remote_event_id=remote_event_id,
        producer=producer,
        visibility=visibility,
    )
    session.add(event)
    session.flush()
    return event


def _idea_project_context(idea: Idea) -> dict[str, Any] | None:
    details = _json_dict(getattr(idea, "agent_details", None))
    value = details.get("project_context") or details.get("project_context_snapshot")
    return dict(value) if isinstance(value, dict) else None


def _thread_context(session: Session, idea_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(IdeaThread)
            .where(IdeaThread.idea_id == str(idea_id))
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(max(1, min(int(limit), 100)))
        ).all()
    )
    rows.reverse()
    return [
        {
            "id": row.id,
            "role": row.role,
            "content": row.content,
            "attachments": row.attachments or [],
            "metadata": _json_dict(row.metadata_),
            "message_type": row.message_type,
            "user_id": str(row.user_id) if row.user_id else None,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _idea_for_org(session: Session, *, idea_id: str, org_id: str) -> Idea:
    idea = session.scalars(
        select(Idea).where(Idea.id == str(idea_id), Idea.org_id == str(org_id))
    ).first()
    if idea is None:
        raise ExternalAgentNotFound("Idea not found")
    return idea


def require_idea_for_org(session: Session, *, idea_id: str, org_id: str) -> Idea:
    return _idea_for_org(session, idea_id=idea_id, org_id=org_id)


def create_external_task_for_idea(
    session: Session,
    *,
    org_id: str,
    user_id: str,
    idea_id: str,
    connection_id: str,
    instructions: str,
    title: str | None = None,
    include_thread_context: bool = True,
    include_project_context: bool = True,
    metadata: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[ExternalAgentTaskRow, IdeaThread]:
    connection = _require_connection(session, connection_id=str(connection_id), org_id=str(org_id))
    if _connection_disabled(connection):
        raise ExternalAgentPermissionError("External agent connection is disabled")
    idea = _idea_for_org(session, idea_id=str(idea_id), org_id=str(org_id))
    _ensure_org_user(session, org_id=str(org_id), user_id=str(user_id))

    parts: list[dict[str, Any]] = [
        {
            "type": "task_request",
            "instructions": str(instructions),
            "idea": {
                "id": str(idea.id),
                "title": idea.title,
                "description": idea.description,
                "status": idea.status,
            },
        }
    ]
    if include_thread_context:
        parts.append({"type": "thread_context", "messages": _thread_context(session, str(idea.id))})
    if include_project_context:
        project_context = _idea_project_context(idea)
        if project_context:
            parts.append({"type": "project_context", "project_context": project_context})

    task = ExternalAgentTaskRow(
        org_id=str(org_id),
        connection_id=str(connection.id),
        created_by_user_id=str(user_id),
        source_surface="cortex",
        source_idea_id=str(idea.id),
        title=str(title or idea.title or "External agent task"),
        instructions=str(instructions),
        input_parts=parts,
        status="queued",
        idempotency_key=idempotency_key or f"cortex:{idea.id}:{uuid.uuid4()}",
        metadata_=dict(metadata or {}),
    )
    session.add(task)
    session.flush()
    append_task_event(
        session,
        task,
        event_type="external_task.created",
        status=task.status,
        message=f"Delegated to {connection.display_name}",
        payload={"idea_id": str(idea.id), "connection_id": str(connection.id)},
        producer="illo",
    )

    current_status = idea.status
    if current_status in {"emerged", "needs_input", "unread_reply", "active"}:
        idea.status = "working"
        idea.updated_at = utcnow()
        session.add(
            IdeaStateLog(
                idea_id=str(idea.id),
                from_state=current_status,
                to_state="working",
                trigger="external_agent_task_created",
            )
        )

    status_message = IdeaThread(
        idea_id=str(idea.id),
        role="illo",
        content=f"Delegated to {connection.display_name}: {str(instructions).strip()}",
        user_id=None,
        message_type="agent_status",
        metadata_={
            "external_agent_task_id": str(task.id),
            "external_agent_connection_id": str(connection.id),
            "external_agent_display_name": connection.display_name,
        },
    )
    session.add(status_message)
    session.flush()
    task.source_thread_message_id = status_message.id
    session.flush()
    return task, status_message


def claim_tasks(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    max_tasks: int = 1,
) -> list[ExternalAgentTaskRow]:
    stmt = (
        select(ExternalAgentTaskRow)
        .where(
            ExternalAgentTaskRow.connection_id == principal.connection_id,
            ExternalAgentTaskRow.org_id == principal.org_id,
            ExternalAgentTaskRow.status == "queued",
        )
        .order_by(ExternalAgentTaskRow.created_at.asc(), ExternalAgentTaskRow.id.asc())
        .limit(max(1, min(int(max_tasks or 1), 10)))
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = list(session.scalars(stmt).all())
    now = utcnow()
    for task in rows:
        task.status = "claimed"
        task.claimed_at = now
        append_task_event(
            session,
            task,
            event_type="external_task.claimed",
            status=task.status,
            message="Task claimed by bridge",
            producer="external_agent_bridge",
        )
    session.flush()
    return rows


def update_task_event(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    task_id: str,
    event_type: str,
    status: str | None = None,
    message: str | None = None,
    payload: Mapping[str, Any] | None = None,
    remote_event_id: str | None = None,
) -> ExternalAgentTaskEventRow:
    task = _require_task_for_principal(session, principal, task_id)
    normalized_status = str(status).strip().lower() if status else None
    if normalized_status and normalized_status not in TASK_TERMINAL_STATUSES:
        task.status = normalized_status
        if normalized_status == "running" and task.started_at is None:
            task.started_at = utcnow()
        if normalized_status == "submitted" and task.submitted_at is None:
            task.submitted_at = utcnow()
    event = append_task_event(
        session,
        task,
        event_type=event_type,
        status=normalized_status,
        message=message,
        payload=payload,
        remote_event_id=remote_event_id,
    )
    session.flush()
    return event


def append_artifact(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    task_id: str,
    kind: str,
    title: str | None = None,
    mime_type: str | None = None,
    content_text: str | None = None,
    content_json: Mapping[str, Any] | None = None,
    uri: str | None = None,
    upload_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentTaskArtifactRow:
    task = _require_task_for_principal(session, principal, task_id)
    artifact = ExternalAgentTaskArtifactRow(
        task_id=str(task.id),
        org_id=str(task.org_id),
        connection_id=str(task.connection_id),
        kind=str(kind or "text"),
        title=title,
        mime_type=mime_type,
        content_text=content_text,
        content_json=dict(content_json) if content_json is not None else None,
        uri=uri,
        upload_id=upload_id,
        metadata_=dict(metadata or {}),
    )
    session.add(artifact)
    session.flush()
    append_task_event(
        session,
        task,
        event_type="external_task.artifact_added",
        status=task.status,
        message=title or kind,
        payload={"artifact_id": str(artifact.id), "kind": artifact.kind},
    )
    return artifact


def _thread_message_payload(message: IdeaThread) -> dict[str, Any]:
    return {
        "id": message.id,
        "idea_id": str(message.idea_id) if message.idea_id else None,
        "role": message.role,
        "content": message.content,
        "attachments": message.attachments or [],
        "metadata": _json_dict(message.metadata_),
        "user_id": str(message.user_id) if message.user_id else None,
        "message_type": message.message_type,
        "created_at": _iso(message.created_at),
    }


def serialize_thread_message(message: IdeaThread) -> dict[str, Any]:
    return _thread_message_payload(message)


def _add_external_agent_thread_message(
    session: Session,
    *,
    task: ExternalAgentTaskRow,
    content: str,
    message_type: str,
) -> IdeaThread | None:
    if not task.source_idea_id:
        return None
    idea = session.get(Idea, str(task.source_idea_id))
    if idea is None:
        return None
    message = IdeaThread(
        idea_id=str(idea.id),
        role="illo",
        content=str(content),
        user_id=None,
        message_type=message_type,
        metadata_={
            "external_agent_task_id": str(task.id),
            "external_agent_connection_id": str(task.connection_id),
        },
    )
    session.add(message)
    if message_type == "agent_response" and idea.status != "resolved":
        previous = idea.status
        idea.status = "unread_reply"
        idea.updated_at = utcnow()
        session.add(
            IdeaStateLog(
                idea_id=str(idea.id),
                from_state=previous,
                to_state="unread_reply",
                trigger="external_agent_task_completed",
            )
        )
        if idea.user_id and idea.org_id:
            NotificationEventRepository(session).create_or_coalesce(
                org_id=str(idea.org_id),
                user_id=str(idea.user_id),
                source=NOTIFICATION_SOURCE_WORKSPACE,
                kind=NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
                actor_user_id=None,
                title=f"Personal agent replied in {idea.title}",
                body=_compact_text(content),
                coalesce_key=f"workspace:external_agent_reply:{idea.user_id}:{idea.id}:{task.id}",
                payload={
                    "preview": _compact_text(content),
                    "idea_title": idea.title,
                    "external_agent_task_id": str(task.id),
                },
                idea_id=str(idea.id),
            )
    session.flush()
    return message


def complete_task(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    task_id: str,
    result_summary: str,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> tuple[ExternalAgentTaskRow, IdeaThread | None]:
    task = _require_task_for_principal(session, principal, task_id)
    for artifact in artifacts or []:
        append_artifact(
            session,
            principal,
            task_id=str(task.id),
            kind=str(artifact.get("kind") or "text"),
            title=artifact.get("title"),
            mime_type=artifact.get("mime_type"),
            content_text=artifact.get("content_text"),
            content_json=artifact.get("content_json") if isinstance(artifact.get("content_json"), Mapping) else None,
            uri=artifact.get("uri"),
            upload_id=artifact.get("upload_id"),
            metadata=artifact.get("metadata") if isinstance(artifact.get("metadata"), Mapping) else None,
        )
    task.status = "completed"
    task.completed_at = utcnow()
    task.result_summary = str(result_summary or "")
    event = append_task_event(
        session,
        task,
        event_type="external_task.completed",
        status=task.status,
        message=_compact_text(result_summary, limit=240),
        payload=dict(payload or {}),
    )
    thread_message = _add_external_agent_thread_message(
        session,
        task=task,
        content=str(result_summary or "External agent completed the task."),
        message_type="agent_response",
    )
    task.metadata_ = {**_json_dict(task.metadata_), "completed_event_id": event.id}
    session.flush()
    return task, thread_message


def fail_task(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    task_id: str,
    error: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[ExternalAgentTaskRow, IdeaThread | None]:
    task = _require_task_for_principal(session, principal, task_id)
    task.status = "failed"
    task.failed_at = utcnow()
    task.error = str(error or "External agent task failed")
    append_task_event(
        session,
        task,
        event_type="external_task.failed",
        status=task.status,
        message=task.error,
        payload=dict(payload or {}),
    )
    thread_message = _add_external_agent_thread_message(
        session,
        task=task,
        content=f"External agent task failed: {task.error}",
        message_type="agent_status",
    )
    session.flush()
    return task, thread_message


def search_workspace(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        return {"query": text, "results": []}
    pattern = f"%{text}%"
    max_results = max(1, min(int(limit or 10), 25))
    results: list[dict[str, Any]] = []

    ideas = session.scalars(
        select(Idea)
        .where(
            Idea.org_id == principal.org_id,
            Idea.archived_at.is_(None),
            or_(Idea.title.ilike(pattern), Idea.description.ilike(pattern)),
        )
        .order_by(Idea.updated_at.desc(), Idea.id.desc())
        .limit(max_results)
    ).all()
    for idea in ideas:
        results.append(
            {
                "type": "idea",
                "idea_id": str(idea.id),
                "title": idea.title,
                "description": idea.description,
                "status": idea.status,
                "updated_at": _iso(idea.updated_at),
            }
        )

    remaining = max_results - len(results)
    if remaining > 0:
        rows = session.execute(
            select(IdeaThread, Idea.title.label("idea_title"))
            .join(Idea, IdeaThread.idea_id == Idea.id)
            .where(
                Idea.org_id == principal.org_id,
                Idea.archived_at.is_(None),
                IdeaThread.content.ilike(pattern),
            )
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(remaining)
        ).all()
        for row in rows:
            thread = row[0]
            results.append(
                {
                    "type": "thread_message",
                    "idea_id": str(thread.idea_id),
                    "idea_title": row.idea_title,
                    "thread_message_id": thread.id,
                    "role": thread.role,
                    "content": thread.content[:600],
                    "created_at": _iso(thread.created_at),
                }
            )
    return {"query": text, "results": results}


def get_thread(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    idea_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    idea = _idea_for_org(session, idea_id=str(idea_id), org_id=principal.org_id)
    return {
        "idea": {
            "id": str(idea.id),
            "title": idea.title,
            "description": idea.description,
            "status": idea.status,
            "created_at": _iso(idea.created_at),
            "updated_at": _iso(idea.updated_at),
        },
        "messages": _thread_context(session, str(idea.id), limit=limit),
    }


def get_team_members(session: Session, principal: AgentBridgePrincipal) -> dict[str, Any]:
    rows = session.scalars(
        select(User)
        .where(User.org_id == principal.org_id, User.approved.is_(True))
        .order_by(User.name.asc(), User.email.asc())
    ).all()
    return {
        "members": [
            {
                "id": str(user.id),
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "color": user.color,
            }
            for user in rows
        ]
    }


def _compact_text(text: str | None, *, limit: int = 160) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def _notify_mentions(
    session: Session,
    *,
    org_id: str,
    idea: Idea,
    thread_message: IdeaThread,
    mentioned_user_ids: Sequence[str],
    actor_user_id: str,
    content: str,
) -> list[str]:
    notified: list[str] = []
    for user_id in dict.fromkeys(str(uid) for uid in mentioned_user_ids if uid):
        if user_id == str(actor_user_id):
            continue
        user = session.get(User, user_id)
        if user is None or str(user.org_id) != str(org_id):
            continue
        session.add(
            UserMention(
                user_id=user_id,
                idea_id=str(idea.id),
                mentioned_by=str(actor_user_id),
                thread_message_id=thread_message.id,
            )
        )
        coalesce_key = f"workspace:external_agent_share:{user_id}:{idea.id}:{thread_message.id}"
        title = f"{principalish_user_name(session, actor_user_id)} shared a personal-agent thread with you"
        body = _compact_text(content)
        payload = {
            "preview": _compact_text(content),
            "idea_title": idea.title,
            "thread_message_id": thread_message.id,
        }
        existing = session.scalars(
            select(NotificationEvent)
            .where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.coalesce_key == coalesce_key,
                NotificationEvent.read_at.is_(None),
            )
            .order_by(NotificationEvent.updated_at.desc(), NotificationEvent.id.desc())
        ).first()
        if existing is not None:
            existing.org_id = str(org_id)
            existing.source = NOTIFICATION_SOURCE_WORKSPACE
            existing.kind = NOTIFICATION_KIND_WORKSPACE_MENTION
            existing.actor_user_id = str(actor_user_id)
            existing.idea_id = str(idea.id)
            existing.title = title
            existing.body = body
            existing.payload = payload
            existing.occurrence_count = max(1, int(existing.occurrence_count or 1)) + 1
            existing.updated_at = utcnow()
        else:
            session.add(
                NotificationEvent(
                    org_id=str(org_id),
                    user_id=user_id,
                    source=NOTIFICATION_SOURCE_WORKSPACE,
                    kind=NOTIFICATION_KIND_WORKSPACE_MENTION,
                    actor_user_id=str(actor_user_id),
                    idea_id=str(idea.id),
                    title=title,
                    body=body,
                    payload=payload,
                    coalesce_key=coalesce_key,
                    occurrence_count=1,
                    updated_at=utcnow(),
                )
            )
        notified.append(user_id)
    return notified


def principalish_user_name(session: Session, user_id: str) -> str:
    user = session.get(User, str(user_id))
    return user.name if user is not None and user.name else "Someone"


def request_source_context(
    principal: AgentBridgePrincipal,
    *,
    surface: str,
    visibility: str,
    permission: str,
    tool_name: str | None = None,
) -> dict[str, Any]:
    context = {
        "surface": surface,
        "acting_user_id": principal.owner_user_id,
        "personal_agent": principal.connection_display_name,
        "personal_agent_kind": principal.agent_kind,
        "connection_id": principal.connection_id,
        "visibility": visibility,
        "permission": permission,
    }
    if tool_name:
        context["tool"] = tool_name
    return context


def create_thread_from_agent(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    title: str,
    body: str,
    teammate_user_ids: Sequence[str] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    trigger_illo: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Idea, IdeaThread, list[str]]:
    _ensure_org_user(session, org_id=principal.org_id, user_id=principal.owner_user_id)
    idea = Idea(
        title=str(title or "Shared from personal agent").strip(),
        description=None,
        status="active" if trigger_illo else "emerged",
        origin="external_agent_share",
        origin_ref=f"external_agent:{principal.connection_id}",
        user_id=principal.owner_user_id,
        org_id=principal.org_id,
        agent_details={
            "external_agent_connection_id": principal.connection_id,
            "external_agent_display_name": principal.connection_display_name,
            "external_agent_kind": principal.agent_kind,
        },
    )
    session.add(idea)
    session.flush()
    thread = IdeaThread(
        idea_id=str(idea.id),
        role="user",
        content=str(body or ""),
        user_id=principal.owner_user_id,
        attachments=list(artifacts or []),
        message_type="trigger" if trigger_illo else "agent_share",
        metadata_={
            "external_agent_connection_id": principal.connection_id,
            "external_agent_display_name": principal.connection_display_name,
            "trigger_illo": bool(trigger_illo),
            **dict(metadata or {}),
        },
    )
    session.add(thread)
    session.flush()
    notified = _notify_mentions(
        session,
        org_id=principal.org_id,
        idea=idea,
        thread_message=thread,
        mentioned_user_ids=teammate_user_ids or [],
        actor_user_id=principal.owner_user_id,
        content=body,
    )
    session.flush()
    return idea, thread, notified


def post_thread_message_from_agent(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    idea_id: str,
    body: str,
    teammate_user_ids: Sequence[str] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    trigger_illo: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Idea, IdeaThread, list[str]]:
    idea = _idea_for_org(session, idea_id=str(idea_id), org_id=principal.org_id)
    thread = IdeaThread(
        idea_id=str(idea.id),
        role="user",
        content=str(body or ""),
        user_id=principal.owner_user_id,
        attachments=list(artifacts or []),
        message_type="trigger" if trigger_illo else "agent_share",
        metadata_={
            "external_agent_connection_id": principal.connection_id,
            "external_agent_display_name": principal.connection_display_name,
            "trigger_illo": bool(trigger_illo),
            **dict(metadata or {}),
        },
    )
    session.add(thread)
    previous = idea.status
    if previous in {"needs_input", "unread_reply", "emerged"}:
        idea.status = "active"
        idea.updated_at = utcnow()
        session.add(
            IdeaStateLog(
                idea_id=str(idea.id),
                from_state=previous,
                to_state="active",
                trigger="external_agent_thread_message",
            )
        )
    session.flush()
    notified = _notify_mentions(
        session,
        org_id=principal.org_id,
        idea=idea,
        thread_message=thread,
        mentioned_user_ids=teammate_user_ids or [],
        actor_user_id=principal.owner_user_id,
        content=body,
    )
    session.flush()
    return idea, thread, notified


def _headless_prompt(question: str, context: Mapping[str, Any] | None) -> str:
    prompt = str(question or "").strip()
    if not context:
        return prompt
    return (
        "A connected personal agent is asking Illo for private workspace context. "
        "Answer concisely with relevant context and sources when possible.\n\n"
        f"Question:\n{prompt}\n\n"
        f"Provided context:\n{dict(context)}"
    )


def _dialect_name(session: Session) -> str:
    bind = session.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


def _append_run_event(session: Session, event: AgentRunEvent) -> AgentRunEventRow:
    if _dialect_name(session) == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:run_id)"), {"run_id": int(event.run_id)})
    sequence_no = event.sequence_no
    if sequence_no is None:
        sequence_no = int(
            session.scalar(
                select(func.coalesce(func.max(AgentRunEventRow.sequence_no), 0)).where(
                    AgentRunEventRow.run_id == int(event.run_id)
                )
            )
            or 0
        ) + 1
    row = AgentRunEventRow(
        run_id=int(event.run_id),
        root_run_id=event.root_run_id or event.run_id,
        sequence_no=sequence_no,
        event_type=event.event_type,
        payload=dict(event.payload or {}),
        producer=event.producer,
        visibility=event.visibility.value if isinstance(event.visibility, EventVisibility) else str(event.visibility),
    )
    session.add(row)
    session.flush()
    return row


def _create_agent_run(session: Session, request: AgentRunRequest) -> AgentRunRow:
    profile = request.normalized_profile
    recipe = request.normalized_recipe
    row = AgentRunRow(
        org_id=request.org_id,
        user_id=request.user_id,
        thread_id=request.thread_id,
        parent_run_id=request.parent_run_id,
        root_run_id=request.root_run_id,
        profile=profile.value,
        recipe=recipe.value,
        status=RunStatus.QUEUED.value,
        input_message=request.message,
        target_ref=dict(request.target_ref or {}),
        workspace_ref=dict(request.workspace_ref or {}),
        model_policy=dict(request.model_policy or {}),
        metadata_=dict(request.metadata or {}),
    )
    session.add(row)
    session.flush()
    row.trace_id = trace_id_for_run_id(row.id)
    if row.root_run_id is None:
        row.root_run_id = row.id
    session.flush()
    _append_run_event(
        session,
        run_event(
            int(row.id),
            "run.created",
            {"profile": profile.value, "recipe": recipe.value},
            root_run_id=int(row.root_run_id),
        ),
    )
    return row


def _latest_run_artifact_text(session: Session, run_id: int) -> str:
    row = session.scalars(
        select(AgentRunArtifactRow)
        .where(
            AgentRunArtifactRow.run_id == int(run_id),
            AgentRunArtifactRow.artifact_type == "final_answer",
        )
        .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
        .limit(1)
    ).first()
    return str(getattr(row, "text", None) or "") if row is not None else ""


def create_headless_ask(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    question: str,
    context: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentTaskRow:
    task_id = str(uuid.uuid4())
    metadata = dict(metadata or {})
    metadata.setdefault(
        "request_source",
        request_source_context(
            principal,
            surface="mcp_personal_agent" if metadata.get("mcp_tool") else "personal_agent_bridge",
            visibility="headless_private",
            permission="private_workspace_context",
            tool_name=str(metadata.get("mcp_tool") or "") or None,
        ),
    )
    task = ExternalAgentTaskRow(
        id=task_id,
        org_id=principal.org_id,
        connection_id=principal.connection_id,
        created_by_user_id=principal.owner_user_id,
        source_surface="bridge_ask_illo",
        title=_compact_text(question, limit=120) or "Ask Illo",
        instructions=str(question),
        input_parts=[{"type": "ask_illo", "question": str(question), "context": dict(context or {})}],
        status="queued",
        idempotency_key=f"ask:{task_id}",
        metadata_={**metadata, "headless": True},
    )
    session.add(task)
    session.flush()
    run = _create_agent_run(
        session,
        AgentRunRequest(
            org_id=principal.org_id,
            user_id=principal.owner_user_id,
            thread_id=f"external-agent:{principal.connection_id}:{task_id}",
            profile=RunProfile.FAST,
            recipe=RunRecipe.FAST,
            message=_headless_prompt(question, context),
            target_ref={
                "kind": "external_agent_headless_ask",
                "external_agent_connection_id": principal.connection_id,
                "external_agent_task_id": task_id,
            },
            workspace_ref={"source": "external_agent_bridge", "mode": "headless"},
            model_policy={"tier": "standard", "thinking": "medium"},
            metadata={
                **metadata,
                "origin": "external_agent_headless_ask",
                "external_agent_connection_id": principal.connection_id,
                "external_agent_task_id": task_id,
                "headless": True,
                "tool_policy": {
                    "mode": "read_mostly",
                    "blocked_tools": list(HEADLESS_ASK_BLOCKED_TOOLS),
                },
            },
        ),
    )
    task.illo_run_id = int(run.id)
    task.status = "submitted"
    task.submitted_at = utcnow()
    append_task_event(
        session,
        task,
        event_type="external_task.ask_illo_submitted",
        status=task.status,
        message="Headless Illo ask queued",
        payload={"run_id": int(run.id)},
        producer="illo",
    )
    session.flush()
    return task


def get_headless_ask(
    session: Session,
    principal: AgentBridgePrincipal,
    *,
    ask_id: str,
) -> dict[str, Any]:
    task = _require_task_for_principal(session, principal, ask_id)
    run: AgentRunRow | None = session.get(AgentRunRow, int(task.illo_run_id)) if task.illo_run_id else None
    answer = ""
    run_status = None
    if run is not None:
        run_status = coerce_run_status(run.status)
        answer = _latest_run_artifact_text(session, int(run.id))
        if run_status in TERMINAL_RUN_STATUSES:
            if run_status == RunStatus.COMPLETED:
                task.status = "completed"
                task.result_summary = answer
                task.completed_at = task.completed_at or utcnow()
            elif run_status in {RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED}:
                task.status = "failed" if run_status == RunStatus.FAILED else "cancelled"
                task.error = task.error or f"Illo ask ended with status {run_status.value}"
                task.failed_at = task.failed_at or utcnow()
            session.flush()
    return {
        "ask": serialize_task(task, include_events=True, session=session),
        "run": {
            "id": int(run.id) if run is not None else None,
            "status": run_status.value if run_status is not None else None,
            "thread_id": run.thread_id if run is not None else None,
        },
        "answer": answer,
    }


__all__ = [
    "DEFAULT_BRIDGE_SCOPES",
    "SCOPE_ARTIFACT_WRITE",
    "SCOPE_CONNECTION_HEARTBEAT",
    "SCOPE_ILLO_ASK",
    "SCOPE_ILLO_THREAD_CREATE",
    "SCOPE_ILLO_THREAD_WRITE",
    "SCOPE_TASK_CLAIM",
    "SCOPE_TASK_COMPLETE",
    "SCOPE_TASK_UPDATE",
    "SCOPE_WORKSPACE_READ",
    "AgentBridgePrincipal",
    "CONNECTION_ADMIN_ROLES",
    "ExternalAgentAuthError",
    "ExternalAgentError",
    "ExternalAgentNotFound",
    "ExternalAgentPermissionError",
    "append_artifact",
    "authenticate_bridge_token",
    "claim_tasks",
    "complete_task",
    "create_connection",
    "create_external_task_for_idea",
    "create_headless_ask",
    "create_thread_from_agent",
    "request_source_context",
    "fail_task",
    "generate_connection_token",
    "get_headless_ask",
    "get_team_members",
    "get_thread",
    "hash_connection_token",
    "list_connections",
    "mint_connection_token",
    "post_thread_message_from_agent",
    "record_heartbeat",
    "require_connection",
    "require_connection_for_user",
    "require_idea_for_org",
    "require_task_for_principal",
    "search_workspace",
    "serialize_artifact",
    "serialize_connection",
    "serialize_event",
    "serialize_task",
    "serialize_token",
    "serialize_thread_message",
    "token_prefix",
    "update_task_event",
]
