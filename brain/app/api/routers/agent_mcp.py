"""Hosted MCP endpoint for personal agents connecting to Illo."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.agent_bridge import (
    _bearer_token,
    _broadcast_thread_result,
    _commit_for_live_fanout,
    _run_trigger_if_requested,
    _thread_payload,
)
from brain.app.api.routers import agent_mcp_handoffs, agent_mcp_skills
from brain.app.api.routers.agent_mcp_domains import DOMAIN_TOOL_HANDLERS
from brain.app.api.routers.agent_mcp_identity import manage_identity, resolve_identities
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.app.mentions import classify_mention_intent
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.systems.cortex.thread_links import thread_id_from_reference
from brain.systems.cortex.project_context.search import search_project_contexts
from brain.systems.cycles.access import CycleActor, cycle_scope_conditions, target_idea_scope_conditions
from brain.systems.cycles.common import (
    EXTERNAL_AGENT_TRIGGERED_CYCLE_ORIGIN,
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
)
from brain.systems.cycles.contracts import normalize_cycle_run_kind
from brain.systems.cycles.commands import (
    UNSET_CYCLE_FIELD,
    async_add_guidance_to_cycle as command_add_cycle_guidance,
    async_add_output_target_to_cycle as command_add_cycle_output_target,
    async_create_cycle as command_create_cycle,
    async_delete_cycle as command_delete_cycle,
    async_remove_output_target_from_cycle as command_remove_cycle_output_target,
    async_update_cycle as command_update_cycle,
    cycle_change_event,
)
from brain.systems.cycles.events import publish_cycle_change_safe
from brain.systems.cycles.serializers import (
    serialize_cycle,
    serialize_cycle_guidance,
    serialize_cycle_output_target,
    serialize_cycle_run,
)
from brain.systems.cycles.service import async_run_cycle_now
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import admin as inbound_admin
from brain.systems.inbound.results import read_inbound_submission_result
from brain.systems.inbound.service import submit_inbound_envelope as _submit_inbound_envelope
from brain.systems.knowledge.search import search_knowledge
from brain.systems.runs.cortex.read_models import (
    public_failed_run_artifact,
    public_failure_for_run,
    public_run_debug_event_payload,
    run_stream_payload,
)
from brain.systems.runs.failures import public_run_failure
from brain.systems.runs.tool_event_read_model import tool_call_summary


router = APIRouter(tags=["agent-mcp"], dependencies=[Depends(rate_limit)])

SUBMIT_TOOL_NAME = "illo_submit"
READ_TOOL_NAME = "illo_read"
ACT_TOOL_NAME = "illo_act"
RESULT_TOOL_NAME = "illo_get_result"


ToolHandler = Callable[
    [AsyncSession, external_agents.AgentBridgePrincipal, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _tool_schema(description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


MCP_TOOLS: dict[str, dict[str, Any]] = {
    SUBMIT_TOOL_NAME: {
        **_tool_schema(
            (
                "Submit instructions, context, traces, decisions, or work material to Illo. "
                "Use this when the request needs Illo's judgment, memory, or coordination. "
                "Use it for preserve/store/remember requests so Illo can choose the durable memory or workspace surface. "
                "The call is async-first: Illo stores an inbound event, queues headless handling, "
                f"and returns an event id that can be read with {RESULT_TOOL_NAME}."
            ),
            {
                "message": {
                    "type": "string",
                    "description": "Natural-language instruction or context for Illo to handle.",
                },
                "desired_outcome": {
                    "type": "string",
                    "description": "Explicit outcome request, such as preserve_knowledge when Illo should create durable memory or workspace state.",
                },
                "origin": {
                    "type": "string",
                    "description": "Stable source event name, for example codex.submit or slack.request.",
                },
                "parts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered context parts such as text, json, link, file, trace, conversation, diff, screenshot, or artifact.",
                    "default": [],
                },
                "source": {
                    "type": "object",
                    "description": "Optional provenance about the external agent, repo, branch, model, session, or service.",
                    "default": {},
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional boundaries such as privacy, urgency, visibility, budget, or notification preferences.",
                    "default": {},
                },
                "correlation": {
                    "type": "object",
                    "description": "Optional correlation such as thread_id, thread_url, external_session_id, delivery_id, or previous submission reference.",
                    "default": {},
                },
                "response": {
                    "type": "object",
                    "description": "Optional response hints, including callback or webhook routing metadata.",
                    "default": {},
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable key so repeated submissions do not duplicate work.",
                },
                "source_tool": {
                    "type": "string",
                    "description": "External tool or service name, for example codex, slack, claude-code, or opencode.",
                    "default": "external",
                },
                "repo": {"type": "string", "description": "Repository or workspace hint."},
                "branch": {"type": "string", "description": "Branch/worktree hint."},
                "task_title": {"type": "string", "description": "Current task title or short objective."},
                "files_touched": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files or paths touched during the work session.",
                    "default": [],
                },
                "session_id": {"type": "string", "description": "Optional external-tool session id."},
                "run_id": {"type": "string", "description": "Optional external-tool run id."},
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            ["message"],
        ),
        "scope": external_agents.SCOPE_SIGNAL_SUBMIT,
        "mutates_inbound": True,
    },
    READ_TOOL_NAME: {
        **_tool_schema(
            (
                "Read deterministic Illo workspace information through a named capability. "
                "Use this for direct lookup and search; use illo_submit when the request needs "
                "Illo's interpretation or decision."
            ),
            {
                "capability": {
                    "type": "string",
                    "description": (
                        "Read capability name, such as workspace.search, project_contexts.search, "
                        "thread.get, skills.get, skills.list, handoff.get, team.members.list, "
                        "domain.inspect, or capabilities."
                    ),
                },
                "arguments": {
                    "type": "object",
                    "description": "Capability-specific arguments.",
                    "default": {},
                },
            },
            ["capability"],
        ),
        "scope": external_agents.SCOPE_WORKSPACE_READ,
    },
    ACT_TOOL_NAME: {
        **_tool_schema(
            (
                "Execute a deterministic external-agent action as the user's delegate through "
                "a named capability. Use illo_submit when the action should be decided by Illo."
            ),
            {
                "capability": {
                    "type": "string",
                    "description": "Action capability name, such as thread.create, thread.post_message, thread.artifact.publish, handoff.create, domain.record.write, or capabilities.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Capability-specific arguments.",
                    "default": {},
                },
                "reason": {
                    "type": "string",
                    "description": "Optional natural-language reason for audit/provenance.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable key supplied by the external agent.",
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            ["capability"],
        ),
        "scope": external_agents.SCOPE_ILLO_ACT,
    },
    RESULT_TOOL_NAME: {
        **_tool_schema(
            (
                "Read the current status and receipts for an async Illo submission. "
                "For preservation requests, returns whether durable evidence is pending, satisfied, or missing. "
                "Prefer webhook callbacks when configured; this tool is the polling fallback."
            ),
            {
                "event_id": {"type": "string", "description": "Inbound event id returned by illo_submit."},
                "submission_id": {"type": "string", "description": "Alias for event_id."},
                "result_id": {"type": "string", "description": "Alias for event_id."},
                "include_payload": {
                    "type": "boolean",
                    "description": "Whether to include stored raw and normalized event payloads.",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum decision receipts to return.",
                    "default": 25,
                },
            },
        ),
        "scope": external_agents.SCOPE_SIGNAL_SUBMIT,
    },
}


def _has_scope(principal: external_agents.AgentBridgePrincipal, scope: str | None) -> bool:
    return not scope or "*" in principal.scopes or scope in principal.scopes


def _clean_string_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _tool_metadata(arguments: dict[str, Any], *, tool_name: str, trigger_illo: bool | None = None) -> dict[str, Any]:
    metadata = {**_clean_dict(arguments.get("metadata")), "mcp_tool": tool_name}
    if trigger_illo is not None:
        metadata["trigger_illo"] = trigger_illo
    return metadata


_FORBIDDEN_SUBMIT_TARGET_FIELDS = frozenset(
    {
        "idea_id",
        "thread_id",
        "target_id",
        "target",
        "project_id",
        "pin_id",
        "teammate_user_ids",
        "trigger_illo",
    }
)


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bounded_limit(value: Any, *, default: int = 25, maximum: int = 100) -> int:
    try:
        return max(1, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _submission_source(arguments: dict[str, Any]) -> dict[str, Any]:
    source = _clean_dict(arguments.get("source"))
    for key in ("source_tool", "repo", "branch", "task_title", "session_id", "run_id"):
        value = _clean_optional_string(arguments.get(key))
        if value:
            source.setdefault(key, value)
    files_touched = _clean_string_list(arguments.get("files_touched"))
    if files_touched:
        source.setdefault("files_touched", files_touched)
    return source


def _clean_submit_parts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{SUBMIT_TOOL_NAME} parts must be an array")
    parts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            parts.append(dict(item))
        elif str(item or "").strip():
            parts.append({"type": "text", "text": str(item)})
    return parts


def _build_submit_envelope(arguments: dict[str, Any]) -> dict[str, Any]:
    forbidden = sorted(_FORBIDDEN_SUBMIT_TARGET_FIELDS.intersection(arguments))
    if forbidden:
        raise ValueError(
            f"{SUBMIT_TOOL_NAME} accepts instructions, context, and optional correlation, not direct workspace targets. "
            f"Move Thread ids under correlation and remove top-level field(s): {', '.join(forbidden)}"
        )
    message = _clean_optional_string(arguments.get("message"))
    if not message:
        raise ValueError(f"{SUBMIT_TOOL_NAME} requires a non-empty message")
    source = _submission_source(arguments)
    source_tool = _clean_optional_string(source.get("source_tool")) or "external"
    source.setdefault("source_tool", source_tool)
    origin = _clean_optional_string(arguments.get("origin")) or f"{source_tool}.submit"
    desired_outcome = _clean_optional_string(arguments.get("desired_outcome"))
    constraints = _clean_dict(arguments.get("constraints"))
    correlation = _clean_dict(arguments.get("correlation"))
    response = _clean_dict(arguments.get("response"))
    parts = _clean_submit_parts(arguments.get("parts"))
    payload = {
        "message": message,
        "parts": parts,
        "source": source,
        "constraints": constraints,
        "correlation": correlation,
        "response": response,
    }
    envelope = {
        "kind": "submission",
        "origin": origin,
        "payload": payload,
        "summary": message,
        "message": message,
        "parts": parts,
        "source": source,
        "constraints": constraints,
        "correlation": correlation,
        "response": response,
        "idempotency_key": _clean_optional_string(arguments.get("idempotency_key")),
    }
    if desired_outcome:
        envelope["desired_outcome"] = desired_outcome
    return envelope


def _submission_connection(principal: external_agents.AgentBridgePrincipal) -> dict[str, Any]:
    return {
        "id": principal.connection_id,
        "org_id": principal.org_id,
        "owner_user_id": principal.owner_user_id,
        "token_id": principal.token_id,
        "display_name": principal.connection_display_name,
        "agent_kind": principal.agent_kind,
        "source_type": "personal_tool",
        "capabilities": [SUBMIT_TOOL_NAME, READ_TOOL_NAME, ACT_TOOL_NAME, RESULT_TOOL_NAME],
    }


def _external_source_actor(principal: external_agents.AgentBridgePrincipal) -> dict[str, Any]:
    return {
        "kind": "external_source_connection",
        "connection_id": principal.connection_id,
        "display_name": principal.connection_display_name,
        "agent_kind": principal.agent_kind,
    }


def _submission_ingress_context(
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return {
        "surface": "mcp_personal_tool",
        "tool_name": SUBMIT_TOOL_NAME,
        "source_actor": _external_source_actor(principal),
        "authority_principal": {
            "kind": "user",
            "user_id": principal.owner_user_id,
            "org_id": principal.org_id,
        },
        "auth": {
            "token_id": principal.token_id,
            "scopes": sorted(principal.scopes),
        },
        "metadata": _tool_metadata(arguments, tool_name=SUBMIT_TOOL_NAME),
    }


async def submit_inbound_envelope(
    db: AsyncSession,
    *,
    connection: dict[str, Any],
    envelope: dict[str, Any],
    ingress_context: dict[str, Any],
) -> dict[str, Any]:
    """Adapter to the shared inbound service, kept patchable for route tests."""

    return await _submit_inbound_envelope(
        db,
        connection=connection,
        envelope=envelope,
        ingress_context=ingress_context,
    )


async def _authenticate_mcp_principal(
    request: Request,
    db: AsyncSession,
) -> external_agents.AgentBridgePrincipal:
    return await external_agents.authenticate_bridge_token(
        db,
        _bearer_token(request, request.headers.get("X-Illo-Bridge-Token")),
    )


def _list_tools(principal: external_agents.AgentBridgePrincipal) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name, spec in MCP_TOOLS.items():
        if not _has_scope(principal, spec.get("scope")):
            continue
        tools.append(
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
        )
    return tools


def _submit_tool_response(result: dict[str, Any]) -> dict[str, Any]:
    outcome = dict(result.get("ilo_outcome") or {})
    handling = dict(outcome.get("handling") or {})
    event_id = str(result.get("event_id") or "")
    return {
        **result,
        "submission_id": event_id or None,
        "result_id": event_id or None,
        "operation": outcome.get("operation"),
        "message": outcome.get("message"),
        "handling": handling or None,
        "run_id": handling.get("run_id"),
        "handling_status": handling.get("status"),
    }


def _thread_argument_id(arguments: dict[str, Any]) -> str:
    for key in ("thread_url", "url", "thread_route", "idea_id", "thread_id"):
        thread_id = thread_id_from_reference(arguments.get(key), allow_raw_id=key in {"idea_id", "thread_id"})
        if thread_id:
            return thread_id
    return str(arguments.get("idea_id") or arguments.get("thread_id") or "")


async def _tool_submit(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    envelope = _build_submit_envelope(arguments)
    result = await submit_inbound_envelope(
        db,
        connection=_submission_connection(principal),
        envelope=envelope,
        ingress_context=_submission_ingress_context(principal, arguments),
    )
    return _submit_tool_response(result)


READ_CAPABILITIES: dict[str, dict[str, Any]] = {
    "knowledge.search": {
        "description": "Search the source-backed Illo Knowledge index with hybrid recall and canonical provenance.",
        "arguments": {
            "query": "string",
            "sources": "string[]",
            "kinds": "string[]",
            "limit": "integer",
        },
    },
    "workspace.search": {
        "description": "Search the Illo workspace for related Project Contexts, ideas, threads, and shared work.",
        "arguments": {"query": "string", "limit": "integer"},
    },
    "project_contexts.search": {
        "description": "Search reusable Project Context profiles and attached project context resources visible to the bridge user.",
        "arguments": {
            "query": "string",
            "limit": "integer",
            "include_inactive": "boolean",
        },
    },
    "thread.get": {
        "description": "Read messages from an existing Illo idea/thread.",
        "arguments": {"idea_id": "string", "limit": "integer"},
    },
    "run.get": {
        "description": "Read a visible Illo run, including bounded tool-call evidence, events, and artifacts.",
        "arguments": {
            "run_id": "integer",
            "include_tool_events": "boolean",
            "include_events": "boolean",
            "include_artifacts": "boolean",
            "event_types": "string[]",
            "limit": "integer",
        },
    },
    "cycles.inspect": {
        "description": "List visible Illo Cycles or inspect one Cycle with optional recent run history.",
        "arguments": {
            "cycle_id": "integer",
            "include_runs": "boolean",
            "limit": "integer",
        },
    },
    **agent_mcp_skills.READ_CAPABILITIES,
    **agent_mcp_handoffs.READ_CAPABILITIES,
    "team.members.list": {
        "description": "List visible Illo team members.",
        "arguments": {},
    },
    "identity.resolve": {
        "description": "Resolve Illospace team members to provider-neutral external identities such as Slack, GitHub, Codex, or MCP connections.",
        "arguments": {
            "provider": "string",
            "external_user_id": "string",
            "user_id": "string",
            "query": "string",
            "limit": "integer",
        },
    },
    "domain.inspect": {
        "description": "Inspect Illo Domains, schemas, records, relations, and recent domain events.",
        "arguments": {
            "domain_id": "integer",
            "slug": "string",
            "object_key": "string",
            "record_id": "integer",
            "search": "string",
            "format": "string",
            "fields": "string[]",
            "order": "string",
            "filters": "object",
            "include_records": "boolean",
            "include_relations": "boolean",
            "include_events": "boolean",
            "limit": "integer",
        },
    },
}


ACT_CAPABILITIES: dict[str, dict[str, Any]] = {
    "thread.create": {
        "description": "Create a visible Illo thread as the user's external-agent delegate.",
        "arguments": {
            "title": "string",
            "body": "string",
            "teammate_user_ids": "string[]",
            "artifacts": "object[]",
            "trigger_illo": "boolean",
        },
    },
    "thread.post_message": {
        "description": "Post a visible message into an existing Illo thread as the user's external-agent delegate.",
        "arguments": {
            "idea_id": "string",
            "thread_id": "string",
            "thread_url": "string",
            "body": "string",
            "teammate_user_ids": "string[]",
            "artifacts": "object[]",
            "trigger_illo": "boolean",
        },
    },
    "thread.artifact.publish": {
        "description": "Publish or republish an interactive HTML artifact app scoped to an existing Illo thread.",
        "arguments": {
            "idea_id": "string",
            "thread_id": "string",
            "thread_url": "string",
            "thread_route": "string",
            "title": "string",
            "description": "string",
            "artifact_kind": "string",
            "source_code": "string",
            "app_id": "string",
            "key": "string",
            "update_existing": "boolean",
            "manifest": "object",
            "visual_spec": "object",
            "metadata": "object",
            "initial_state": "object",
        },
    },
    "thread.collaboration.start": {
        "description": (
            "Start or update a reusable team collaboration board for a thread. "
            "Use this for brainstorms, votes, async team opinions, decision rooms, and status collection "
            "instead of generating throwaway HTML apps."
        ),
        "arguments": {
            "idea_id": "string",
            "thread_id": "string",
            "title": "string",
            "prompt": "string",
            "mode": "string",
            "options": "object[]",
            "session_key": "string",
            "metadata": "object",
        },
    },
    **agent_mcp_handoffs.ACT_CAPABILITIES,
    "domain.record.write": {
        "description": "Create, update, or archive records in Illo Domains as the user's external-agent delegate.",
        "arguments": {
            "action": "create_record | update_record | archive_record",
            "domain_id": "integer",
            "slug": "string",
            "object_key": "string",
            "record_id": "integer",
            "data": "object",
            "data_patch": "object",
            "title": "string",
            "expected_version": "integer",
            "reason": "string",
        },
    },
    "cycle.manage": {
        "description": "Create, update, delete, run, or adjust reusable Illo Cycles as the user's external-agent delegate.",
        "arguments": {
            "action": "create | update | delete | run | add_guidance | add_output_target | remove_output_target",
            "cycle_id": "integer",
            "name": "string",
            "prompt": "string",
            "schedule_expr": "string",
            "run_at": "string",
            "timezone": "string",
            "enabled": "boolean",
            "model_override": "string",
            "thinking_override": "string",
            "execution_policy_key": "string | null",
            "target_idea_id": "string",
            "guidance": "string",
            "target_type": "string",
            "target_id": "string",
            "label": "string",
            "config": "object",
            "output_target_id": "integer",
            "rationale": "string",
            "run_kind": "scheduled_digest | off_slot_material_alert",
        },
    },
    "identity.manage": {
        "description": "Link or unlink a provider-neutral external identity on an existing external source connection.",
        "arguments": {
            "action": "link | unlink",
            "connection_id": "string",
            "provider": "string",
            "external_user_id": "string",
            "user_id": "string",
            "display_name": "string",
            "metadata": "object",
        },
    },
}


def _capability_catalog_payload(catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "capabilities": [
            {"name": name, **details}
            for name, details in sorted(catalog.items())
        ]
    }


def _capability_name(arguments: dict[str, Any], *, tool_name: str) -> str:
    capability = _clean_optional_string(arguments.get("capability"))
    if not capability:
        raise ValueError(f"{tool_name} requires a non-empty capability")
    return capability


def _capability_arguments(arguments: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    value = arguments.get("arguments")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{tool_name} arguments must be an object")
    return dict(value)


def _required_capability_string(arguments: dict[str, Any], key: str, *, capability: str) -> str:
    value = _clean_optional_string(arguments.get(key))
    if not value:
        raise ValueError(f"{capability} requires a non-empty {key}")
    return value


def _cycle_actor(principal: external_agents.AgentBridgePrincipal) -> CycleActor:
    return CycleActor(
        user_id=principal.owner_user_id,
        org_id=principal.org_id,
        principal_type="external_agent",
        source_id=principal.connection_id,
    )


async def _get_cycle_for_principal(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    cycle_id: int,
) -> Cycle:
    stmt = select(Cycle).where(Cycle.id == cycle_id, *cycle_scope_conditions(_cycle_actor(principal)))
    cycle = (await db.scalars(stmt)).first()
    if cycle is None:
        raise ValueError("Cycle not found")
    return cycle


async def _validate_cycle_target_idea(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    idea_id: str | None,
) -> None:
    if not idea_id:
        return
    stmt = select(Idea.id).where(*target_idea_scope_conditions(idea_id, _cycle_actor(principal)))
    if (await db.execute(stmt)).first() is None:
        raise ValueError("target_idea_id must belong to the current workspace")


def _serialize_run_event(
    event: AgentRunEventRow,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": event.id,
        "run_id": event.run_id,
        "root_run_id": event.root_run_id,
        "sequence_no": event.sequence_no,
        "event_type": event.event_type,
        "payload": public_run_debug_event_payload(event, failure),
        "producer": event.producer,
        "visibility": event.visibility,
        "created_at": _iso(event.created_at),
    }


def _serialize_run_artifact(
    artifact: AgentRunArtifactRow,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    return public_failed_run_artifact({
        "id": artifact.id,
        "run_id": artifact.run_id,
        "root_run_id": artifact.root_run_id,
        "artifact_type": artifact.artifact_type,
        "title": artifact.title,
        "payload": artifact.payload or {},
        "text": artifact.text,
        "uri": artifact.uri,
        "visibility": artifact.visibility,
        "created_at": _iso(artifact.created_at),
    }, failure)


async def _read_run_get(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    run_id = _clean_optional_int(arguments.get("run_id"))
    if run_id is None:
        raise ValueError("run.get requires run_id")
    run = await db.get(AgentRunRow, run_id)
    if run is None or str(run.org_id or "") != str(principal.org_id):
        raise ValueError("Run not found")

    failure_events = list(
        (
            await db.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id == run.id,
                    AgentRunEventRow.event_type == "run.failed",
                )
                .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
                .limit(1)
            )
        ).all()
    )
    failure = public_failure_for_run(run, failure_events)
    limit = _bounded_limit(arguments.get("limit"), default=50, maximum=200)
    run_payload = run_stream_payload(run)
    if failure is not None:
        run_payload["failure"] = failure
    payload: dict[str, Any] = {"run": run_payload}
    event_types = _clean_string_list(arguments.get("event_types"))
    include_events = bool(arguments.get("include_events", False))
    include_tool_events = bool(arguments.get("include_tool_events", True))
    if include_events:
        event_stmt = (
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == run.id)
            .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            .limit(limit)
        )
        if event_types:
            event_stmt = event_stmt.where(AgentRunEventRow.event_type.in_(event_types))
        payload["events"] = [
            _serialize_run_event(event, failure)
            for event in (await db.scalars(event_stmt)).all()
        ]
    if include_tool_events:
        tool_stmt = (
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == run.id,
                AgentRunEventRow.event_type.like("run.tool_%"),
            )
            .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            .limit(limit)
        )
        payload["tool_events"] = [
            _serialize_run_event(event, failure)
            for event in (await db.scalars(tool_stmt)).all()
        ]
        payload["tool_call_summary"] = await tool_call_summary(db, run_id)
    if bool(arguments.get("include_artifacts", True)):
        artifact_stmt = (
            select(AgentRunArtifactRow)
            .where(AgentRunArtifactRow.run_id == run.id)
            .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            .limit(limit)
        )
        payload["artifacts"] = [
            _serialize_run_artifact(artifact, failure)
            for artifact in (await db.scalars(artifact_stmt)).all()
        ]
    return payload


async def _read_cycles_inspect(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    limit = _bounded_limit(arguments.get("limit"), default=25, maximum=100)
    cycle_id = _clean_optional_int(arguments.get("cycle_id"))
    if cycle_id is None:
        stmt = (
            select(Cycle)
            .where(*cycle_scope_conditions(_cycle_actor(principal)))
            .order_by(Cycle.created_at.desc(), Cycle.id.desc())
            .limit(limit)
        )
        return {"cycles": [serialize_cycle(cycle) for cycle in (await db.scalars(stmt)).all()]}

    cycle = await _get_cycle_for_principal(db, principal, cycle_id)
    payload = {"cycle": serialize_cycle(cycle)}
    if bool(arguments.get("include_runs", False)):
        run_stmt = (
            select(CycleRun)
            .where(CycleRun.cycle_id == cycle.id)
            .order_by(CycleRun.created_at.desc(), CycleRun.id.desc())
            .limit(limit)
        )
        payload["runs"] = [serialize_cycle_run(run) for run in (await db.scalars(run_stmt)).all()]
    return payload


async def _tool_read(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    capability = _capability_name(arguments, tool_name=READ_TOOL_NAME)
    capability_arguments = _capability_arguments(arguments, tool_name=READ_TOOL_NAME)
    if capability == "capabilities":
        return _capability_catalog_payload(READ_CAPABILITIES)
    if capability == "workspace.search":
        return await external_agents.search_workspace(
            db,
            principal,
            query=_required_capability_string(capability_arguments, "query", capability=capability),
            limit=int(capability_arguments.get("limit") or 10),
        )
    if capability == "knowledge.search":
        return await search_knowledge(
            db,
            _required_capability_string(
                capability_arguments,
                "query",
                capability=capability,
            ),
            org_id=principal.org_id,
            sources=_clean_string_list(capability_arguments.get("sources")),
            kinds=_clean_string_list(capability_arguments.get("kinds")),
            limit=int(capability_arguments.get("limit") or 10),
        )
    if capability == "project_contexts.search":
        return await search_project_contexts(
            db,
            org_id=principal.org_id,
            user_id=principal.owner_user_id,
            query=capability_arguments.get("query"),
            limit=int(capability_arguments.get("limit") or 10),
            include_inactive=bool(capability_arguments.get("include_inactive", False)),
        )
    if capability == "thread.get":
        return await external_agents.get_thread(
            db,
            principal,
            idea_id=_thread_argument_id(capability_arguments),
            limit=int(capability_arguments.get("limit") or 100),
        )
    if capability == "run.get":
        return await _read_run_get(db, principal, capability_arguments)
    if capability == "cycles.inspect":
        return await _read_cycles_inspect(db, principal, capability_arguments)
    if capability == "skills.get":
        return await agent_mcp_skills.read_skill(db, principal, capability_arguments)
    if capability == "skills.list":
        return await agent_mcp_skills.list_skills(db, principal)
    if capability == "handoff.get":
        return await agent_mcp_handoffs.read_handoff(db, principal, capability_arguments)
    if capability == "team.members.list":
        return await external_agents.get_team_members(db, principal)
    if capability == "identity.resolve":
        return await resolve_identities(db, principal, capability_arguments)
    if capability == "domain.inspect":
        return await DOMAIN_TOOL_HANDLERS["illo_inspect_domains"](db, principal, capability_arguments)
    raise ValueError(f"Unknown {READ_TOOL_NAME} capability: {capability}")


def _action_metadata(
    arguments: dict[str, Any],
    capability_arguments: dict[str, Any],
    *,
    capability: str,
    trigger_illo: bool | None = None,
) -> dict[str, Any]:
    metadata = {
        **_clean_dict(capability_arguments.get("metadata")),
        **_clean_dict(arguments.get("metadata")),
        "mcp_tool": ACT_TOOL_NAME,
        "mcp_capability": capability,
    }
    reason = _clean_optional_string(arguments.get("reason"))
    if reason:
        metadata["reason"] = reason
    idempotency_key = _clean_optional_string(arguments.get("idempotency_key"))
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    if trigger_illo is not None:
        metadata["trigger_illo"] = trigger_illo
    return metadata


async def _act_create_thread(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    capability_arguments: dict[str, Any],
    *,
    original_arguments: dict[str, Any],
) -> dict[str, Any]:
    body = _required_capability_string(capability_arguments, "body", capability="thread.create")
    should_trigger = bool(
        capability_arguments.get("trigger_illo")
        or classify_mention_intent(body).should_invoke_illo
    )
    metadata = _action_metadata(
        original_arguments,
        capability_arguments,
        capability="thread.create",
        trigger_illo=should_trigger,
    )
    idea, message, notified = await external_agents.create_thread_from_agent(
        db,
        principal,
        title=_required_capability_string(capability_arguments, "title", capability="thread.create"),
        body=body,
        teammate_user_ids=_clean_string_list(capability_arguments.get("teammate_user_ids")),
        artifacts=_clean_artifacts(capability_arguments.get("artifacts")),
        trigger_illo=should_trigger,
        metadata=metadata,
    )
    result = _thread_payload(idea, message, notified)
    result["_mutates_thread"] = True
    result["_trigger_idea"] = idea
    result["_trigger_body"] = body
    result["_trigger_metadata"] = metadata
    return result


async def _act_post_thread_message(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    capability_arguments: dict[str, Any],
    *,
    original_arguments: dict[str, Any],
) -> dict[str, Any]:
    body = _required_capability_string(capability_arguments, "body", capability="thread.post_message")
    should_trigger = bool(
        capability_arguments.get("trigger_illo")
        or classify_mention_intent(body).should_invoke_illo
    )
    metadata = _action_metadata(
        original_arguments,
        capability_arguments,
        capability="thread.post_message",
        trigger_illo=should_trigger,
    )
    idea, message, notified = await external_agents.post_thread_message_from_agent(
        db,
        principal,
        idea_id=_thread_argument_id(capability_arguments),
        body=body,
        teammate_user_ids=_clean_string_list(capability_arguments.get("teammate_user_ids")),
        artifacts=_clean_artifacts(capability_arguments.get("artifacts")),
        trigger_illo=should_trigger,
        metadata=metadata,
    )
    result = _thread_payload(idea, message, notified)
    result["_mutates_thread"] = True
    result["_trigger_idea"] = idea
    result["_trigger_body"] = body
    result["_trigger_metadata"] = metadata
    return result


async def _act_publish_thread_artifact(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    capability_arguments: dict[str, Any],
) -> dict[str, Any]:
    from brain.systems.cortex.thread_artifacts import publish_thread_artifact_app

    result = await publish_thread_artifact_app(
        db,
        org_id=principal.org_id,
        user_id=principal.owner_user_id,
        thread_id=_thread_argument_id(capability_arguments),
        title=_required_capability_string(capability_arguments, "title", capability="thread.artifact.publish"),
        description=_clean_optional_string(capability_arguments.get("description")),
        artifact_kind=_clean_optional_string(capability_arguments.get("artifact_kind")),
        source_code=_required_capability_string(capability_arguments, "source_code", capability="thread.artifact.publish"),
        app_id=_clean_optional_string(capability_arguments.get("app_id")),
        key=_clean_optional_string(capability_arguments.get("key")),
        update_existing=bool(capability_arguments.get("update_existing", True)),
        manifest=_clean_dict(capability_arguments.get("manifest")),
        visual_spec=_clean_dict(capability_arguments.get("visual_spec")),
        metadata={
            **_clean_dict(capability_arguments.get("metadata")),
            "mcp_tool": ACT_TOOL_NAME,
            "mcp_capability": "thread.artifact.publish",
        },
        initial_state=_clean_dict(capability_arguments.get("initial_state")),
    )
    result["_mutates_workspace_app"] = True
    result["_workspace_app_change"] = {"action": result["action"], "app": result["app"]}
    return result


async def _act_start_thread_collaboration(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    capability_arguments: dict[str, Any],
) -> dict[str, Any]:
    from brain.systems.cortex.thread_artifacts import publish_thread_collaboration_app

    result = await publish_thread_collaboration_app(
        db,
        org_id=principal.org_id,
        user_id=principal.owner_user_id,
        thread_id=_thread_argument_id(capability_arguments),
        title=_required_capability_string(capability_arguments, "title", capability="thread.collaboration.start"),
        prompt=_required_capability_string(capability_arguments, "prompt", capability="thread.collaboration.start"),
        mode=_clean_optional_string(capability_arguments.get("mode")),
        options=capability_arguments.get("options") if isinstance(capability_arguments.get("options"), list) else [],
        session_key=_clean_optional_string(capability_arguments.get("session_key")),
        metadata={
            **_clean_dict(capability_arguments.get("metadata")),
            "mcp_tool": ACT_TOOL_NAME,
            "mcp_capability": "thread.collaboration.start",
        },
    )
    result["_mutates_workspace_app"] = True
    result["_workspace_app_change"] = {"action": result["action"], "app": result["app"]}
    return result


def _cycle_mutation_payload(action: str, cycle: Cycle, payload: dict[str, Any]) -> dict[str, Any]:
    payload["_mutates_cycle"] = True
    payload["_cycle_change"] = {"action": action, "event": cycle_change_event(cycle)}
    return payload


async def _act_manage_cycle(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    action = _required_capability_string(arguments, "action", capability="cycle.manage")
    actor = _cycle_actor(principal)
    rationale = _clean_optional_string(arguments.get("rationale"))

    if action == "create":
        target_idea_id = _clean_optional_string(arguments.get("target_idea_id"))
        await _validate_cycle_target_idea(db, principal, target_idea_id)
        cycle = await command_create_cycle(
            db,
            actor=actor,
            name=_required_capability_string(arguments, "name", capability="cycle.manage"),
            prompt=_required_capability_string(arguments, "prompt", capability="cycle.manage"),
            timezone_name=_clean_optional_string(arguments.get("timezone")) or "UTC",
            schedule_expr=_clean_optional_string(arguments.get("schedule_expr")),
            run_at=arguments.get("run_at"),
            enabled=bool(arguments.get("enabled", True)),
            model_override=_clean_optional_string(arguments.get("model_override")),
            thinking_override=_clean_optional_string(arguments.get("thinking_override")),
            execution_policy_key=_clean_optional_string(
                arguments.get("execution_policy_key")
            ),
            target_idea_id=target_idea_id,
            guidance=_clean_optional_string(arguments.get("guidance")),
            rationale=rationale,
        )
        await db.flush()
        await db.refresh(cycle)
        return _cycle_mutation_payload("create", cycle, {"cycle": serialize_cycle(cycle)})

    cycle_id = _clean_optional_int(arguments.get("cycle_id"))
    if cycle_id is None:
        raise ValueError(f"cycle.manage action '{action}' requires cycle_id")
    cycle = await _get_cycle_for_principal(db, principal, cycle_id)

    if action == "update":
        target_idea_id = arguments.get("target_idea_id", UNSET_CYCLE_FIELD)
        if target_idea_id is not UNSET_CYCLE_FIELD:
            target_idea_id = _clean_optional_string(target_idea_id)
            await _validate_cycle_target_idea(db, principal, target_idea_id)
        execution_policy_key = arguments.get(
            "execution_policy_key", UNSET_CYCLE_FIELD
        )
        if execution_policy_key is not UNSET_CYCLE_FIELD:
            execution_policy_key = _clean_optional_string(execution_policy_key)
        cycle = await command_update_cycle(
            db,
            cycle,
            actor=actor,
            name=_clean_optional_string(arguments.get("name")),
            prompt=_clean_optional_string(arguments.get("prompt")),
            timezone_name=_clean_optional_string(arguments.get("timezone")),
            schedule_expr=_clean_optional_string(arguments.get("schedule_expr")),
            run_at=arguments.get("run_at", UNSET_CYCLE_FIELD),
            enabled=arguments.get("enabled", UNSET_CYCLE_FIELD),
            model_override=arguments.get("model_override", UNSET_CYCLE_FIELD),
            thinking_override=arguments.get("thinking_override", UNSET_CYCLE_FIELD),
            execution_policy_key=execution_policy_key,
            target_idea_id=target_idea_id,
            guidance=_clean_optional_string(arguments.get("guidance")),
            rationale=rationale,
        )
        await db.flush()
        await db.refresh(cycle)
        return {
            "cycle": serialize_cycle(cycle),
            "_mutates_cycle": True,
        }

    if action == "delete":
        await command_delete_cycle(db, cycle)
        return _cycle_mutation_payload("delete", cycle, {"ok": True, "id": cycle.id})

    if action == "run":
        run_kind = normalize_cycle_run_kind(
            _clean_optional_string(arguments.get("run_kind"))
            or OFF_SLOT_MATERIAL_ALERT_RUN_KIND
        )
        result = await async_run_cycle_now(
            cycle.id,
            run_kind=run_kind,
            launch_context={
                "origin": EXTERNAL_AGENT_TRIGGERED_CYCLE_ORIGIN,
                "source": "illo_act.cycle.manage",
                "actor_type": "external_agent",
                "actor_id": principal.connection_id,
                "agent_kind": principal.agent_kind,
                "rationale": rationale,
            },
        )
        payload = {"cycle": serialize_cycle(cycle), "run": result}
        payload["_mutates_cycle"] = True
        payload["_cycle_change"] = {"action": "run", "event": cycle_change_event(cycle)}
        return payload

    if action == "add_guidance":
        guidance = await command_add_cycle_guidance(
            db,
            cycle,
            actor=actor,
            guidance=_required_capability_string(arguments, "guidance", capability="cycle.manage"),
            rationale=rationale,
        )
        return {
            "cycle": serialize_cycle(cycle),
            "guidance": serialize_cycle_guidance(guidance),
            "_mutates_cycle": True,
        }

    if action == "add_output_target":
        target = await command_add_cycle_output_target(
            db,
            cycle,
            actor=actor,
            target_type=_required_capability_string(arguments, "target_type", capability="cycle.manage"),
            target_id=_clean_optional_string(arguments.get("target_id")),
            label=_clean_optional_string(arguments.get("label")),
            config=_clean_dict(arguments.get("config")),
            rationale=rationale,
        )
        return _cycle_mutation_payload(
            "update",
            cycle,
            {"cycle": serialize_cycle(cycle), "output_target": serialize_cycle_output_target(target)},
        )

    if action == "remove_output_target":
        output_target_id = _clean_optional_int(arguments.get("output_target_id") or arguments.get("target_id"))
        if output_target_id is None:
            raise ValueError("cycle.manage action 'remove_output_target' requires output_target_id")
        target = await command_remove_cycle_output_target(
            db,
            cycle,
            actor=actor,
            target_id=output_target_id,
            rationale=rationale,
        )
        if target is None:
            raise ValueError("Cycle output target not found")
        return _cycle_mutation_payload(
            "update",
            cycle,
            {"cycle": serialize_cycle(cycle), "output_target": serialize_cycle_output_target(target)},
        )

    raise ValueError(f"Unknown cycle.manage action: {action}")


async def _tool_act(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    capability = _capability_name(arguments, tool_name=ACT_TOOL_NAME)
    capability_arguments = _capability_arguments(arguments, tool_name=ACT_TOOL_NAME)
    if capability == "capabilities":
        return _capability_catalog_payload(ACT_CAPABILITIES)
    if capability == "thread.create":
        return await _act_create_thread(
            db,
            principal,
            capability_arguments,
            original_arguments=arguments,
        )
    if capability == "thread.post_message":
        return await _act_post_thread_message(
            db,
            principal,
            capability_arguments,
            original_arguments=arguments,
        )
    if capability == "thread.artifact.publish":
        return await _act_publish_thread_artifact(db, principal, capability_arguments)
    if capability == "thread.collaboration.start":
        return await _act_start_thread_collaboration(db, principal, capability_arguments)
    if capability == "handoff.create":
        return await agent_mcp_handoffs.create_handoff(
            db,
            principal,
            capability_arguments,
            metadata=_action_metadata(arguments, capability_arguments, capability="handoff.create"),
            idempotency_key=_clean_optional_string(arguments.get("idempotency_key")),
        )
    if capability == "domain.record.write":
        result = await DOMAIN_TOOL_HANDLERS["illo_write_domain_record"](db, principal, capability_arguments)
        result["_mutates_domain"] = True
        return result
    if capability == "cycle.manage":
        return await _act_manage_cycle(db, principal, capability_arguments)
    if capability == "identity.manage":
        result = await manage_identity(db, principal, capability_arguments)
        result["_mutates_identity"] = True
        return result
    raise ValueError(f"Unknown {ACT_TOOL_NAME} capability: {capability}")


async def _tool_get_result(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    event_id = (
        _clean_optional_string(arguments.get("event_id"))
        or _clean_optional_string(arguments.get("submission_id"))
        or _clean_optional_string(arguments.get("result_id"))
    )
    if not event_id:
        raise ValueError(f"{RESULT_TOOL_NAME} requires event_id, submission_id, or result_id")
    result = await read_inbound_submission_result(
        db,
        org_id=principal.org_id,
        connection_id=principal.connection_id,
        event_id=event_id,
        include_payload=bool(arguments.get("include_payload", True)),
        limit=int(arguments.get("limit") or 25),
    )
    if result.mutated_inbound:
        await db.commit()
    payload = result.payload
    for candidate in (
        payload.get("event"),
        payload.get("latest_receipt"),
    ):
        failure = dict(candidate.get("failure") or {}) if isinstance(candidate, dict) else {}
        if not failure:
            continue
        public_failure = public_run_failure(failure.get("status"), failure.get("category"))
        if public_failure is not None:
            return {**payload, "failure": public_failure}
    return payload


async def _add_thread_trigger_result_if_needed(
    db: AsyncSession,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_payload: dict[str, Any],
    principal: external_agents.AgentBridgePrincipal,
) -> None:
    trigger_idea = tool_payload.pop("_trigger_idea", None)
    if trigger_idea is None:
        return
    body = str(tool_payload.pop("_trigger_body", "") or "")
    metadata = dict(tool_payload.pop("_trigger_metadata", None) or _tool_metadata(arguments, tool_name=tool_name))
    tool_payload["trigger"] = await _run_trigger_if_requested(
        db,
        idea=trigger_idea,
        body=body,
        metadata=metadata,
        principal=principal,
    )


TOOL_HANDLERS: dict[str, ToolHandler] = {
    SUBMIT_TOOL_NAME: _tool_submit,
    READ_TOOL_NAME: _tool_read,
    ACT_TOOL_NAME: _tool_act,
    RESULT_TOOL_NAME: _tool_get_result,
}


def _result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error_response(req_id: Any, *, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _request_id(message: Any) -> Any:
    return message.get("id") if isinstance(message, dict) else None


def _mcp_auth_error_response(payload: Any, message: str) -> JSONResponse:
    data = {"http_status": 401, "auth": "bearer"}
    if isinstance(payload, list):
        responses = [
            _error_response(_request_id(item), code=-32001, message=message, data=data)
            for item in payload
            if not isinstance(item, dict) or "id" in item
        ]
        return JSONResponse(responses)
    return JSONResponse(_error_response(_request_id(payload), code=-32001, message=message, data=data))


def _tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, default=str),
            }
        ]
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def _handle_mcp_request(
    message: dict[str, Any],
    *,
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    req_id = message.get("id")

    if method == "initialize":
        requested_version = str((message.get("params") or {}).get("protocolVersion") or "2025-06-18")
        return _result(
            req_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "illo", "version": "0.1.0"},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _result(req_id, {"tools": _list_tools(principal)})

    if method != "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    params = message.get("params") or {}
    tool_name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if tool_name not in MCP_TOOLS or tool_name not in TOOL_HANDLERS:
        return _result(req_id, _tool_error(f"Unknown tool: {tool_name}"))
    spec = MCP_TOOLS[tool_name]
    scope = spec.get("scope")
    if not _has_scope(principal, scope):
        return _result(req_id, _tool_error(f"Bridge token is missing scope: {scope}"))
    if not isinstance(arguments, dict):
        return _result(req_id, _tool_error("Tool arguments must be an object"))

    try:
        try:
            tool_payload = await TOOL_HANDLERS[tool_name](db, principal, arguments)
        except Exception as exc:
            raise_external_agent_http_error(exc)
        if spec.get("mutates_inbound"):
            await db.commit()
        if tool_payload.pop("_mutates_domain", False):
            await db.commit()
        if tool_payload.pop("_mutates_identity", False):
            await db.commit()
        if tool_payload.pop("_mutates_cycle", False):
            cycle_change = tool_payload.pop("_cycle_change", None)
            await db.commit()
            if isinstance(cycle_change, dict):
                publish_cycle_change_safe(
                    action=str(cycle_change.get("action") or "update"),
                    **dict(cycle_change.get("event") or {}),
                )
        if tool_payload.pop("_mutates_handoff", False):
            await db.commit()
        if tool_payload.pop("_mutates_workspace_app", False):
            workspace_app_change = tool_payload.pop("_workspace_app_change", None)
            await db.commit()
            if isinstance(workspace_app_change, dict):
                from brain.systems.workspace_apps.events import publish_workspace_app_change

                publish_workspace_app_change(
                    org_id=principal.org_id,
                    action=str(workspace_app_change.get("action") or "update"),
                    app=workspace_app_change.get("app"),
                    app_id=workspace_app_change.get("app_id"),
                    key=workspace_app_change.get("key"),
                )
        mutates_thread = bool(spec.get("mutates_thread") or tool_payload.pop("_mutates_thread", False))
        if mutates_thread:
            await _add_thread_trigger_result_if_needed(
                db,
                tool_name=tool_name,
                arguments=arguments,
                tool_payload=tool_payload,
                principal=principal,
            )
            await _commit_for_live_fanout(db)
            await _broadcast_thread_result(tool_payload, org_id=principal.org_id)
        return _result(req_id, _tool_result(tool_payload))
    except Exception as exc:
        await db.rollback()
        return _result(req_id, _tool_error(str(exc)))


async def _mcp_endpoint(
    request: Request,
    db: AsyncSession,
):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
            status_code=400,
        )
    try:
        principal = await _authenticate_mcp_principal(request, db)
    except external_agents.ExternalAgentAuthError as exc:
        return _mcp_auth_error_response(payload, f"MCP authentication failed: {exc}")
    except external_agents.ExternalAgentPermissionError as exc:
        return _mcp_auth_error_response(payload, f"MCP authentication failed: {exc}")

    if isinstance(payload, list):
        responses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            response = await _handle_mcp_request(item, db=db, principal=principal)
            if response is not None:
                responses.append(response)
        return JSONResponse(responses)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
            status_code=400,
        )
    response = await _handle_mcp_request(payload, db=db, principal=principal)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


@router.post("/mcp")
async def hosted_mcp(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _mcp_endpoint(request, db)


@router.post("/api/mcp")
async def hosted_api_mcp(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _mcp_endpoint(request, db)
