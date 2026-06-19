"""Thread-scoped interactive artifact publishing."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.idea import Idea
from brain.systems.cortex.thread_links import thread_route_for_id, thread_url_for_route
from brain.systems.workspace_apps.compiler import compile_workspace_app_input
from brain.systems.workspace_apps.contracts import (
    APP_CAPSULE_RENDERER_KEY,
    APP_CAPSULE_SOURCE_KIND,
    APP_KIT_NAME,
    CONTRACT_VERSION,
)
from brain.systems.workspace_apps.service import (
    WorkspaceAppNotFound,
    a_create_app,
    a_get_app,
    a_serialize_app,
    a_update_app,
    slugify,
)

THREAD_ARTIFACT_SOURCE = "thread_artifact"
DEFAULT_THREAD_ARTIFACT_KIND = "interactive"


class ThreadArtifactError(ValueError):
    """Raised when a thread artifact cannot be published."""


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_kind(value: Any) -> str:
    text = slugify(str(value or DEFAULT_THREAD_ARTIFACT_KIND))
    return text or DEFAULT_THREAD_ARTIFACT_KIND


def _thread_artifact_key(thread_id: str, title: str, artifact_kind: str) -> str:
    thread_part = slugify(str(thread_id))[:28] or "thread"
    title_part = slugify(title)[:48] or "artifact"
    kind_part = slugify(artifact_kind)[:24] or DEFAULT_THREAD_ARTIFACT_KIND
    return f"thread-{thread_part}-{kind_part}-{title_part}"[:100]


def _default_manifest(thread_id: str, artifact_kind: str, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    next_manifest = dict(manifest or {})
    next_manifest.setdefault("contract_version", CONTRACT_VERSION)
    next_manifest.setdefault("state_key", f"thread-artifact-{thread_id}"[:120])
    next_manifest.setdefault("data_plan", {"mode": "capability", "bindings": {}})
    next_manifest.setdefault(
        "design_contract",
        {"kit": APP_KIT_NAME, "theme_modes": ["dark", "light"]},
    )
    next_manifest["thread_artifact"] = {
        **dict(next_manifest.get("thread_artifact") or {}),
        "thread_id": str(thread_id),
        "kind": artifact_kind,
        "source": THREAD_ARTIFACT_SOURCE,
    }
    return next_manifest


def _default_visual_spec(title: str, artifact_kind: str, visual_spec: Mapping[str, Any] | None) -> dict[str, Any]:
    next_visual = dict(visual_spec or {})
    next_visual.setdefault(
        "thumbnail",
        {
            "label": title,
            "status": "Interactive",
            "secondary": artifact_kind.replace("-", " ").title(),
        },
    )
    return next_visual


def _artifact_metadata(
    *,
    thread_id: str,
    artifact_kind: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    next_metadata["source"] = THREAD_ARTIFACT_SOURCE
    next_metadata["artifact_scope"] = "thread"
    next_metadata["thread_id"] = str(thread_id)
    next_metadata["artifact_kind"] = artifact_kind
    next_metadata["thread_artifact"] = {
        **dict(next_metadata.get("thread_artifact") or {}),
        "thread_id": str(thread_id),
        "kind": artifact_kind,
        "source": THREAD_ARTIFACT_SOURCE,
    }
    return next_metadata


async def _require_thread(session: AsyncSession, *, org_id: str, thread_id: str) -> None:
    idea_id = await session.scalar(
        select(Idea.id).where(
            Idea.id == str(thread_id),
            Idea.org_id == str(org_id),
            Idea.archived_at.is_(None),
        )
    )
    if idea_id is None:
        raise ThreadArtifactError("Thread not found for this workspace")


async def publish_thread_artifact_app(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str | None,
    thread_id: str,
    title: str,
    source_code: str,
    description: str | None = None,
    artifact_kind: str | None = None,
    key: str | None = None,
    app_id: str | None = None,
    update_existing: bool = True,
    manifest: Mapping[str, Any] | None = None,
    visual_spec: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a thread-scoped app-capsule artifact."""

    clean_thread_id = _clean_text(thread_id)
    clean_title = _clean_text(title)
    clean_source = str(source_code or "").strip()
    if not clean_thread_id:
        raise ThreadArtifactError("thread_id is required")
    if not clean_title:
        raise ThreadArtifactError("title is required")
    if not clean_source:
        raise ThreadArtifactError("source_code is required")

    await _require_thread(session, org_id=org_id, thread_id=clean_thread_id)
    clean_kind = _clean_kind(artifact_kind)
    app_key = _clean_text(key) or _thread_artifact_key(clean_thread_id, clean_title, clean_kind)
    route = thread_route_for_id(clean_thread_id)
    artifact_route = f"{route}?app={app_id or app_key}"
    thread_url = thread_url_for_route(route)

    compiled = compile_workspace_app_input(
        action="create",
        name=clean_title,
        key=app_key,
        renderer_key=APP_CAPSULE_RENDERER_KEY,
        source_kind=APP_CAPSULE_SOURCE_KIND,
        source_code=clean_source,
        manifest=_default_manifest(clean_thread_id, clean_kind, manifest),
        visual_spec=_default_visual_spec(clean_title, clean_kind, visual_spec),
        metadata=_artifact_metadata(thread_id=clean_thread_id, artifact_kind=clean_kind, metadata=metadata),
        initial_state=dict(initial_state or {}) or None,
    )

    action = "created"
    target_app_id = _clean_text(app_id)
    target_key = app_key
    if update_existing:
        try:
            existing = await a_get_app(session, org_id, target_app_id, key=None if target_app_id else target_key)
        except WorkspaceAppNotFound:
            existing = None
        if existing is not None:
            target_app_id = str(existing.id)

    if target_app_id:
        app = await a_update_app(
            session,
            org_id=org_id,
            app_id=target_app_id,
            name=clean_title,
            description=description,
            renderer_key=compiled.renderer_key,
            source_kind=compiled.source_kind,
            source_code=compiled.source_code,
            manifest=compiled.manifest,
            visual_spec=compiled.visual_spec,
            metadata=compiled.metadata,
            anchor_user_id=user_id,
            updated_by_user_id=user_id,
        )
        action = "updated"
    else:
        app = await a_create_app(
            session,
            org_id=org_id,
            key=target_key,
            name=clean_title,
            description=description,
            renderer_key=compiled.renderer_key,
            source_kind=compiled.source_kind,
            source_code=compiled.source_code,
            manifest=compiled.manifest or {},
            visual_spec=compiled.visual_spec or {},
            metadata=compiled.metadata or {},
            created_by_user_id=user_id,
            anchor_user_id=user_id,
            initial_state=dict(initial_state or {}) or None,
            state_key=str((compiled.manifest or {}).get("state_key") or "default"),
        )

    serialized = await a_serialize_app(session, app)
    artifact_route = f"{route}?app={serialized['id']}"
    return {
        "action": action,
        "thread_id": clean_thread_id,
        "thread_route": route,
        "thread_url": thread_url,
        "artifact_route": artifact_route,
        "artifact_url": thread_url_for_route(artifact_route),
        "artifact_kind": clean_kind,
        "app_id": serialized["id"],
        "app_key": serialized["key"],
        "app_name": serialized["name"],
        "version": (serialized.get("active_version") or {}).get("version"),
        "renderer_key": serialized["renderer_key"],
        "source_kind": (serialized.get("active_version") or {}).get("source_kind"),
        "app": serialized,
        "compiler_repairs": list(compiled.repairs),
        "warnings": list(compiled.warnings),
    }


__all__ = [
    "DEFAULT_THREAD_ARTIFACT_KIND",
    "THREAD_ARTIFACT_SOURCE",
    "ThreadArtifactError",
    "publish_thread_artifact_app",
]
