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
            "status_check": dict(self.status_check or {}),
            "setup": dict(self.setup or {}),
            "source": self.source,
        }


def builtin_capability_manifests() -> list[CapabilityManifest]:
    return [
        CapabilityManifest(
            key="slack",
            name="Slack",
            category="communication",
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
]
