"""Launch handoff capabilities for the hosted MCP endpoint."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems import launch_handoffs
from brain.systems.external_agents import service as external_agents


READ_CAPABILITIES: dict[str, dict[str, Any]] = {
    "handoff.get": {
        "description": "Read a launch handoff prepared by Illo for Codex or another local agent.",
        "arguments": {"handoff_id": "string", "url": "string"},
    },
    "packets.outcomes": {
        "description": (
            "Handoff-packet outcome summary (minted / launched / ignored, median time to "
            "launch, per-member split) — the digest's packets footer reads this."
        ),
        "arguments": {"since_hours": "number (default 168)"},
    },
}


ACT_CAPABILITIES: dict[str, dict[str, Any]] = {
    "handoff.create": {
        "description": "Create a durable launch handoff link for opening work in Codex or another local agent.",
        "arguments": {
            "title": "string",
            "instructions": "string",
            "summary": "string",
            "target_tool": "string",
            "repo_origin_url": "string",
            "branch_hint": "string",
            "source_surface": "string",
            "source_ref": "object",
            "context_parts": "object[]",
            "acceptance_criteria": "array",
            "idempotency_key": "string",
            "metadata": "object",
        },
    },
}


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _clean_json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _required_capability_string(arguments: dict[str, Any], key: str, *, capability: str) -> str:
    value = _clean_optional_string(arguments.get(key))
    if not value:
        raise ValueError(f"{capability} requires a non-empty {key}")
    return value


def handoff_argument_id(arguments: dict[str, Any]) -> str:
    for key in ("handoff_url", "launch_url", "url", "route", "handoff_id", "id"):
        handoff_id = launch_handoffs.handoff_id_from_reference(
            arguments.get(key),
            allow_raw_id=key in {"handoff_id", "id"},
        )
        if handoff_id:
            return handoff_id
    return str(arguments.get("handoff_id") or arguments.get("id") or "")


async def read_handoff(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    row = await launch_handoffs.require_launch_handoff(
        db,
        handoff_argument_id(arguments),
        org_id=principal.org_id,
    )
    return {"handoff": launch_handoffs.serialize_launch_handoff(row)}


async def read_packet_outcomes(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Slice 07: the outcomes reporter over the caller's org, JSON-safe."""
    from datetime import datetime, timedelta, timezone

    from brain.systems.briefing.outcomes import (
        format_outcomes_line,
        load_packet_handoffs,
        packet_outcomes,
    )

    try:
        since_hours = float(arguments.get("since_hours") or 168)
    except (TypeError, ValueError):
        since_hours = 168.0
    since_hours = max(1.0, min(since_hours, 24 * 90))
    now = datetime.now(timezone.utc)
    rows = await load_packet_handoffs(
        db, org_id=principal.org_id, since=now - timedelta(hours=since_hours)
    )
    summary = packet_outcomes(rows, now=now)
    return {
        "since_hours": since_hours,
        "outcomes": summary.to_dict(),
        "digest_line": format_outcomes_line(summary),
    }


async def create_handoff(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
    *,
    metadata: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_idempotency_key = _clean_optional_string(arguments.get("idempotency_key")) or idempotency_key
    row = await launch_handoffs.create_launch_handoff(
        db,
        launch_handoffs.LaunchHandoffCreateInput(
            org_id=principal.org_id,
            created_by_user_id=principal.owner_user_id,
            title=_required_capability_string(arguments, "title", capability="handoff.create"),
            instructions=_required_capability_string(arguments, "instructions", capability="handoff.create"),
            target_tool=_clean_optional_string(arguments.get("target_tool")) or launch_handoffs.TARGET_CODEX,
            summary=_clean_optional_string(arguments.get("summary")),
            source_surface=_clean_optional_string(arguments.get("source_surface")) or "mcp_personal_tool",
            source_ref=_clean_dict(arguments.get("source_ref")),
            context_parts=_clean_object_list(arguments.get("context_parts")),
            acceptance_criteria=_clean_json_list(arguments.get("acceptance_criteria")),
            repo_origin_url=_clean_optional_string(arguments.get("repo_origin_url")),
            branch_hint=_clean_optional_string(arguments.get("branch_hint")),
            idempotency_key=clean_idempotency_key,
            metadata=metadata,
        ),
    )
    payload = launch_handoffs.serialize_launch_handoff(row)
    return {"handoff": payload, "launch_url": payload["launch_url"], "_mutates_handoff": True}


__all__ = [
    "ACT_CAPABILITIES",
    "READ_CAPABILITIES",
    "create_handoff",
    "handoff_argument_id",
    "read_handoff",
    "read_packet_outcomes",
]
