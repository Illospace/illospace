"""Shared execution-artifact persistence and lookup helpers."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunArtifactRow
from brain.systems.runs.domain import AgentRunArtifact, EventVisibility
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.artifacts import coerce_execution_artifact, coerce_execution_artifacts
from brain.systems.runs.store import AsyncAgentRunStore

logger = logging.getLogger("agent_runtime")


def _append_unique_artifacts(current: list, artifacts: list[Any]) -> list:
    """Return a copy of current with artifacts appended once, preserving order."""
    merged = coerce_execution_artifacts(list(current or []))
    for artifact in artifacts:
        normalized = coerce_execution_artifact(artifact)
        if normalized is None:
            continue
        if normalized not in merged:
            merged.append(normalized)
    return merged


async def load_execution_artifacts(*, execution_id: str | None = None, execution_ids: list[str] | None = None) -> list[dict]:
    """Load persisted execution artifacts from canonical AgentRun artifacts."""
    requested_execution_ids = [eid for eid in [execution_id, *(execution_ids or [])] if eid]
    if not requested_execution_ids:
        return []
    try:
        async with UnitOfWork() as uow:
            records = (
                await uow.session.scalars(
                select(AgentRunArtifactRow)
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
                )
            ).all()
            artifacts: list[dict] = []
            for record in records:
                payload = record.payload if isinstance(record.payload, dict) else {}
                provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
                if payload.get("execution_id") not in requested_execution_ids and provenance.get("execution_id") not in requested_execution_ids:
                    continue
                for normalized in coerce_execution_artifacts([payload]):
                    if normalized not in artifacts:
                        artifacts.append(normalized)
            return artifacts
    except Exception as e:
        logger.debug(
            "Execution artifact load failed for execution ids %s: %s",
            requested_execution_ids,
            e,
        )
        return []


async def append_run_execution_artifacts(*, run_id: int | None, artifacts: list[Any]) -> None:
    """Persist run-level execution artifacts on the canonical AgentRun ledger."""
    if not run_id or not artifacts:
        return
    try:
        async with UnitOfWork() as uow:
            store = AsyncAgentRunStore(uow.session)
            run = await store.get_run(int(run_id))
            if not run:
                return
            normalized_artifacts = _append_unique_artifacts([], artifacts)
            existing = {
                json.dumps(row.payload or {}, sort_keys=True, default=str)
                for row in (
                    await uow.session.scalars(
                        select(AgentRunArtifactRow)
                        .where(AgentRunArtifactRow.run_id == int(run_id))
                        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
                    )
                ).all()
                if isinstance(row.payload, dict)
            }
            for artifact in normalized_artifacts:
                artifact_key = json.dumps(artifact, sort_keys=True, default=str)
                if artifact_key in existing:
                    continue
                existing.add(artifact_key)
                await store.append_artifact(
                    AgentRunArtifact(
                        run_id=int(run_id),
                        root_run_id=run.root_run_id,
                        artifact_type=str(artifact.get("type") or "execution_artifact"),
                        title=str(artifact.get("summary") or artifact.get("type") or "Execution artifact"),
                        payload=artifact,
                        visibility=EventVisibility.INTERNAL,
                    )
                )
    except Exception:
        logger.debug("Failed to persist run execution artifacts", exc_info=True)


async def append_execution_artifacts(*, execution_id: str, provenance: dict, artifacts: list[Any]) -> None:
    """Persist execution-scoped artifacts onto the canonical AgentRun ledger."""
    if not execution_id or not artifacts:
        return
    run_id = provenance.get("run_id") if isinstance(provenance, dict) else None
    if not run_id:
        return
    try:
        async with UnitOfWork() as uow:
            store = AsyncAgentRunStore(uow.session)
            run = await store.get_run(int(run_id))
            if not run:
                return
            normalized_artifacts = _append_unique_artifacts([], artifacts)
            existing = {
                json.dumps(row.payload or {}, sort_keys=True, default=str)
                for row in (
                    await uow.session.scalars(
                        select(AgentRunArtifactRow)
                        .where(AgentRunArtifactRow.run_id == int(run_id))
                        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
                    )
                ).all()
                if isinstance(row.payload, dict)
            }
            for artifact in normalized_artifacts:
                payload = dict(artifact)
                provenance_payload = dict(provenance or {})
                provenance_payload["execution_id"] = execution_id
                payload.setdefault("provenance", provenance_payload)
                payload.setdefault("execution_id", execution_id)
                artifact_key = json.dumps(payload, sort_keys=True, default=str)
                if artifact_key in existing:
                    continue
                existing.add(artifact_key)
                await store.append_artifact(
                    AgentRunArtifact(
                        run_id=int(run_id),
                        root_run_id=run.root_run_id,
                        artifact_type=str(payload.get("type") or "execution_artifact"),
                        title=str(payload.get("summary") or payload.get("type") or "Execution artifact"),
                        payload=payload,
                        visibility=EventVisibility.INTERNAL,
                    )
                )
    except Exception:
        logger.debug("Failed to persist execution artifacts", exc_info=True)


async def update_execution_summary(
    *,
    execution_id: str,
    provenance: dict,
    outcome: str,
    outcome_details: str | None = None,
    agent_model: str | None = None,
    thinking_level: str | None = None,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_used: int = 0,
    estimated_cost: float = 0.0,
    duration_sec: int = 0,
) -> None:
    """Record execution summary on the canonical AgentRun ledger."""
    if not execution_id:
        return
    run_id = provenance.get("run_id") if isinstance(provenance, dict) else None
    if not run_id:
        return
    try:
        async with UnitOfWork() as uow:
            store = AsyncAgentRunStore(uow.session)
            run = await store.get_run(int(run_id))
            if not run:
                return
            payload = {
                "type": "execution_summary",
                "execution_id": execution_id,
                "provenance": dict(provenance or {}),
                "outcome": outcome,
                "outcome_details": (outcome_details or "")[:4000] or None,
                "agent_model": agent_model,
                "thinking_level": thinking_level,
                "tokens_input": int(tokens_input or 0),
                "tokens_output": int(tokens_output or 0),
                "tokens_used": int(tokens_used or 0),
                "estimated_cost": float(estimated_cost or 0.0),
                "duration_sec": int(duration_sec or 0),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            await store.append_artifact(
                AgentRunArtifact(
                    run_id=int(run_id),
                    root_run_id=run.root_run_id,
                    artifact_type="execution_summary",
                    title="Execution summary",
                    payload=payload,
                    visibility=EventVisibility.INTERNAL,
                )
            )
            metadata = dict(run.metadata_ or {})
            metadata["last_execution_summary"] = payload
            run.metadata_ = metadata
            await uow.session.flush()
    except Exception:
        logger.debug("Failed to update execution summary", exc_info=True)
