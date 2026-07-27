"""Metadata types for agent tool registrations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

EnumT = TypeVar("EnumT", bound=StrEnum)


class ToolAvailability(StrEnum):
    COORDINATOR = "coordinator"
    WORKER = "worker"


class ToolPermission(StrEnum):
    AUTOMATE_BROWSER = "automate_browser"
    CLOSE_SESSION = "close_session"
    EXECUTE_PYTHON = "execute_python"
    EXECUTE_SHELL = "execute_shell"
    EXECUTE_TESTS = "execute_tests"
    MANAGE_INBOUND = "manage_inbound"
    MANAGE_CYCLES = "manage_cycles"
    MANAGE_RUNTIME = "manage_runtime"
    MANAGE_SOUL = "manage_soul"
    NETWORK_READ = "network_read"
    PARALLEL_READ = "parallel_read"
    POST_REPLY = "post_reply"
    POST_VISUAL_REPLY = "post_visual_reply"
    PROMOTE_SESSION = "promote_session"
    READ = "read"
    READ_ACTIVITY = "read_activity"
    READ_DOMAIN = "read_domain"
    READ_MEMORY = "read_memory"
    READ_RUNTIME = "read_runtime"
    READ_SECRET = "read_secret"
    READ_SESSION = "read_session"
    READ_SKILLS = "read_skills"
    READ_WORKSPACE = "read_workspace"
    SPAWN_WORKER = "spawn_worker"
    WRITE_MEMORY = "write_memory"
    WRITE_CHAT = "write_chat"
    WRITE_DOMAIN = "write_domain"
    WRITE_IDEA = "write_idea"
    WRITE_PROJECT = "write_project"
    WRITE_WORKSPACE_APP = "write_workspace_app"
    WRITE_SESSION = "write_session"
    WRITE_SKILL = "write_skill"
    WRITE_WORKSPACE = "write_workspace"


class ToolRiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolSideEffectClass(StrEnum):
    APPEND_ONLY = "append_only"
    BROWSER_ARTIFACT = "browser_artifact"
    BROWSER_INTERACTION = "browser_interaction"
    BROWSER_NAVIGATION = "browser_navigation"
    BROWSER_SESSION = "browser_session"
    CYCLE_MANAGEMENT = "cycle_management"
    CHAT_MESSAGE = "chat_message"
    RUN_ANNOTATION = "run_annotation"
    DOMAIN_MANAGEMENT = "domain_management"
    INBOUND_CONFIGURATION = "inbound_configuration"
    IDEA_MANAGEMENT = "idea_management"
    MEMORY_CURATION = "memory_curation"
    DEPLOYMENT_MANAGEMENT = "deployment_management"
    WORKSPACE_TOOL_MANAGEMENT = "workspace_tool_management"
    SOUL_MANAGEMENT = "soul_management"
    PROJECT_CONTEXT_MANAGEMENT = "project_context_management"
    FILE_EDIT = "file_edit"
    FILE_WRITE = "file_write"
    READ_ONLY = "read_only"
    READ_ONLY_EXTERNAL = "read_only_external"
    RUN_SPAWN = "run_spawn"
    SCRATCHPAD = "scratchpad"
    SCRATCHPAD_LIFECYCLE = "scratchpad_lifecycle"
    SHELL = "shell"
    SKILL_WRITE = "skill_write"
    WRITE = "write"
    WORKSPACE_APP_MANAGEMENT = "workspace_app_management"


def is_write_side_effect_class(value: ToolSideEffectClass | str) -> bool:
    """Return whether an exact registration side-effect class may change state."""
    side_effect_class = (
        value
        if isinstance(value, ToolSideEffectClass)
        else ToolSideEffectClass(str(value))
    )
    return side_effect_class not in {
        ToolSideEffectClass.READ_ONLY,
        ToolSideEffectClass.READ_ONLY_EXTERNAL,
    }


class ToolReversibility(StrEnum):
    APPEND_ONLY = "append_only"
    NONE = "none"
    READ_MOSTLY = "read_mostly"
    READ_ONLY_EXTERNAL = "read_only_external"
    REVERSIBLE = "reversible"
    REVERSIBLE_BY_ARCHIVE = "reversible_by_archive"
    REVERSIBLE_WITH_VERSION_CONTROL = "reversible_with_version_control"
    VARIABLE = "variable"


class ToolParallelSafety(StrEnum):
    SERIAL = "serial"
    SAFE = "safe"
    AGENT_SAFE = "agent_safe"


class ActionPolicyResult(StrEnum):
    ALLOW_AUDIT = "allow_audit"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class ToolContextRoute:
    """Routing affordance for lightweight context-reply lookups."""

    description: str
    domains: tuple[str, ...]
    scopes: tuple[str, ...] = ("narrow",)
    empty_result_policy: str = "answer_honestly"

    def __post_init__(self) -> None:
        description = str(self.description or "").strip()
        if not description:
            raise ValueError("Tool context routes must declare a description")
        object.__setattr__(self, "description", description)
        domains = _normalize_string_tuple(self.domains, field_name="context_route.domains", tool_name=description)
        if not domains:
            raise ValueError(f"Context route {description!r} must declare at least one domain")
        object.__setattr__(self, "domains", domains)
        scopes = _normalize_string_tuple(self.scopes, field_name="context_route.scopes", tool_name=description)
        object.__setattr__(self, "scopes", scopes or ("narrow",))
        object.__setattr__(self, "empty_result_policy", str(self.empty_result_policy or "answer_honestly").strip())

    def to_payload(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "domains": list(self.domains),
            "scopes": list(self.scopes),
            "empty_result_policy": self.empty_result_policy,
        }


def _enum_values(enum_type: type[StrEnum]) -> str:
    return ", ".join(member.value for member in enum_type)


def _normalize_enum(
    enum_type: type[EnumT],
    value: EnumT | str,
    field_name: str,
    *,
    tool_name: str,
) -> EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise ValueError(
            f"Invalid {field_name} for tool {tool_name!r}: {value!r}. "
            f"Expected one of: {_enum_values(enum_type)}"
        ) from exc


def _normalize_availability(
    value: tuple[ToolAvailability | str, ...] | list[ToolAvailability | str] | ToolAvailability | str,
    *,
    tool_name: str,
) -> tuple[ToolAvailability, ...]:
    raw_values: tuple[ToolAvailability | str, ...]
    if isinstance(value, str):
        raw_values = (value,)
    else:
        raw_values = tuple(value)
    if not raw_values:
        raise ValueError(f"Tool {tool_name!r} must declare at least one availability role")

    normalized: list[ToolAvailability] = []
    for raw in raw_values:
        member = _normalize_enum(
            ToolAvailability,
            raw,
            "availability",
            tool_name=tool_name,
        )
        if member not in normalized:
            normalized.append(member)
    return tuple(normalized)


def _normalize_string_tuple(value: Any, *, field_name: str, tool_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = (value,) if isinstance(value, str) else tuple(value)
    normalized: list[str] = []
    for raw in raw_values:
        item = str(raw).strip()
        if not item:
            raise ValueError(f"Tool {tool_name!r} has an empty {field_name} entry")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _normalize_context_route(value: Any, *, tool_name: str) -> ToolContextRoute | None:
    if value is None:
        return None
    if isinstance(value, ToolContextRoute):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"Tool {tool_name!r} context_route must be a mapping")
    return ToolContextRoute(
        description=str(value.get("description") or ""),
        domains=tuple(value.get("domains") or ()),
        scopes=tuple(value.get("scopes") or ("narrow",)),
        empty_result_policy=str(value.get("empty_result_policy") or "answer_honestly"),
    )


@dataclass(frozen=True)
class ToolRegistration:
    """Single source of truth for a tool's schema and runtime policy metadata."""

    name: str
    schema: Mapping[str, Any]
    handler: Callable[..., Any] | None = None
    toolset: str = "unknown"
    availability: tuple[ToolAvailability | str, ...] = (
        ToolAvailability.COORDINATOR,
        ToolAvailability.WORKER,
    )
    permission: ToolPermission | str = ToolPermission.READ
    risk_class: ToolRiskClass | str = ToolRiskClass.LOW
    side_effect_class: ToolSideEffectClass | str = ToolSideEffectClass.WRITE
    reversibility: ToolReversibility | str = ToolReversibility.NONE
    output_budget_chars: int = 10_000
    parallel_safety: ToolParallelSafety | str = ToolParallelSafety.SERIAL
    evidence_emitter: bool = False
    description: str = ""
    action_manifest: bool = False
    expected_effect: str | None = None
    context_route: ToolContextRoute | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "availability",
            _normalize_availability(self.availability, tool_name=self.name),
        )
        object.__setattr__(
            self,
            "permission",
            _normalize_enum(ToolPermission, self.permission, "permission", tool_name=self.name),
        )
        object.__setattr__(
            self,
            "risk_class",
            _normalize_enum(ToolRiskClass, self.risk_class, "risk_class", tool_name=self.name),
        )
        object.__setattr__(
            self,
            "side_effect_class",
            _normalize_enum(
                ToolSideEffectClass,
                self.side_effect_class,
                "side_effect_class",
                tool_name=self.name,
            ),
        )
        object.__setattr__(
            self,
            "reversibility",
            _normalize_enum(
                ToolReversibility,
                self.reversibility,
                "reversibility",
                tool_name=self.name,
            ),
        )
        object.__setattr__(
            self,
            "parallel_safety",
            _normalize_enum(
                ToolParallelSafety,
                self.parallel_safety,
                "parallel_safety",
                tool_name=self.name,
            ),
        )
        object.__setattr__(self, "output_budget_chars", int(self.output_budget_chars))
        if self.output_budget_chars <= 0:
            raise ValueError(f"Tool {self.name!r} must declare a positive output_budget_chars")
        if self.expected_effect is not None:
            object.__setattr__(self, "expected_effect", str(self.expected_effect))
        object.__setattr__(self, "context_route", _normalize_context_route(self.context_route, tool_name=self.name))

    def to_permission_payload(self) -> dict[str, Any]:
        """Return compact metadata suitable for context packs."""
        return {
            "toolset": self.toolset,
            "availability": [role.value for role in self.availability],
            "permission": self.permission.value,
            "risk_class": self.risk_class.value,
            "side_effect_class": self.side_effect_class.value,
            "reversibility": self.reversibility.value,
            "output_budget_chars": self.output_budget_chars,
            "parallel_safety": self.parallel_safety.value,
            "evidence_emitter": self.evidence_emitter,
            "action_manifest": self.action_manifest,
            "expected_effect": self.expected_effect,
            "context_route": self.context_route.to_payload() if self.context_route else None,
        }

    def to_action_policy(self) -> dict[str, str] | None:
        """Return concrete action policy metadata for side-effecting tools."""
        if not self.action_manifest:
            return None
        return {
            "risk": self.risk_class.value,
            "reversibility": self.reversibility.value,
            "expected_effect": self.expected_effect or self.description or self.name,
        }
