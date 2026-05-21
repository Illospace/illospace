"""Typed Project workspace mount manifest primitives.

The materializer owns how resources become local files. This module owns the
pure, agent-facing projection of those resources once a concrete workspace path
exists: stable mount paths, draft identity, and mount-to-workspace resolution.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import posixpath

from brain.systems.cortex.project_context.resources import ProjectResource


PROJECT_CONTEXT_DIR = ".illo-project-context"
PROJECT_CONTEXT_LOCAL_DIR = "local"
PROJECT_KEY_FIELDS = (
    "id",
    "project_id",
    "profile_id",
    "selected_profile_id",
    "slug",
    "selected_profile_slug",
    "project_key",
)
FILE_RESOURCE_KINDS = {"file", "doc", "document"}
ProjectResourceLike = Mapping[str, Any] | ProjectResource


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_segment(value: Any, *, fallback: str) -> str:
    text = _clean_text(value) or fallback
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in text.strip("/"))[:120]
    return safe or fallback


def _project_key_from_context(project_context: Mapping[str, Any] | None) -> str | None:
    context = project_context if isinstance(project_context, Mapping) else {}
    for key in PROJECT_KEY_FIELDS:
        value = _clean_text(context.get(key))
        if value:
            return _safe_segment(value, fallback="project")
    return None


def _normalise_project_context(project_context: Mapping[str, Any] | None) -> Mapping[str, Any]:
    context = project_context if isinstance(project_context, Mapping) else {}
    snapshot = context.get("project_context_snapshot")
    return snapshot if isinstance(snapshot, Mapping) else context


def _normalise_mount_path(value: Any, *, fallback: str) -> str:
    text = (_clean_text(value) or fallback).replace("\\", "/")
    if text in {"", "."}:
        text = fallback
    if not text.startswith("/"):
        text = "/" + text
    normalised = posixpath.normpath(text)
    if normalised == ".":
        return "/"
    return normalised if normalised.startswith("/") else f"/{normalised}"


def _append_mount_suffix(path: str, suffix: int) -> str:
    if path == "/":
        return f"/resource-{suffix}"
    parent, leaf = path.rsplit("/", 1)
    if "." in leaf and not leaf.startswith("."):
        stem, extension = leaf.rsplit(".", 1)
        leaf = f"{stem}-{suffix}.{extension}"
    else:
        leaf = f"{leaf}-{suffix}"
    return f"{parent}/{leaf}" if parent else f"/{leaf}"


def _disambiguate_mount_paths(paths: Sequence[str]) -> list[str]:
    counts = Counter(paths)
    reserved = set(paths)
    used: set[str] = set()
    disambiguated: list[str] = []

    for path in paths:
        if path not in used:
            used.add(path)
            disambiguated.append(path)
            continue

        suffix = 2
        while True:
            candidate = _append_mount_suffix(path, suffix)
            if candidate not in used and candidate not in reserved:
                used.add(candidate)
                disambiguated.append(candidate)
                break
            suffix += 1

    assert len(disambiguated) == len(paths)
    assert len(set(disambiguated)) == len(paths) or not counts
    return disambiguated


def _resource_mapping(raw: ProjectResourceLike, *, index: int) -> dict[str, Any]:
    if isinstance(raw, ProjectResource):
        return raw.to_dict()
    return ProjectResource.from_mapping(raw, index=index).to_dict()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resource_kind(resource: Mapping[str, Any]) -> str:
    return (_clean_text(resource.get("kind") or resource.get("type") or resource.get("resource_type")) or "resource").lower()


def _materialization(resource: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(resource.get("materialization"))


def _resource_workspace_path(resource: Mapping[str, Any]) -> str | None:
    materialization = _materialization(resource)
    return (
        _clean_text(materialization.get("workspace_path"))
        or _clean_text(resource.get("workspace_path"))
        or _clean_text(materialization.get("path"))
        or _clean_text(resource.get("path") or resource.get("local_path") or resource.get("workspace_root"))
    )


def _resource_path(resource: Mapping[str, Any], workspace_path: str | None) -> str | None:
    materialization = _materialization(resource)
    return (
        _clean_text(materialization.get("path"))
        or _clean_text(resource.get("path") or resource.get("local_path"))
        or workspace_path
    )


def _source_path(resource: Mapping[str, Any]) -> str | None:
    materialization = _materialization(resource)
    return _clean_text(materialization.get("source_path")) or _clean_text(resource.get("source_path"))


def _resource_label(resource: Mapping[str, Any], workspace_path: str | None, *, index: int) -> str:
    return (
        _clean_text(resource.get("label"))
        or _clean_text(resource.get("name"))
        or _clean_text(resource.get("repo"))
        or _clean_text(resource.get("id"))
        or (Path(workspace_path).name if workspace_path else None)
        or f"resource-{index + 1}"
    )


def _mount_path_candidate(resource: Mapping[str, Any], workspace_path: str | None, *, index: int) -> str:
    candidate = (
        _clean_text(resource.get("mount_path"))
        or _clean_text(resource.get("project_path"))
        or _clean_text(resource.get("repo"))
        or _clean_text(resource.get("name"))
        or _clean_text(resource.get("label"))
        or (Path(workspace_path).name if workspace_path else None)
        or _clean_text(resource.get("id"))
        or f"resource-{index + 1}"
    )
    return _normalise_mount_path(candidate, fallback=f"/resource-{index + 1}")


def _normalise_agent_path(path: Any) -> str | None:
    text = _clean_text(path)
    if not text:
        return None
    return _normalise_mount_path(text, fallback="/")


def _relative_agent_path(agent_path: str, mount_path: str) -> str | None:
    if mount_path == "/":
        return agent_path.lstrip("/")
    if agent_path == mount_path:
        return ""
    prefix = mount_path.rstrip("/") + "/"
    if agent_path.startswith(prefix):
        return agent_path[len(prefix):]
    return None


def _join_workspace_path(workspace_path: str, relative_path: str) -> str:
    if not relative_path:
        return workspace_path
    return str(Path(workspace_path).joinpath(*relative_path.split("/")))


@dataclass(frozen=True)
class ThreadDraftIdentity:
    """Stable local draft location for one Project resource inside a thread root."""

    thread_workspace_root: str
    resource_key: str
    project_key: str | None = None
    source_path: str | None = None
    resource_kind: str = "resource"

    @classmethod
    def from_project_resource(
        cls,
        resource: ProjectResourceLike,
        *,
        thread_workspace_root: str | Path,
        project_context: Mapping[str, Any] | None = None,
        index: int = 0,
    ) -> "ThreadDraftIdentity":
        normalised = _resource_mapping(resource, index=index)
        source_path = _source_path(normalised) or _clean_text(normalised.get("path") or normalised.get("local_path"))
        source_name = Path(source_path).name if source_path else None
        resource_key = _safe_segment(
            normalised.get("id")
            or normalised.get("mount_path")
            or normalised.get("project_path")
            or normalised.get("name")
            or normalised.get("label")
            or source_name
            or f"resource-{index + 1}",
            fallback=f"resource-{index + 1}",
        )
        return cls(
            thread_workspace_root=str(Path(thread_workspace_root).expanduser()),
            project_key=_project_key_from_context(_normalise_project_context(project_context)),
            resource_key=resource_key,
            source_path=source_path,
            resource_kind=_resource_kind(normalised),
        )

    @property
    def draft_parent_path(self) -> str:
        parent = Path(self.thread_workspace_root) / PROJECT_CONTEXT_DIR / PROJECT_CONTEXT_LOCAL_DIR
        if self.project_key:
            parent = parent / self.project_key
        return str(parent)

    @property
    def draft_workspace_path(self) -> str:
        return str(Path(self.draft_parent_path) / self.resource_key)

    @property
    def draft_resource_path(self) -> str:
        if self.resource_kind in FILE_RESOURCE_KINDS and self.source_path:
            return str(Path(self.draft_workspace_path) / Path(self.source_path).name)
        return self.draft_workspace_path

    def to_dict(self) -> dict[str, str]:
        payload = {
            "thread_workspace_root": self.thread_workspace_root,
            "project_key": self.project_key,
            "resource_key": self.resource_key,
            "source_path": self.source_path,
            "resource_kind": self.resource_kind,
            "draft_workspace_path": self.draft_workspace_path,
            "draft_resource_path": self.draft_resource_path,
        }
        return {key: value for key, value in payload.items() if value}


@dataclass(frozen=True)
class ProjectMount:
    """One agent-facing Project mount backed by a materialized workspace path."""

    id: str
    resource_id: str
    kind: str
    mount_path: str
    workspace_path: str
    resource_path: str | None = None
    source_path: str | None = None
    original_mount_path: str | None = None
    label: str | None = None
    repo: str | None = None
    draft_identity: ThreadDraftIdentity | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_project_resource(
        cls,
        resource: ProjectResourceLike,
        *,
        mount_path: str,
        original_mount_path: str,
        project_context: Mapping[str, Any] | None = None,
        thread_workspace_root: str | Path | None = None,
        index: int = 0,
    ) -> "ProjectMount":
        normalised = _resource_mapping(resource, index=index)
        workspace_path = _resource_workspace_path(normalised)
        if not workspace_path:
            raise ValueError("Project resource is missing a materialized workspace path")

        resource_path = _resource_path(normalised, workspace_path)
        source_path = _source_path(normalised)
        draft_identity = None
        if thread_workspace_root is not None:
            draft_identity = ThreadDraftIdentity.from_project_resource(
                normalised,
                thread_workspace_root=thread_workspace_root,
                project_context=project_context,
                index=index,
            )
        resource_id = _clean_text(normalised.get("id")) or f"resource-{index + 1}"
        return cls(
            id=mount_path,
            resource_id=resource_id,
            kind=_resource_kind(normalised),
            mount_path=mount_path,
            workspace_path=workspace_path,
            resource_path=resource_path,
            source_path=source_path,
            original_mount_path=original_mount_path,
            label=_resource_label(normalised, workspace_path, index=index),
            repo=_clean_text(normalised.get("repo")),
            draft_identity=draft_identity,
            metadata={
                "materialization": dict(_materialization(normalised)),
            }
            if _materialization(normalised)
            else {},
        )

    @property
    def name(self) -> str:
        return self.mount_path

    @property
    def agent_path(self) -> str:
        return self.mount_path

    @property
    def is_file_mount(self) -> bool:
        return self.kind in FILE_RESOURCE_KINDS and bool(self.resource_path)

    def to_workspace_entry(self) -> dict[str, str]:
        return {"name": self.mount_path, "path": self.workspace_path}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "resource_id": self.resource_id,
            "kind": self.kind,
            "mount_path": self.mount_path,
            "workspace_path": self.workspace_path,
            "resource_path": self.resource_path,
            "source_path": self.source_path,
            "original_mount_path": self.original_mount_path,
            "label": self.label,
            "repo": self.repo,
            "metadata": dict(self.metadata),
        }
        if self.draft_identity is not None:
            payload["draft_identity"] = self.draft_identity.to_dict()
        return payload

    def contains_agent_path(self, agent_path: str) -> bool:
        normalised = _normalise_agent_path(agent_path)
        return bool(normalised is not None and _relative_agent_path(normalised, self.mount_path) is not None)

    def resolve_agent_path(self, agent_path: str) -> str | None:
        normalised = _normalise_agent_path(agent_path)
        if normalised is None:
            return None
        relative_path = _relative_agent_path(normalised, self.mount_path)
        if relative_path is None:
            return None
        if not relative_path:
            return self.resource_path if self.is_file_mount and self.resource_path else self.workspace_path
        if self.is_file_mount:
            return None
        return _join_workspace_path(self.workspace_path, relative_path)


@dataclass(frozen=True)
class ProjectWorkspaceManifest:
    """Typed, agent-facing view of all materialized Project mounts."""

    mounts: tuple[ProjectMount, ...] = field(default_factory=tuple)
    project_key: str | None = None
    project_id: str | None = None
    workspace_root: str | None = None

    @classmethod
    def from_project_context(
        cls,
        project_context: Mapping[str, Any] | None,
        *,
        workspaces: Sequence[Mapping[str, Any] | str] | None = None,
        thread_workspace_root: str | Path | None = None,
    ) -> "ProjectWorkspaceManifest":
        context = project_context if isinstance(project_context, Mapping) else {}
        snapshot = _normalise_project_context(context)
        resource_mounts = _mounts_from_resources(
            snapshot.get("resources") if isinstance(snapshot.get("resources"), Sequence) else [],
            project_context=snapshot,
            thread_workspace_root=thread_workspace_root,
        )
        workspace_mounts = _mounts_from_workspace_entries(
            workspaces if workspaces is not None else context.get("workspaces"),
            existing_paths={mount.workspace_path for mount in resource_mounts},
            existing_mount_paths=[mount.mount_path for mount in resource_mounts],
        )
        mounts = tuple([*resource_mounts, *workspace_mounts])
        workspace_root = (
            _clean_text(context.get("resolved_workspace_root"))
            or _clean_text(context.get("workspace_root"))
            or (mounts[0].workspace_path if mounts else None)
        )
        return cls(
            mounts=mounts,
            project_key=_project_key_from_context(snapshot),
            project_id=_clean_text(snapshot.get("id") or snapshot.get("project_id") or snapshot.get("profile_id")),
            workspace_root=workspace_root,
        )

    @classmethod
    def from_workspace_ref(
        cls,
        workspace_ref: Mapping[str, Any] | None,
        *,
        thread_workspace_root: str | Path | None = None,
    ) -> "ProjectWorkspaceManifest":
        return cls.from_project_context(workspace_ref, thread_workspace_root=thread_workspace_root)

    @property
    def allowed_workspaces(self) -> list[dict[str, str]]:
        return [mount.to_workspace_entry() for mount in self.mounts]

    def mount_for_agent_path(self, agent_path: str) -> ProjectMount | None:
        normalised = _normalise_agent_path(agent_path)
        if normalised is None:
            return None
        candidates = sorted(self.mounts, key=lambda mount: len(mount.mount_path), reverse=True)
        for mount in candidates:
            if _relative_agent_path(normalised, mount.mount_path) is not None:
                return mount
        return None

    def resolve_agent_path(self, agent_path: str) -> str | None:
        mount = self.mount_for_agent_path(agent_path)
        return mount.resolve_agent_path(agent_path) if mount else None

    def to_workspace_ref(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"workspaces": self.allowed_workspaces}
        if self.workspace_root:
            payload["workspace_root"] = self.workspace_root
            payload["resolved_workspace_root"] = self.workspace_root
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_key": self.project_key,
            "project_id": self.project_id,
            "workspace_root": self.workspace_root,
            "workspaces": self.allowed_workspaces,
            "mounts": [mount.to_dict() for mount in self.mounts],
        }


def _mounts_from_resources(
    resources: Any,
    *,
    project_context: Mapping[str, Any] | None,
    thread_workspace_root: str | Path | None,
) -> list[ProjectMount]:
    if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes)):
        return []

    candidates: list[tuple[int, Mapping[str, Any], str]] = []
    for index, raw_resource in enumerate(resources):
        if not isinstance(raw_resource, (Mapping, ProjectResource)):
            continue
        resource = _resource_mapping(raw_resource, index=index)
        workspace_path = _resource_workspace_path(resource)
        if not workspace_path:
            continue
        candidates.append((index, resource, _mount_path_candidate(resource, workspace_path, index=index)))

    disambiguated = _disambiguate_mount_paths([candidate for _, _, candidate in candidates])
    mounts: list[ProjectMount] = []
    for (index, resource, original_mount_path), mount_path in zip(candidates, disambiguated):
        mounts.append(
            ProjectMount.from_project_resource(
                resource,
                mount_path=mount_path,
                original_mount_path=original_mount_path,
                project_context=project_context,
                thread_workspace_root=thread_workspace_root,
                index=index,
            )
        )
    return mounts


def _mounts_from_workspace_entries(
    workspaces: Any,
    *,
    existing_paths: set[str],
    existing_mount_paths: Sequence[str],
) -> list[ProjectMount]:
    if not isinstance(workspaces, Sequence) or isinstance(workspaces, (str, bytes)):
        return []

    raw_mounts: list[tuple[int, Mapping[str, Any], str, str]] = []
    for index, item in enumerate(workspaces):
        if isinstance(item, str):
            workspace_path = _clean_text(item)
            label = Path(workspace_path).name if workspace_path else f"workspace-{index + 1}"
            workspace = {"path": workspace_path, "name": label}
        elif isinstance(item, Mapping):
            workspace = item
            workspace_path = _clean_text(workspace.get("path"))
        else:
            continue
        if not workspace_path or workspace_path in existing_paths:
            continue
        mount_path = _normalise_mount_path(
            workspace.get("mount_path") or workspace.get("name") or workspace.get("label") or Path(workspace_path).name,
            fallback=f"/workspace-{index + 1}",
        )
        raw_mounts.append((index, workspace, workspace_path, mount_path))

    disambiguated = _disambiguate_mount_paths([
        *existing_mount_paths,
        *[mount_path for _, _, _, mount_path in raw_mounts],
    ])[len(existing_mount_paths):]
    mounts: list[ProjectMount] = []
    for (index, workspace, workspace_path, original_mount_path), mount_path in zip(raw_mounts, disambiguated):
        resource_id = f"workspace-{index + 1}"
        mounts.append(
            ProjectMount(
                id=mount_path,
                resource_id=resource_id,
                kind="workspace",
                mount_path=mount_path,
                workspace_path=workspace_path,
                resource_path=workspace_path,
                original_mount_path=original_mount_path,
                label=_clean_text(workspace.get("label") or workspace.get("name")) or Path(workspace_path).name,
            )
        )
    return mounts


def normalize_project_workspace_manifest(
    project_context: Mapping[str, Any] | None,
    *,
    workspaces: Sequence[Mapping[str, Any] | str] | None = None,
    thread_workspace_root: str | Path | None = None,
) -> ProjectWorkspaceManifest:
    return ProjectWorkspaceManifest.from_project_context(
        project_context,
        workspaces=workspaces,
        thread_workspace_root=thread_workspace_root,
    )


def build_project_workspace_manifest_contract(project_context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the durable, agent-facing mount contract for a Project root.

    Runtime workspace paths are added by materialization. The persisted contract
    records the stable mount identities so Projects are not just loose resource
    blobs before a thread draft exists.
    """

    snapshot = _normalise_project_context(project_context)
    resources = snapshot.get("resources") if isinstance(snapshot.get("resources"), Sequence) else []
    candidates: list[tuple[int, Mapping[str, Any], str, str | None]] = []
    for index, raw_resource in enumerate(resources):
        if not isinstance(raw_resource, (Mapping, ProjectResource)):
            continue
        resource = _resource_mapping(raw_resource, index=index)
        workspace_path = _resource_workspace_path(resource)
        candidates.append((index, resource, _mount_path_candidate(resource, workspace_path, index=index), workspace_path))

    disambiguated = _disambiguate_mount_paths([candidate for _, _, candidate, _ in candidates])
    mounts: list[dict[str, Any]] = []
    for (index, resource, original_mount_path, workspace_path), mount_path in zip(candidates, disambiguated):
        mount: dict[str, Any] = {
            "id": mount_path,
            "resource_id": _clean_text(resource.get("id")) or f"resource-{index + 1}",
            "kind": _resource_kind(resource),
            "mount_path": mount_path,
            "original_mount_path": original_mount_path,
            "label": _resource_label(resource, workspace_path, index=index),
        }
        for key in ("repo", "uri", "path", "source_path"):
            value = _clean_text(resource.get(key))
            if value:
                mount[key] = value
        if workspace_path:
            mount["workspace_path"] = workspace_path
        mounts.append(mount)

    return {
        "schema_version": 1,
        "project_key": _project_key_from_context(snapshot),
        "project_id": _clean_text(snapshot.get("id") or snapshot.get("project_id") or snapshot.get("profile_id")),
        "mounts": mounts,
    }


def attach_project_workspace_manifest_contract(project_context: Mapping[str, Any] | None) -> dict[str, Any]:
    context = dict(project_context or {})
    context["project_workspace_manifest"] = build_project_workspace_manifest_contract(context)
    return context


def resolve_project_mount_path(
    project_context: Mapping[str, Any] | None,
    agent_path: str,
    *,
    workspaces: Sequence[Mapping[str, Any] | str] | None = None,
) -> str | None:
    return normalize_project_workspace_manifest(project_context, workspaces=workspaces).resolve_agent_path(agent_path)


__all__ = [
    "ProjectMount",
    "ProjectWorkspaceManifest",
    "ThreadDraftIdentity",
    "attach_project_workspace_manifest_contract",
    "build_project_workspace_manifest_contract",
    "normalize_project_workspace_manifest",
    "resolve_project_mount_path",
]
