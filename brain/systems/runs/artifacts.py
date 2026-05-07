"""Typed AgentRun artifact and ownership-scope helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from brain.systems.runs.domain import (
    AgentRunArtifact,
    ArtifactType as RunArtifactType,
    EventVisibility,
)


class ArtifactType(StrEnum):
    WORKER_ACTIVITY = "worker_activity"
    FILE_OBSERVATION = "file_observation"
    TEST_RUN = "test_run"
    COMMAND_RUN = "command_run"
    PROJECT_CONTEXT_PROVENANCE = "project_context_provenance"
    EXISTING_PR_UNDER_REVIEW = "existing_pr_under_review"


@runtime_checkable
class ArtifactPayload(Protocol):
    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible artifact payload."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ArtifactPayload):
        return value.to_dict()
    if isinstance(value, OwnershipScope):
        return value.to_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _prune_empty(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _jsonable(value)
        for key, value in payload.items()
        if value not in (None, "", {})
    }


def _list_of_strings(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _list_of_jsonable(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return [_jsonable(value)]


@dataclass(frozen=True)
class OwnershipScope:
    """A worker/file ownership scope that serializes to compact JSON."""

    skill_name: str | None = None
    node_id: str | None = None
    session_strategy: str | None = None
    workspace_root: str | None = None
    allowed_workspaces: list[Any] = field(default_factory=list)
    role: str | None = None
    owns: str | None = None
    escalate_if: str | None = None
    reads_from: list[str] = field(default_factory=list)
    publishes_to: list[str] = field(default_factory=list)
    owned_paths: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    mode: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "OwnershipScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        known = {
            "skill_name",
            "node_id",
            "session_strategy",
            "workspace_root",
            "allowed_workspaces",
            "role",
            "owns",
            "escalate_if",
            "reads_from",
            "publishes_to",
            "owned_paths",
            "allowed_paths",
            "forbidden_paths",
            "mode",
        }
        return cls(
            skill_name=_optional_text(value.get("skill_name")),
            node_id=_optional_text(value.get("node_id")),
            session_strategy=_optional_text(value.get("session_strategy")),
            workspace_root=_optional_text(value.get("workspace_root")),
            allowed_workspaces=_list_of_jsonable(value.get("allowed_workspaces")),
            role=_optional_text(value.get("role")),
            owns=_optional_text(value.get("owns")),
            escalate_if=_optional_text(value.get("escalate_if")),
            reads_from=_list_of_strings(value.get("reads_from")),
            publishes_to=_list_of_strings(value.get("publishes_to")),
            owned_paths=_list_of_strings(value.get("owned_paths")),
            allowed_paths=_list_of_strings(value.get("allowed_paths")),
            forbidden_paths=_list_of_strings(value.get("forbidden_paths")),
            mode=_optional_text(value.get("mode")),
            extra={
                str(key): _jsonable(val)
                for key, val in value.items()
                if key not in known and val not in (None, "", {})
            },
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "skill_name": self.skill_name,
            "node_id": self.node_id,
            "session_strategy": self.session_strategy,
            "workspace_root": self.workspace_root,
            "allowed_workspaces": self.allowed_workspaces,
            "role": self.role,
            "owns": self.owns,
            "escalate_if": self.escalate_if,
            "reads_from": self.reads_from,
            "publishes_to": self.publishes_to,
            "owned_paths": self.owned_paths,
            "allowed_paths": self.allowed_paths,
            "forbidden_paths": self.forbidden_paths,
            "mode": self.mode,
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def coerce_ownership_scope(value: Any) -> dict[str, Any]:
    """Return an ownership scope as JSON-compatible dict data."""
    return OwnershipScope.from_value(value).to_dict()


@dataclass(frozen=True)
class WorkerActivityArtifact:
    event: str
    status: str
    worker_id: str
    execution_id: str
    run_id: int | str | None = None
    node_id: str | None = None
    skill: str | None = None
    session_id: str | None = None
    current_tool: str | None = None
    target: Mapping[str, Any] | None = None
    ownership_scope: OwnershipScope | Mapping[str, Any] | None = None
    last_progress_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.WORKER_ACTIVITY.value,
            "schema_version": 1,
            "event": self.event,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "skill": self.skill or "general",
            "session_id": self.session_id,
            "current_tool": self.current_tool,
            "target": self.target or {},
            "status": self.status,
            "last_progress_at": self.last_progress_at or _utc_now(),
            "ownership_scope": coerce_ownership_scope(self.ownership_scope),
            "error": str(self.error)[:1000] if self.error else None,
        }
        return _prune_empty(payload)


@dataclass(frozen=True)
class FileObservationArtifact:
    operation: str
    path: str
    absolute_path: str | None = None
    sha256: str | None = None
    mtime: float | None = None
    size_bytes: int | None = None
    observed_at: str | None = None
    run_id: int | str | None = None
    execution_id: str | None = None
    worker_id: str | None = None
    node_id: str | None = None
    skill: str | None = None
    session_id: str | None = None
    ownership_scope: OwnershipScope | Mapping[str, Any] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.FILE_OBSERVATION.value,
            "schema_version": 1,
            "operation": str(self.operation or "").lower(),
            "path": self.path,
            "absolute_path": self.absolute_path,
            "sha256": self.sha256,
            "mtime": self.mtime,
            "size_bytes": self.size_bytes,
            "observed_at": self.observed_at or _utc_now(),
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "skill": self.skill,
            "session_id": self.session_id,
            "ownership_scope": coerce_ownership_scope(self.ownership_scope),
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


@dataclass(frozen=True)
class TestRunArtifact:
    command: str
    status: str
    exit_code: int | None = None
    summary: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.TEST_RUN.value,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "summary": self.summary,
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


@dataclass(frozen=True)
class CommandRunArtifact:
    command: str
    status: str
    exit_code: int | None = None
    working_dir: str | None = None
    summary: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.COMMAND_RUN.value,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "working_dir": self.working_dir,
            "summary": self.summary,
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


@dataclass(frozen=True)
class ProjectContextProvenanceArtifact:
    """Evidence that an artifact or command was evaluated against Project Context."""

    summary: str
    project_context_id: str | None = None
    project_context_status: str | None = None
    path: str | None = None
    permission_allowed: bool | None = None
    permission_reason: str | None = None
    permission_scope: Mapping[str, Any] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.PROJECT_CONTEXT_PROVENANCE.value,
            "summary": self.summary,
            "project_context_id": self.project_context_id,
            "project_context_status": self.project_context_status,
            "path": self.path,
            "permission_allowed": self.permission_allowed,
            "permission_reason": self.permission_reason,
            "permission_scope": self.permission_scope,
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


@dataclass(frozen=True)
class ExistingPullRequestReviewArtifact:
    """Evidence that a worker reviewed an existing external pull request."""

    url: str
    number: int | None = None
    repo: str | None = None
    state: str | None = None
    title: str | None = None
    head_sha: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    mergeable: str | None = None
    summary: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": ArtifactType.EXISTING_PR_UNDER_REVIEW.value,
            "url": self.url,
            "number": self.number,
            "repo": self.repo,
            "state": self.state,
            "title": self.title,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "mergeable": self.mergeable,
            "summary": self.summary,
            **dict(self.extra or {}),
        }
        return _prune_empty(payload)


def worker_activity_artifact(**kwargs: Any) -> dict[str, Any]:
    return WorkerActivityArtifact(**kwargs).to_dict()


def file_observation_artifact(**kwargs: Any) -> dict[str, Any]:
    return FileObservationArtifact(**kwargs).to_dict()


def test_run_artifact(**kwargs: Any) -> dict[str, Any]:
    return TestRunArtifact(**kwargs).to_dict()


def command_run_artifact(**kwargs: Any) -> dict[str, Any]:
    return CommandRunArtifact(**kwargs).to_dict()


def project_context_provenance_artifact(**kwargs: Any) -> dict[str, Any]:
    return ProjectContextProvenanceArtifact(**kwargs).to_dict()


def existing_pr_under_review_artifact(**kwargs: Any) -> dict[str, Any]:
    return ExistingPullRequestReviewArtifact(**kwargs).to_dict()


def coerce_execution_artifact(value: Any) -> dict[str, Any] | None:
    """Accept typed artifacts and plain artifact dicts at JSON boundaries."""
    if isinstance(value, ArtifactPayload):
        return value.to_dict()
    if isinstance(value, Mapping):
        return _jsonable(dict(value))
    return None


def coerce_execution_artifacts(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    artifacts: list[dict[str, Any]] = []
    for value in values:
        artifact = coerce_execution_artifact(value)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def file_observation_artifacts(values: Any) -> list[dict[str, Any]]:
    """Return normalized, documented file-observation artifact payloads."""
    observations: list[dict[str, Any]] = []
    for artifact in coerce_execution_artifacts(values):
        if artifact.get("type") != ArtifactType.FILE_OBSERVATION.value:
            continue
        path = artifact.get("path") or artifact.get("relative_path") or artifact.get("absolute_path")
        if not path:
            continue
        provenance = artifact.get("provenance") if isinstance(artifact.get("provenance"), Mapping) else {}
        normalized = {
            **artifact,
            "type": ArtifactType.FILE_OBSERVATION.value,
            "schema_version": int(artifact.get("schema_version") or 1),
            "operation": str(artifact.get("operation") or "").lower(),
            "path": str(path),
            "execution_id": artifact.get("execution_id") or provenance.get("execution_id"),
            "ownership_scope": coerce_ownership_scope(
                artifact.get("ownership_scope") or provenance.get("ownership_scope")
            ),
        }
        observations.append(_prune_empty(normalized))
    return observations


def test_run_artifacts(values: Any) -> list[dict[str, Any]]:
    return [
        artifact
        for artifact in coerce_execution_artifacts(values)
        if artifact.get("type") == ArtifactType.TEST_RUN.value
    ]


def existing_pr_review_artifacts(values: Any) -> list[dict[str, Any]]:
    return [
        artifact
        for artifact in coerce_execution_artifacts(values)
        if artifact.get("type") == ArtifactType.EXISTING_PR_UNDER_REVIEW.value
    ]


def final_answer_artifact(run_id: int, text: str, *, root_run_id: int | None = None) -> AgentRunArtifact:
    return AgentRunArtifact(
        run_id=run_id,
        root_run_id=root_run_id,
        artifact_type=RunArtifactType.FINAL_ANSWER,
        title="Final answer",
        text=text,
    )


def evidence_artifact(
    run_id: int,
    *,
    title: str,
    payload: dict[str, Any],
    root_run_id: int | None = None,
    visibility: EventVisibility = EventVisibility.INTERNAL,
) -> AgentRunArtifact:
    return AgentRunArtifact(
        run_id=run_id,
        root_run_id=root_run_id,
        artifact_type=RunArtifactType.VERIFIER_EVIDENCE,
        title=title,
        payload=payload,
        visibility=visibility,
    )
