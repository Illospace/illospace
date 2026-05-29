"""Runtime capability manifests for Illo's agent-visible self model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUIDE_MAX_CHARS = 8_000


@dataclass(frozen=True)
class CapabilityManifest:
    key: str
    name: str
    category: str
    summary: str
    aliases: tuple[str, ...] = ()
    affordances: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    tool_details: tuple[Mapping[str, Any], ...] = ()
    status_check: Mapping[str, Any] | None = None
    setup: Mapping[str, Any] = field(default_factory=dict)
    source: str = "builtin"

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "summary": self.summary,
            "aliases": list(self.aliases),
            "affordances": list(self.affordances),
            "tools": list(self.tools),
            "tool_details": [dict(detail) for detail in self.tool_details],
            "status_check": dict(self.status_check or {}),
            "setup": dict(self.setup or {}),
            "source": self.source,
        }


def builtin_capability_manifests() -> list[CapabilityManifest]:
    return [
        CapabilityManifest(
            key="slack",
            name="Slack",
            category="external_surface",
            summary=(
                "Illo can participate in Slack conversations when a Slack source "
                "connection is registered for the workspace."
            ),
            aliases=("slack", "team chat", "chat teammate"),
            affordances=(
                "inspect connection health",
                "map Slack users to Illospace users",
                "read bounded Slack context for Slack-triggered runs",
                "reply to the originating Slack conversation",
            ),
            tools=("manage_slack", "read_slack_conversation", "post_slack_reply"),
            status_check={"tool": "manage_slack", "args": {"action": "status"}},
            setup={
                "mode": "guided_user_action",
                "agent_role": "check_status_collect_credentials_and_answer_questions",
                "credential_store": "Vault",
                "credentials": [
                    {
                        "key_name": "SLACK_BOT_TOKEN",
                        "description": "Slack bot token for the Illo app.",
                    },
                    {
                        "key_name": "SLACK_APP_TOKEN",
                        "description": "Slack app-level Socket Mode token for the Illo app.",
                    },
                ],
            },
        ),
    ]


def _coerce_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _coerce_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(str(item).strip() for item in value if str(item or "").strip())
    return ()


def _manifest_from_mapping(value: Mapping[str, Any], *, default_key: str | None = None, source: str) -> CapabilityManifest | None:
    key = _coerce_text(value.get("key") or default_key)
    if not key:
        return None
    name = _coerce_text(value.get("name"), key.replace("_", " ").replace("-", " ").title())
    return CapabilityManifest(
        key=key,
        name=name,
        category=_coerce_text(value.get("category"), "custom"),
        summary=_coerce_text(value.get("summary") or value.get("description"), f"{name} capability."),
        aliases=_coerce_text_tuple(value.get("aliases")),
        affordances=_coerce_text_tuple(value.get("affordances") or value.get("capabilities")),
        tools=_coerce_text_tuple(value.get("tools")),
        tool_details=tuple(
            dict(item) for item in value.get("tool_details", ())
            if isinstance(item, Mapping)
        ) if isinstance(value.get("tool_details"), Iterable) else (),
        status_check=value.get("status_check") if isinstance(value.get("status_check"), Mapping) else None,
        setup=value.get("setup") if isinstance(value.get("setup"), Mapping) else {},
        source=source,
    )


def custom_capability_manifests(*containers: Mapping[str, Any] | None) -> list[CapabilityManifest]:
    manifests: list[CapabilityManifest] = []
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for field_name in ("capability_manifests", "runtime_capabilities", "capabilities"):
            raw = container.get(field_name)
            if isinstance(raw, Mapping) and raw.get("key"):
                manifest = _manifest_from_mapping(raw, source=field_name)
                if manifest:
                    manifests.append(manifest)
            elif isinstance(raw, Mapping):
                for key, value in raw.items():
                    if isinstance(value, Mapping):
                        manifest = _manifest_from_mapping(value, default_key=str(key), source=field_name)
                        if manifest:
                            manifests.append(manifest)
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, Mapping):
                        manifest = _manifest_from_mapping(item, source=field_name)
                        if manifest:
                            manifests.append(manifest)
    return manifests


_FIRST_PARTY_CAPABILITY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "workspace_context",
        "name": "Workspace Context",
        "category": "core_workspace",
        "summary": "Illo can inspect current Illospace workspace context: roster, recent activity, projects, records, apps, cycles, and run provenance.",
        "aliases": ("workspace", "team activity", "workspace overview", "what can you see"),
        "tools": ("read_workspace_overview", "read_team_activity", "read_team_members", "query_workspace_data", "my_activity"),
    },
    {
        "key": "threads",
        "name": "Threads and Discussion",
        "category": "core_workspace",
        "summary": "Illo can work with Illospace Threads, AI Timeline messages, and Thread Discussion surfaces.",
        "aliases": ("thread", "threads", "discussion", "ai timeline", "ideas"),
        "tools": ("manage_idea", "read_thread_discussion", "post_thread_discussion_reply", "post_ai_timeline_message", "post_chat_message", "cortex_reply", "cortex_visual_reply"),
    },
    {
        "key": "domains",
        "name": "Domains",
        "category": "core_workspace",
        "summary": "Illo can inspect and mutate user-created structured workspace databases, schemas, records, and Domain audit events.",
        "aliases": ("domain", "domains", "workspace records", "structured records", "team database"),
        "tools": ("read_workspace_records", "manage_domain"),
    },
    {
        "key": "cycles",
        "name": "Cycles",
        "category": "core_workspace",
        "summary": "Illo can inspect and manage recurring workspace work such as scheduled prompts, check-ins, reports, and automation runs.",
        "aliases": ("cycle", "cycles", "recurring work", "automations", "scheduled work"),
        "tools": ("read_cycles", "manage_cycle"),
    },
    {
        "key": "project_context",
        "name": "Project Context",
        "category": "core_workspace",
        "summary": "Illo can inspect and manage durable project context profiles, connected resources, files, folders, repos, docs, and thread attachments.",
        "aliases": ("project", "projects", "repos", "files", "docs", "connected repo", "project context"),
        "tools": ("read_project_contexts", "manage_project"),
    },
    {
        "key": "workspace_apps",
        "name": "Workspace Apps",
        "category": "core_workspace",
        "summary": "Illo can inspect and manage generated workspace apps, dashboards, app metadata, and app-local state.",
        "aliases": ("apps", "dashboards", "workspace apps", "generated apps"),
        "tools": ("read_workspace_apps", "manage_workspace_app"),
    },
    {
        "key": "vault",
        "name": "Vault",
        "category": "security",
        "summary": "Illo can reason about Vault secret metadata, ask the user for missing credentials, and request task-scoped secret access without exposing raw values.",
        "aliases": ("secrets", "credentials", "tokens", "vault"),
        "tools": ("vault_inventory", "vault_secret_prompt", "brain_vault"),
    },
    {
        "key": "skills",
        "name": "Skills",
        "category": "agent_runtime",
        "summary": "Illo can inspect, load, create, update, archive, and package installed skills and skill assets.",
        "aliases": ("skill", "skills", "procedures", "agent skills"),
        "tools": ("brain_skills", "skill_view", "skill_asset", "manage_skill"),
    },
    {
        "key": "memory",
        "name": "Memory",
        "category": "agent_runtime",
        "summary": "Illo can search and write long-term semantic memories, lessons, facts, patterns, episodes, and guardrails.",
        "aliases": ("memory", "memories", "remember", "lessons", "guardrails"),
        "tools": ("brain_recall", "brain_encode", "brain_guardrails"),
    },
    {
        "key": "inbound_coordination",
        "name": "Inbound Coordination",
        "category": "integration_foundation",
        "summary": "Illo can inspect and manage inbound source connections, tokens, policies, replay, source cards, and Domain projections.",
        "aliases": ("inbound", "sources", "webhooks", "mcp", "personal agents", "external agents"),
        "tools": ("manage_inbound",),
    },
    {
        "key": "code_execution",
        "name": "Code and File Execution",
        "category": "runtime_surface",
        "summary": "Illo can inspect files and, when enabled for the run, write/edit files and execute shell, Python, or test commands in the available workspace.",
        "aliases": ("code", "files", "scripts", "commands", "terminal", "tests", "repository"),
        "tools": ("read_file", "search_files", "list_files", "write_file", "edit_file", "exec_command", "run_script", "test_runner", "project_context"),
    },
    {
        "key": "web_research",
        "name": "Web Research",
        "category": "external_read",
        "summary": "Illo can search and fetch external web content when web tools are available.",
        "aliases": ("web", "internet", "search", "fetch", "research"),
        "tools": ("web_search", "web_fetch"),
    },
    {
        "key": "browser_automation",
        "name": "Browser Automation",
        "category": "runtime_surface",
        "summary": "Illo can inspect and operate a live browser session when browser automation is exposed to the run.",
        "aliases": ("browser", "ui automation", "web app testing"),
        "tools": ("browser",),
    },
    {
        "key": "worker_coordination",
        "name": "Worker Coordination",
        "category": "agent_runtime",
        "summary": "Illo can spawn scoped worker runs for parallel or delegated investigation when the coordinator tool is available.",
        "aliases": ("workers", "spawn worker", "parallel agents"),
        "tools": ("spawn_worker",),
    },
    {
        "key": "runtime_self_context",
        "name": "Runtime and Self Context",
        "category": "agent_runtime",
        "summary": "Illo can inspect its runtime settings, capability registry, and verified source/install context.",
        "aliases": ("runtime", "self context", "who are you", "where are you installed", "models"),
        "tools": ("runtime_settings", "read_self_context", "read_capabilities"),
    },
    {
        "key": "deployment",
        "name": "Deployment",
        "category": "operations",
        "summary": "Illo can inspect or start the self-update deployment flow when the deployment management tool is available.",
        "aliases": ("deploy", "deployment", "update", "self update"),
        "tools": ("manage_deployment",),
    },
    {
        "key": "voice",
        "name": "Voice and Audio",
        "category": "media",
        "summary": "Illo can transcribe audio attachments when the voice runtime is configured.",
        "aliases": ("voice", "audio", "transcription", "voice notes"),
        "tools": ("transcribe_audio_attachment",),
    },
    {
        "key": "agent_personality",
        "name": "Agent Personality",
        "category": "agent_runtime",
        "summary": "Illo can inspect or update its private SOUL.md personality file when that management surface is exposed.",
        "aliases": ("soul", "personality", "agent behavior"),
        "tools": ("manage_soul",),
    },
)


def _tool_detail(name: str, registration: Any) -> dict[str, Any]:
    route = registration.context_route
    return {
        "name": name,
        "toolset": registration.toolset,
        "availability": [role.value for role in registration.availability],
        "permission": registration.permission.value,
        "risk_class": registration.risk_class.value,
        "side_effect_class": registration.side_effect_class.value,
        "reversibility": registration.reversibility.value,
        "expected_effect": registration.expected_effect,
        "context_domains": list(route.domains) if route is not None else [],
    }


def _tool_affordance(registration: Any) -> str:
    if registration.expected_effect:
        return str(registration.expected_effect)
    if registration.context_route is not None:
        return str(registration.context_route.description)
    return str(registration.description)


def registry_capability_manifests(
    *,
    available_tool_names: Iterable[str] | None = None,
) -> list[CapabilityManifest]:
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    registrations = all_tool_registrations()
    available = {str(name) for name in available_tool_names} if available_tool_names is not None else set(registrations)
    manifests: list[CapabilityManifest] = []
    for spec in _FIRST_PARTY_CAPABILITY_SPECS:
        tools = tuple(
            name
            for name in spec["tools"]
            if name in registrations and name in available
        )
        if not tools:
            continue
        details = tuple(_tool_detail(name, registrations[name]) for name in tools)
        affordances = tuple(_tool_affordance(registrations[name]) for name in tools)
        manifests.append(CapabilityManifest(
            key=str(spec["key"]),
            name=str(spec["name"]),
            category=str(spec["category"]),
            summary=str(spec["summary"]),
            aliases=_coerce_text_tuple(spec.get("aliases")),
            affordances=affordances,
            tools=tools,
            tool_details=details,
            setup={"mode": "built_in"},
            source="tool_registry",
        ))
    return manifests


def merge_capability_manifests(manifests: Iterable[CapabilityManifest]) -> list[CapabilityManifest]:
    merged: dict[str, CapabilityManifest] = {}
    for manifest in manifests:
        merged[manifest.key] = manifest
    return list(merged.values())


def _matches_query(manifest: CapabilityManifest, query: str) -> bool:
    haystack = " ".join((
        manifest.key,
        manifest.name,
        manifest.category,
        manifest.summary,
        " ".join(manifest.aliases),
        " ".join(manifest.affordances),
        " ".join(manifest.tools),
        " ".join(str(detail.get("expected_effect") or "") for detail in manifest.tool_details),
    )).lower()
    stopwords = {
        "a", "about", "add", "agent", "an", "app", "apps", "can",
        "capabilities", "capability", "configure", "connect", "connector",
        "connectors", "do", "enable", "for", "help", "i", "illo", "in",
        "install", "integrate", "integration", "integrations", "me", "my",
        "of", "on", "our", "please", "plugin", "plugins", "set", "setup",
        "the", "to", "tool", "tools", "up", "what", "which", "with", "you",
    }
    terms = [
        term
        for term in re.split(r"[^a-z0-9_/-]+", query.lower())
        if term and term not in stopwords
    ]
    if not terms:
        return True
    return any(term in haystack for term in terms)


def filter_capability_manifests(
    manifests: Iterable[CapabilityManifest],
    *,
    query: str | None = None,
    capability_key: str | None = None,
    category: str | None = None,
) -> list[CapabilityManifest]:
    key = _coerce_text(capability_key).lower()
    cat = _coerce_text(category).lower()
    q = _coerce_text(query).lower()
    result: list[CapabilityManifest] = []
    for manifest in manifests:
        if key and key not in {manifest.key.lower(), *(alias.lower() for alias in manifest.aliases)}:
            continue
        if cat and manifest.category.lower() != cat:
            continue
        if q and not _matches_query(manifest, q):
            continue
        result.append(manifest)
    return result


def load_setup_guide(manifest: CapabilityManifest) -> dict[str, Any] | None:
    setup = manifest.setup or {}
    inline_guide = _coerce_text(setup.get("guide_markdown") or setup.get("guide_content") or setup.get("guide"))
    if inline_guide:
        title = next((line.lstrip("#").strip() for line in inline_guide.splitlines() if line.startswith("#")), manifest.name)
        truncated = len(inline_guide) > _GUIDE_MAX_CHARS
        return {
            "ref": "inline",
            "available": True,
            "title": title,
            "content": inline_guide[:_GUIDE_MAX_CHARS],
            "truncated": truncated,
        }
    guide_ref = _coerce_text(setup.get("guide_ref"))
    if not guide_ref:
        return None
    path = (_REPO_ROOT / guide_ref).resolve()
    try:
        path.relative_to(_REPO_ROOT)
    except ValueError:
        return {"ref": guide_ref, "available": False, "error": "guide_ref escapes repository root"}
    if not path.exists() or not path.is_file():
        return {"ref": guide_ref, "available": False, "error": "guide_ref not found"}
    text = path.read_text(encoding="utf-8")
    title = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")), manifest.name)
    truncated = len(text) > _GUIDE_MAX_CHARS
    return {
        "ref": guide_ref,
        "available": True,
        "title": title,
        "content": text[:_GUIDE_MAX_CHARS],
        "truncated": truncated,
    }


__all__ = [
    "CapabilityManifest",
    "builtin_capability_manifests",
    "custom_capability_manifests",
    "filter_capability_manifests",
    "load_setup_guide",
    "merge_capability_manifests",
    "registry_capability_manifests",
]
