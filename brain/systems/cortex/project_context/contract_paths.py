"""File-contract path resolution for materialized Project Context resources."""
from __future__ import annotations

import os
import re

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)


@dataclass(frozen=True)
class FileContractResolution:
    expected_path: str
    resolved_path: str
    candidate_paths: list[str] = field(default_factory=list)
    existing_path: str | None = None
    resolution_source: str = "workspace"
    matching_artifacts: list[dict[str, Any]] = field(default_factory=list)


def resolve_file_contract_path(
    expected_path: str,
    run: Any,
    *,
    target_workspace_root: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> FileContractResolution:
    """Resolve file/document contract evidence across workspace and Project Context roots."""

    candidate_paths = _contract_file_candidate_paths(expected_path, run, target_workspace_root)
    resolved = candidate_paths[0] if candidate_paths else _resolve_contract_path(
        expected_path,
        getattr(run, "worktree_path", None) or target_workspace_root,
    )
    existing_path = next((candidate for candidate in candidate_paths if os.path.exists(candidate)), None)
    if existing_path:
        return FileContractResolution(
            expected_path=expected_path,
            resolved_path=existing_path,
            candidate_paths=candidate_paths,
            existing_path=existing_path,
            resolution_source="project_context" if path_in_project_context_roots(existing_path, run) else "workspace",
        )
    matching_artifacts = _matching_file_contract_artifacts(
        artifacts or [],
        expected_path,
        candidate_paths,
        run,
        target_workspace_root,
    )
    if matching_artifacts:
        return FileContractResolution(
            expected_path=expected_path,
            resolved_path=resolved,
            candidate_paths=candidate_paths,
            resolution_source="execution_artifact",
            matching_artifacts=matching_artifacts,
        )
    return FileContractResolution(
        expected_path=expected_path,
        resolved_path=resolved,
        candidate_paths=candidate_paths,
    )


def project_context_roots(run: Any) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_root(path: Any, aliases: Iterable[Any] = ()) -> None:
        if not isinstance(path, str) or not path.strip():
            return
        expanded = os.path.abspath(os.path.expanduser(path.strip()))
        real = os.path.realpath(expanded)
        if real in seen:
            return
        seen.add(real)
        alias_set = {
            alias
            for alias in (_normalize_contract_relative_path(item) for item in aliases)
            if alias
        }
        base_name = os.path.basename(expanded.rstrip(os.sep))
        parent_name = os.path.basename(os.path.dirname(expanded.rstrip(os.sep)))
        if base_name:
            alias_set.add(base_name)
        if parent_name and base_name:
            alias_set.add(f"{parent_name}/{base_name}")
        roots.append({"path": expanded, "aliases": alias_set})

    target_metadata = getattr(run, "target_metadata", None)
    if isinstance(target_metadata, Mapping):
        snapshot = target_metadata.get("project_context_snapshot")
        if isinstance(snapshot, Mapping):
            for resource in snapshot.get("resources") or []:
                if not isinstance(resource, Mapping):
                    continue
                add_root(
                    resource.get("path"),
                    (
                        resource.get("repo"),
                        resource.get("name"),
                        resource.get("uri"),
                        resource.get("url"),
                        resource.get("remote"),
                        resource.get("repo_url"),
                        _github_repo_slug_from_text(resource.get("repo")),
                        _github_repo_slug_from_text(resource.get("url")),
                        _github_repo_slug_from_text(resource.get("remote")),
                        _github_repo_slug_from_text(resource.get("repo_url")),
                    ),
                )

    metadata = getattr(run, "metadata_", None)
    if isinstance(metadata, Mapping):
        for workspace in metadata.get("workspaces") or []:
            if isinstance(workspace, Mapping):
                add_root(workspace.get("path"), (workspace.get("name"), _github_repo_slug_from_text(workspace.get("name"))))
            elif isinstance(workspace, str):
                add_root(workspace, ())

    return roots


def path_in_project_context_roots(path: str, run: Any) -> bool:
    return any(_relative_path_if_inside(path, root_info["path"]) is not None for root_info in project_context_roots(run))


def _contract_file_candidate_paths(
    expected_path: str,
    run: Any,
    target_workspace_root: str | None,
) -> list[str]:
    candidates: list[str] = []
    workspace_roots = [
        str(root).strip()
        for root in (getattr(run, "worktree_path", None), target_workspace_root)
        if str(root or "").strip()
    ]
    for root in workspace_roots:
        _append_unique_path(candidates, _resolve_contract_path(expected_path, root))
    if os.path.isabs(expected_path):
        _append_unique_path(candidates, expected_path)
        return candidates

    expected_relative = _normalize_contract_relative_path(expected_path)
    if not expected_relative:
        return candidates
    for root_info in project_context_roots(run):
        root = root_info["path"]
        if not root:
            continue
        if os.path.isfile(root):
            if _project_file_resource_matches_expected(root, root_info["aliases"], expected_relative):
                _append_unique_path(candidates, root)
            continue
        for relative in _project_context_relative_candidates(expected_relative, root_info["aliases"]):
            candidate = _safe_join_contract_path(root, relative)
            if candidate:
                _append_unique_path(candidates, candidate)
    return candidates


def _matching_file_contract_artifacts(
    artifacts: list[dict[str, Any]],
    expected_path: str,
    candidate_paths: list[str],
    run: Any,
    target_workspace_root: str | None,
) -> list[dict[str, Any]]:
    candidate_reals = {
        os.path.realpath(path)
        for path in candidate_paths
        if path
    }
    relative_variants = _file_contract_relative_variants(
        expected_path,
        candidate_paths,
        run,
        target_workspace_root,
    )
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in _file_contract_artifact_candidates(artifacts):
        if not _file_artifact_was_successful(artifact):
            continue
        artifact_paths = _artifact_file_paths(artifact)
        if not artifact_paths:
            continue
        for path in artifact_paths:
            if _file_artifact_path_matches(path, candidate_reals, relative_variants):
                key = _artifact_signature(artifact)
                if key not in seen:
                    seen.add(key)
                    matches.append(artifact)
                break
    return matches


def _file_contract_artifact_candidates(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_type = _artifact_type(artifact)
        if artifact_type in {"file_observation", "file"}:
            candidates.append(artifact)
            continue
        if artifact_type == "evidence_record" and str(artifact.get("kind") or "").lower() == "file":
            details = artifact.get("details") if isinstance(artifact.get("details"), Mapping) else {}
            candidates.append({**artifact, **{f"details_{key}": value for key, value in details.items()}})
            continue
        if artifact_type == "worker_result":
            evidence = artifact.get("evidence") if isinstance(artifact.get("evidence"), Mapping) else {}
            for item in evidence.get("files") or []:
                if isinstance(item, Mapping):
                    candidates.append({
                        "type": "worker_file_evidence",
                        **dict(item),
                        "worker_id": artifact.get("worker_id"),
                        "execution_id": artifact.get("execution_id"),
                        "node_id": artifact.get("node_id"),
                    })
                elif str(item or "").strip():
                    candidates.append({
                        "type": "worker_file_evidence",
                        "path": str(item).strip(),
                        "status": "observed",
                        "worker_id": artifact.get("worker_id"),
                        "execution_id": artifact.get("execution_id"),
                        "node_id": artifact.get("node_id"),
                    })
    return candidates


def _file_artifact_was_successful(artifact: dict[str, Any]) -> bool:
    status = str(artifact.get("status") or artifact.get("operation") or artifact.get("details_status") or "").lower()
    if status in {"failed", "error", "blocked"}:
        return False
    if artifact.get("error") or artifact.get("details_error"):
        return False
    return True


def _artifact_file_paths(artifact: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "path",
        "relative_path",
        "absolute_path",
        "requested_path",
        "details_path",
        "details_relative_path",
        "details_absolute_path",
        "details_requested_path",
    ):
        value = artifact.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    details = artifact.get("details") if isinstance(artifact.get("details"), Mapping) else {}
    for key in ("path", "relative_path", "absolute_path", "requested_path"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    return list(dict.fromkeys(paths))


def _file_artifact_path_matches(
    path: str,
    candidate_reals: set[str],
    relative_variants: set[str],
) -> bool:
    if os.path.isabs(path):
        return os.path.realpath(path) in candidate_reals
    normalized = _normalize_contract_relative_path(path)
    return bool(normalized and normalized in relative_variants)


def _file_contract_relative_variants(
    expected_path: str,
    candidate_paths: list[str],
    run: Any,
    target_workspace_root: str | None,
) -> set[str]:
    variants = {_normalize_contract_relative_path(expected_path)}
    roots = [
        {"path": str(root), "aliases": set()}
        for root in (getattr(run, "worktree_path", None), target_workspace_root)
        if str(root or "").strip()
    ]
    roots.extend(project_context_roots(run))
    for candidate in candidate_paths:
        for root_info in roots:
            root = root_info["path"]
            relative = _relative_path_if_inside(candidate, root)
            if not relative:
                continue
            normalized = _normalize_contract_relative_path(relative)
            if not normalized:
                continue
            variants.add(normalized)
            for alias in root_info.get("aliases") or set():
                normalized_alias = _normalize_contract_relative_path(alias)
                if normalized_alias:
                    variants.add(f"{normalized_alias}/{normalized}")
    return {item for item in variants if item}


def _project_context_relative_candidates(expected_relative: str, aliases: set[str]) -> list[str]:
    candidates = [expected_relative]
    for alias in sorted(aliases, key=len, reverse=True):
        normalized_alias = _normalize_contract_relative_path(alias)
        if not normalized_alias:
            continue
        if expected_relative == normalized_alias:
            candidates.append("")
        elif expected_relative.startswith(f"{normalized_alias}/"):
            candidates.append(expected_relative[len(normalized_alias) + 1:])
    return list(dict.fromkeys(candidates))


def _project_file_resource_matches_expected(root: str, aliases: set[str], expected_relative: str) -> bool:
    file_name = _normalize_contract_relative_path(os.path.basename(root))
    if expected_relative == file_name:
        return True
    for alias in aliases:
        normalized_alias = _normalize_contract_relative_path(alias)
        if normalized_alias and expected_relative in {normalized_alias, f"{normalized_alias}/{file_name}"}:
            return True
    return False


def _safe_join_contract_path(root: str, relative: str) -> str | None:
    root_real = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
    relative = _normalize_contract_relative_path(relative)
    candidate = root_real if not relative else os.path.abspath(os.path.join(root_real, relative))
    candidate_real = os.path.realpath(candidate)
    try:
        if os.path.commonpath([root_real, candidate_real]) != root_real:
            return None
    except ValueError:
        return None
    return candidate


def _relative_path_if_inside(path: str, root: str | None) -> str | None:
    if not path or not root:
        return None
    path_real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    root_real = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
    try:
        if os.path.commonpath([root_real, path_real]) != root_real:
            return None
        return os.path.relpath(path_real, root_real)
    except ValueError:
        return None


def _append_unique_path(paths: list[str], path: str | None) -> None:
    if not path:
        return
    normalized = os.path.abspath(os.path.expanduser(path))
    if normalized not in paths:
        paths.append(normalized)


def _normalize_contract_relative_path(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/+", "/", text)
    return text.strip("/")


def _github_repo_slug_from_text(value: Any) -> str | None:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    github_match = re.search(
        r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:\.git)?",
        text,
        re.IGNORECASE,
    )
    if github_match:
        return f"{github_match.group('owner')}/{github_match.group('repo').removesuffix('.git')}"
    slug_match = re.fullmatch(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:\.git)?", text)
    if slug_match:
        return f"{slug_match.group('owner')}/{slug_match.group('repo').removesuffix('.git')}"
    return None


def _artifact_type(artifact: dict[str, Any]) -> str:
    return str(artifact.get("type") or "").strip().lower()


def _artifact_signature(artifact: Any) -> str:
    if not isinstance(artifact, dict):
        return str(artifact)[:200]
    parts = [
        str(artifact.get("type") or ""),
        str(artifact.get("tool_name") or artifact.get("tool") or artifact.get("skill") or ""),
        str(artifact.get("path") or ""),
        str(artifact.get("url") or ""),
        str(artifact.get("number") or artifact.get("pull_number") or artifact.get("pr_number") or ""),
        str(artifact.get("sha") or artifact.get("head_sha") or ""),
        str(artifact.get("execution_id") or ""),
    ]
    return "|".join(parts)[:500]


def _resolve_contract_path(path: str, workspace_root: str | None) -> str:
    if os.path.isabs(path):
        return path
    base = workspace_root or _ROOT
    return os.path.abspath(os.path.join(base, path))
