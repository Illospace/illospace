"""Worker assignment and evidence primitives for native AgentRun workers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import fnmatch
import os
from typing import Any


@dataclass(frozen=True)
class EvidenceRequirement:
    """A required artifact-level proof point for worker acceptance."""

    artifact_type: str | None = None
    kind: str = "artifact"
    id: str = ""
    description: str = ""
    min_count: int = 1
    title_contains: tuple[str, ...] = ()
    text_contains: tuple[str, ...] = ()
    payload_contains: dict[str, Any] = field(default_factory=dict)
    uri_required: bool = False
    required: bool = True

    def __post_init__(self) -> None:
        artifact_type = _optional_text(self.artifact_type)
        object.__setattr__(self, "artifact_type", artifact_type)
        object.__setattr__(self, "kind", str(self.kind or "artifact").strip().lower() or "artifact")
        object.__setattr__(self, "id", _optional_text(self.id) or artifact_type or "evidence")
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "min_count", max(1, int(self.min_count or 1)))
        object.__setattr__(self, "title_contains", _string_tuple(self.title_contains))
        object.__setattr__(self, "text_contains", _string_tuple(self.text_contains))
        object.__setattr__(self, "payload_contains", dict(self.payload_contains or {}))
        object.__setattr__(self, "uri_required", bool(self.uri_required))
        object.__setattr__(self, "required", bool(self.required))

    @property
    def requirement_id(self) -> str:
        return self.id

    def matches(self, artifact: Any) -> bool:
        if self.artifact_type and _artifact_type(artifact) != self.artifact_type:
            return False
        title = _artifact_text_value(artifact, "title")
        text = _artifact_text_value(artifact, "text")
        if any(fragment.lower() not in title.lower() for fragment in self.title_contains):
            return False
        if any(fragment.lower() not in text.lower() for fragment in self.text_contains):
            return False
        payload = _artifact_payload(artifact)
        if any(not _payload_matches(payload, key, expected) for key, expected in self.payload_contains.items()):
            return False
        if self.uri_required and not _artifact_text_value(artifact, "uri"):
            return False
        return True

    def matching_artifacts(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> tuple[Any, ...]:
        return tuple(artifact for artifact in _artifact_tuple(artifacts) if self.matches(artifact))

    def is_satisfied_by(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> bool:
        if not self.required:
            return True
        return len(self.matching_artifacts(artifacts)) >= self.min_count

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "artifact_type": self.artifact_type,
            "description": self.description,
            "min_count": self.min_count,
            "title_contains": list(self.title_contains),
            "text_contains": list(self.text_contains),
            "payload_contains": dict(self.payload_contains),
            "uri_required": self.uri_required,
            "required": self.required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | str | "EvidenceRequirement") -> "EvidenceRequirement":
        if isinstance(payload, EvidenceRequirement):
            return payload
        if isinstance(payload, str):
            return cls(artifact_type=payload, id=payload)
        if not isinstance(payload, Mapping):
            raise TypeError("EvidenceRequirement payload must be a mapping or string")
        return cls(
            artifact_type=_optional_text(payload.get("artifact_type") or payload.get("type")),
            kind=str(payload.get("kind") or "artifact"),
            id=str(payload.get("id") or payload.get("requirement_id") or ""),
            description=str(payload.get("description") or ""),
            min_count=int(payload.get("min_count") or payload.get("count") or 1),
            title_contains=_string_tuple(payload.get("title_contains")),
            text_contains=_string_tuple(payload.get("text_contains") or payload.get("must_contain")),
            payload_contains=dict(payload.get("payload_contains") or {}),
            uri_required=bool(payload.get("uri_required") or payload.get("requires_uri")),
            required=payload.get("required", True) is not False,
        )


@dataclass(frozen=True)
class AcceptanceCriteria:
    """Completion criteria for a worker assignment."""

    summary: str = ""
    checklist: tuple[str, ...] = ()
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", str(self.summary or "").strip())
        object.__setattr__(self, "checklist", _string_tuple(self.checklist))
        object.__setattr__(
            self,
            "evidence_requirements",
            tuple(EvidenceRequirement.from_payload(item) for item in self.evidence_requirements),
        )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def required_evidence(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(requirement for requirement in self.evidence_requirements if requirement.required)

    def missing_evidence(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> tuple[EvidenceRequirement, ...]:
        return tuple(requirement for requirement in self.required_evidence() if not requirement.is_satisfied_by(artifacts))

    def has_required_evidence(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> bool:
        return not self.missing_evidence(artifacts)

    def satisfied_by(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> bool:
        return self.has_required_evidence(artifacts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "checklist": list(self.checklist),
            "evidence_requirements": [requirement.to_payload() for requirement in self.evidence_requirements],
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | list[Any] | tuple[Any, ...] | "AcceptanceCriteria" | None,
    ) -> "AcceptanceCriteria":
        if isinstance(payload, AcceptanceCriteria):
            return payload
        if payload is None:
            return cls()
        if isinstance(payload, (list, tuple)):
            if all(isinstance(item, str) for item in payload):
                return cls(checklist=tuple(str(item) for item in payload))
            return cls(evidence_requirements=tuple(EvidenceRequirement.from_payload(item) for item in payload))
        if not isinstance(payload, Mapping):
            raise TypeError("AcceptanceCriteria payload must be a mapping, list, or tuple")
        requirements = (
            payload.get("evidence_requirements")
            or payload.get("required_evidence")
            or payload.get("evidence")
            or payload.get("requirements")
            or ()
        )
        return cls(
            summary=str(payload.get("summary") or payload.get("description") or ""),
            checklist=_string_tuple(payload.get("checklist") or payload.get("criteria")),
            evidence_requirements=tuple(EvidenceRequirement.from_payload(item) for item in requirements),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class WorkerAssignment:
    """A native worker child-run assignment."""

    id: str = ""
    role: str = "worker"
    objective: str = ""
    node_id: str | None = None
    run_id: int | None = None
    allowed_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    forbidden_resources: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    risk_level: str = "medium"
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    acceptance_criteria: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        role = str(self.role or "worker").strip() or "worker"
        node_id = _optional_text(self.node_id)
        assignment_id = _optional_text(self.id) or node_id or role
        object.__setattr__(self, "id", assignment_id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "objective", str(self.objective or "").strip())
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "allowed_files", _string_tuple(self.allowed_files))
        object.__setattr__(self, "forbidden_files", _string_tuple(self.forbidden_files))
        object.__setattr__(self, "allowed_resources", _string_tuple(self.allowed_resources))
        object.__setattr__(self, "forbidden_resources", _string_tuple(self.forbidden_resources))
        object.__setattr__(self, "expected_artifacts", _string_tuple(self.expected_artifacts))
        object.__setattr__(self, "risk_level", str(self.risk_level or "medium").strip().lower() or "medium")
        object.__setattr__(
            self,
            "evidence_requirements",
            tuple(EvidenceRequirement.from_payload(item) for item in self.evidence_requirements),
        )
        object.__setattr__(self, "acceptance_criteria", AcceptanceCriteria.from_payload(self.acceptance_criteria))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def assignment_id(self) -> str:
        return self.id

    def scope_payload(self) -> dict[str, Any]:
        payload = {
            "assignment_id": self.id,
            "node_id": self.node_id,
            "role": self.role,
            "objective": self.objective,
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "allowed_resources": list(self.allowed_resources),
            "forbidden_resources": list(self.forbidden_resources),
            "expected_artifacts": list(self.expected_artifacts),
            "risk_level": self.risk_level,
            "evidence_requirements": [requirement.to_payload() for requirement in self.evidence_requirements],
            "acceptance_criteria": self.acceptance_criteria.to_payload(),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    def to_worker_scope_payload(self) -> dict[str, Any]:
        return self.scope_payload()

    def to_prompt_payload(self) -> dict[str, Any]:
        return self.scope_payload()

    def to_tool_scope(self):
        from brain.systems.runs.tools import ToolScope

        return ToolScope(
            allowed_files=self.allowed_files,
            forbidden_files=self.forbidden_files,
            allowed_resources=self.allowed_resources,
            forbidden_resources=self.forbidden_resources,
        )

    def allows_file(self, path: str) -> bool:
        normalized = _normalize_pattern(path)
        if _matches_any(normalized, self.forbidden_files):
            return False
        return not self.allowed_files or _matches_any(normalized, self.allowed_files)

    def allows_resource(self, resource: str) -> bool:
        normalized = str(resource or "").strip()
        if _matches_any(normalized, self.forbidden_resources):
            return False
        return not self.allowed_resources or _matches_any(normalized, self.allowed_resources)

    def required_evidence(self) -> tuple[EvidenceRequirement, ...]:
        expected_artifact_requirements = tuple(
            EvidenceRequirement(artifact_type=artifact_type, id=artifact_type, description=f"Produce {artifact_type}")
            for artifact_type in self.expected_artifacts
        )
        return _dedupe_requirements(
            tuple(
                requirement
                for requirement in (
                    *expected_artifact_requirements,
                    *self.evidence_requirements,
                    *self.acceptance_criteria.required_evidence(),
                )
                if requirement.required and not _is_implicit_worker_result_requirement(requirement)
            )
        )

    def required_artifact_types(self) -> tuple[str, ...]:
        expected_artifact_requirements = tuple(
            EvidenceRequirement(artifact_type=artifact_type, id=artifact_type, description=f"Produce {artifact_type}")
            for artifact_type in self.expected_artifacts
        )
        values = [
            requirement.artifact_type
            for requirement in (
                *expected_artifact_requirements,
                *self.evidence_requirements,
                *self.acceptance_criteria.evidence_requirements,
            )
            if requirement.required and requirement.artifact_type
        ]
        return tuple(dict.fromkeys(str(value) for value in values if value))

    def missing_evidence(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> tuple[EvidenceRequirement, ...]:
        return tuple(requirement for requirement in self.required_evidence() if not requirement.is_satisfied_by(artifacts))

    def has_required_evidence(self, artifacts: list[Any] | tuple[Any, ...] | Any) -> bool:
        return not self.missing_evidence(artifacts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "assignment_id": self.id,
            "role": self.role,
            "objective": self.objective,
            "node_id": self.node_id,
            "run_id": self.run_id,
            "scope": {
                "allowed_files": list(self.allowed_files),
                "forbidden_files": list(self.forbidden_files),
                "allowed_resources": list(self.allowed_resources),
                "forbidden_resources": list(self.forbidden_resources),
                "expected_artifacts": list(self.expected_artifacts),
                "risk_level": self.risk_level,
            },
            "evidence_requirements": [requirement.to_payload() for requirement in self.evidence_requirements],
            "acceptance_criteria": self.acceptance_criteria.to_payload(),
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | "WorkerAssignment" | None,
        *,
        default_id: str | None = None,
    ) -> "WorkerAssignment":
        if isinstance(payload, WorkerAssignment):
            return payload
        if payload is None:
            return cls(id=str(default_id or "worker"))
        if not isinstance(payload, Mapping):
            raise TypeError("WorkerAssignment payload must be a mapping")
        scope = payload.get("scope") if isinstance(payload.get("scope"), Mapping) else {}
        criteria_payload = (
            payload.get("acceptance_criteria")
            or payload.get("acceptance")
            or payload.get("criteria")
        )
        evidence_payload = (
            payload.get("evidence_requirements")
            or payload.get("required_evidence")
            or scope.get("evidence_requirements")
            or ()
        )
        return cls(
            id=str(payload.get("id") or payload.get("assignment_id") or payload.get("node_id") or default_id or ""),
            role=str(payload.get("role") or scope.get("role") or default_id or "worker"),
            objective=str(payload.get("objective") or scope.get("objective") or ""),
            node_id=_optional_text(payload.get("node_id") or scope.get("node_id")),
            run_id=_optional_int(payload.get("run_id")),
            allowed_files=_string_tuple(payload.get("allowed_files") or scope.get("allowed_files") or scope.get("files")),
            forbidden_files=_string_tuple(payload.get("forbidden_files") or scope.get("forbidden_files")),
            allowed_resources=_string_tuple(
                payload.get("allowed_resources") or scope.get("allowed_resources") or scope.get("resources")
            ),
            forbidden_resources=_string_tuple(payload.get("forbidden_resources") or scope.get("forbidden_resources")),
            expected_artifacts=_string_tuple(payload.get("expected_artifacts") or scope.get("expected_artifacts")),
            risk_level=str(payload.get("risk_level") or payload.get("risk") or scope.get("risk_level") or "medium"),
            evidence_requirements=tuple(EvidenceRequirement.from_payload(item) for item in evidence_payload),
            acceptance_criteria=AcceptanceCriteria.from_payload(criteria_payload),
            metadata=dict(payload.get("metadata") or {}),
        )


def _artifact_tuple(artifacts: list[Any] | tuple[Any, ...] | Any) -> tuple[Any, ...]:
    if artifacts is None:
        return ()
    if isinstance(artifacts, (list, tuple)):
        return tuple(artifacts)
    return (artifacts,)


def _artifact_type(artifact: Any) -> str | None:
    value = _artifact_value(artifact, "artifact_type")
    if value is None:
        value = _artifact_value(artifact, "type")
    value = getattr(value, "value", value)
    return _optional_text(value)


def _artifact_payload(artifact: Any) -> dict[str, Any]:
    value = _artifact_value(artifact, "payload")
    return dict(value) if isinstance(value, Mapping) else {}


def _artifact_text_value(artifact: Any, key: str) -> str:
    return str(_artifact_value(artifact, key) or "")


def _artifact_value(artifact: Any, key: str) -> Any:
    if isinstance(artifact, Mapping):
        if key in artifact:
            return artifact.get(key)
        if key == "artifact_type":
            return artifact.get("type")
        return None
    return getattr(artifact, key, None)


def _payload_matches(payload: Mapping[str, Any], key: str, expected: Any) -> bool:
    actual = _payload_lookup(payload, key)
    return _value_matches(actual, expected)


def _payload_lookup(payload: Mapping[str, Any], key: str) -> Any:
    current: Any = payload
    for part in str(key).split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (list, tuple, set, frozenset)):
        if isinstance(actual, (list, tuple, set, frozenset)):
            return all(item in actual for item in expected)
        return actual in expected
    return actual == expected


def _dedupe_requirements(requirements: tuple[EvidenceRequirement, ...]) -> tuple[EvidenceRequirement, ...]:
    seen: set[tuple[Any, ...]] = set()
    result: list[EvidenceRequirement] = []
    for requirement in requirements:
        key = (
            requirement.id,
            requirement.artifact_type,
            requirement.min_count,
            requirement.title_contains,
            requirement.text_contains,
            tuple(sorted(requirement.payload_contains.items())),
            requirement.uri_required,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(requirement)
    return tuple(result)


def _is_implicit_worker_result_requirement(requirement: EvidenceRequirement) -> bool:
    return (
        requirement.artifact_type == "worker_result"
        and requirement.description.strip().lower() == "produce the worker result summary"
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Mapping):
        value = value.values()
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        text = str(value).strip()
        return (text,) if text else ()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _normalize_pattern(value: Any) -> str:
    text = str(value or "").strip()
    return os.path.normpath(text).replace(os.sep, "/") if text else ""


def _matches_any(target: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normalize_pattern(target)
    basename = os.path.basename(normalized)
    for pattern in patterns:
        normalized_pattern = _normalize_pattern(pattern)
        if (
            fnmatch.fnmatch(normalized, normalized_pattern)
            or fnmatch.fnmatch(basename, normalized_pattern)
            or normalized == normalized_pattern
        ):
            return True
    return False


__all__ = [
    "AcceptanceCriteria",
    "EvidenceRequirement",
    "WorkerAssignment",
]
