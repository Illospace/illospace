"""Deterministic repo-summary refresh helpers.

Repo summaries are intentionally generated from caller-supplied scopes.  This
module does not walk an entire repository, call an LLM, or write to persistence;
it compares previous summary metadata with the current scoped repo state and
returns payloads that a background pipeline can upsert later.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from brain.platform.async_io import run_subprocess_sync
from brain.systems.memory.source_freshness import evaluate_source_freshness

SUMMARY_SCHEMA_VERSION = "repo-summary/v1"
SOURCE_DIGEST_VERSION = "repo-summary-source/v1"
IDENTITY_DIGEST_VERSION = "repo-summary-identity/v1"
SCOPE_DIGEST_VERSION = "repo-summary-scope/v1"

DEFAULT_ARCHITECTURE_GLOBS: tuple[str, ...] = (
    "README.md",
    "pyproject.toml",
    "requirements*.txt",
    "Makefile",
    "brain/systems/memory/*.py",
    "brain/jobs/pipelines/*.py",
    "brain/systems/learning/*.py",
    "brain/app/scheduler/*.py",
)

DEFAULT_MAX_FILES = 250
DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_TOTAL_BYTES = 5_000_000
HOT_PATH_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class RepoSummarySpec:
    """A bounded repo-summary scope supplied by a caller."""

    repo_root: str | os.PathLike[str]
    path_globs: Sequence[str] = DEFAULT_ARCHITECTURE_GLOBS
    summary_kind: str = "architecture"
    summary_id: str | None = None
    include_prose: bool = False
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepoSummarySpec":
        """Build a spec from JSON-ish pipeline input."""

        path_globs = (
            value.get("path_globs")
            or value.get("file_path_globs")
            or value.get("globs")
            or DEFAULT_ARCHITECTURE_GLOBS
        )
        if isinstance(path_globs, str):
            path_globs = [path_globs]
        return cls(
            repo_root=value.get("repo_root") or value.get("root") or Path.cwd(),
            path_globs=tuple(str(item) for item in path_globs),
            summary_kind=str(value.get("summary_kind") or value.get("kind") or "architecture"),
            summary_id=_clean_text(value.get("summary_id")),
            include_prose=bool(value.get("include_prose", False)),
            max_files=_coerce_int(value.get("max_files"), DEFAULT_MAX_FILES),
            max_file_bytes=_coerce_int(value.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES),
            max_total_bytes=_coerce_int(value.get("max_total_bytes"), DEFAULT_MAX_TOTAL_BYTES),
        )


@dataclass(frozen=True)
class RepoFileDigest:
    """Digest metadata for one scoped repo file."""

    path: str
    size_bytes: int
    sha256: str
    extension: str
    line_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": f"sha256:{self.sha256}",
            "extension": self.extension,
            "line_count": self.line_count,
        }


@dataclass(frozen=True)
class RepoDigestState:
    """Current deterministic state for one repo-summary scope."""

    repo_root: Path
    path_globs: tuple[str, ...]
    summary_kind: str
    branch: str | None
    commit_sha: str | None
    source_digest: str
    source_digest_complete: bool
    files: tuple[RepoFileDigest, ...]
    skipped_files: tuple[dict[str, Any], ...]
    unmatched_globs: tuple[str, ...]
    truncated: bool

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def byte_count(self) -> int:
        return sum(file.size_bytes for file in self.files)

    @property
    def subject_ref(self) -> str:
        return f"repo:{self.repo_root.as_posix()}#{self.summary_scope_digest}"

    @property
    def source_ref(self) -> str:
        if self.commit_sha:
            return f"git:{self.commit_sha}"
        return f"repo:{self.repo_root.as_posix()}"

    @property
    def summary_scope_digest(self) -> str:
        payload = {
            "version": SCOPE_DIGEST_VERSION,
            "repo_root": self.repo_root.as_posix(),
            "summary_kind": self.summary_kind,
            "path_globs": list(self.path_globs),
        }
        return _sha256_json(payload)

    @property
    def summary_identity_digest(self) -> str:
        return summary_identity_digest(
            repo_root=self.repo_root,
            branch=self.branch,
            commit_sha=self.commit_sha,
            path_globs=self.path_globs,
            source_digest=self.source_digest,
            summary_kind=self.summary_kind,
        )

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": IDENTITY_DIGEST_VERSION,
            "identity_digest": self.summary_identity_digest,
            "scope_digest": self.summary_scope_digest,
            "repo_root": self.repo_root.as_posix(),
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "path_globs": list(self.path_globs),
            "source_digest": self.source_digest,
            "summary_kind": self.summary_kind,
        }


def refresh_repo_summaries(
    specs: Sequence[RepoSummarySpec | Mapping[str, Any]] | None = None,
    *,
    previous_summaries: Sequence[Mapping[str, Any]] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    path_globs: Sequence[str] | None = None,
    now: datetime | None = None,
    include_prose: bool = False,
) -> dict[str, Any]:
    """Return current and historical summary payloads for changed scopes.

    ``previous_summaries`` should contain the metadata produced by this module
    or legacy dicts with equivalent top-level fields.  Unchanged summaries are
    returned as current payloads with ``action="unchanged"``; changed summaries
    produce a new current payload and a historical/superseded copy of the old
    payload.  No input payload is deleted or mutated.
    """

    clock = _coerce_datetime(now) or datetime.now(timezone.utc)
    resolved_specs = _resolve_specs(
        specs,
        repo_root=repo_root,
        path_globs=path_globs,
        include_prose=include_prose,
    )
    previous_index = _index_previous(previous_summaries or [])

    current: list[dict[str, Any]] = []
    refreshed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    for spec in resolved_specs:
        state = collect_repo_summary_state(spec)
        previous = previous_index.get(state.summary_scope_digest)
        change = _classify_change(previous, state)
        payload = build_current_architecture_summary_payload(
            state,
            previous=previous,
            action=change["action"],
            refresh_reason=change["reason"],
            generated_at=clock,
            summary_id=spec.summary_id,
            include_prose=spec.include_prose or include_prose,
        )
        current.append(payload)

        if change["action"] == "unchanged":
            unchanged.append(payload)
        elif change["action"] == "metadata_refreshed":
            actions.append(
                _action(
                    "metadata_refreshed",
                    state,
                    reason=change["reason"],
                    generated_at=clock,
                    summary_id=payload["summary_id"],
                    previous_summary_id=_summary_id(previous),
                )
            )
        else:
            refreshed.append(payload)
            actions.append(
                _action(
                    "summary_refreshed",
                    state,
                    reason=change["reason"],
                    generated_at=clock,
                    summary_id=payload["summary_id"],
                    previous_summary_id=_summary_id(previous),
                )
            )

        if previous and change["action"] != "unchanged":
            historical.append(
                mark_summary_historical(
                    previous,
                    superseded_by=payload["summary_id"],
                    superseded_at=clock,
                    reason=change["reason"],
                    current_state=state,
                )
            )

        actions.extend(payload.get("pending_actions", []))
        if state.unmatched_globs:
            actions.append(
                _action(
                    "repo_summary_scope_warning",
                    state,
                    reason="path_globs_matched_no_files",
                    generated_at=clock,
                    summary_id=payload["summary_id"],
                    details={"unmatched_globs": list(state.unmatched_globs)},
                )
            )
        if state.truncated or not state.source_digest_complete:
            actions.append(
                _action(
                    "repo_summary_scope_incomplete",
                    state,
                    reason="digest_scope_limited_by_safety_caps",
                    generated_at=clock,
                    summary_id=payload["summary_id"],
                    details={"skipped_files": list(state.skipped_files)},
                )
            )

    pending_actions = [action for action in actions if action.get("type") == "pending_escalation"]
    stats = {
        "evaluated": len(resolved_specs),
        "current": len(current),
        "refreshed": len(refreshed),
        "metadata_refreshed": sum(1 for item in current if item["action"] == "metadata_refreshed"),
        "unchanged": len(unchanged),
        "historical": len(historical),
        "pending_actions": len(pending_actions),
        "actions": len(actions),
    }
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "generated_at": clock.isoformat(),
        "current": current,
        "current_summaries": current,
        "refreshed": refreshed,
        "refreshed_summaries": refreshed,
        "unchanged": unchanged,
        "unchanged_summaries": unchanged,
        "historical": historical,
        "historical_summaries": historical,
        "actions": actions,
        "pending_actions": pending_actions,
        "stats": stats,
    }


def regenerate_repo_summaries(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for callers that use the PR-L18 feature name."""

    return refresh_repo_summaries(*args, **kwargs)


def collect_repo_summary_state(spec: RepoSummarySpec | Mapping[str, Any]) -> RepoDigestState:
    """Collect bounded deterministic digest state for a summary spec."""

    if isinstance(spec, Mapping):
        spec = RepoSummarySpec.from_mapping(spec)

    repo_root = Path(spec.repo_root).expanduser().resolve(strict=False)
    path_globs = _normalize_globs(spec.path_globs)
    matched, unmatched = _match_scoped_files(repo_root, path_globs)
    selected_paths = matched[: max(0, spec.max_files)]
    truncated = len(matched) > len(selected_paths)

    files: list[RepoFileDigest] = []
    skipped: list[dict[str, Any]] = []
    total_bytes = 0
    for rel_path in selected_paths:
        path = repo_root / rel_path
        try:
            size = path.stat().st_size
        except OSError:
            skipped.append({"path": rel_path, "reason": "stat_failed"})
            continue
        if size > spec.max_file_bytes:
            skipped.append({"path": rel_path, "reason": "file_too_large", "size_bytes": size})
            continue
        if total_bytes + size > spec.max_total_bytes:
            skipped.append({"path": rel_path, "reason": "total_bytes_cap", "size_bytes": size})
            continue

        digest, line_count = _hash_file(path)
        if digest is None:
            skipped.append({"path": rel_path, "reason": "read_failed", "size_bytes": size})
            continue
        total_bytes += size
        files.append(
            RepoFileDigest(
                path=rel_path,
                size_bytes=size,
                sha256=digest,
                extension=Path(rel_path).suffix.lower() or "[none]",
                line_count=line_count,
            )
        )

    if truncated:
        for rel_path in matched[len(selected_paths) :]:
            skipped.append({"path": rel_path, "reason": "max_files_cap"})

    source_digest_complete = not skipped and not truncated
    source_digest = _source_digest(files=files, skipped_files=skipped)
    return RepoDigestState(
        repo_root=repo_root,
        path_globs=path_globs,
        summary_kind=str(spec.summary_kind or "architecture"),
        branch=_git_branch(repo_root),
        commit_sha=_git_head(repo_root),
        source_digest=source_digest,
        source_digest_complete=source_digest_complete,
        files=tuple(files),
        skipped_files=tuple(skipped),
        unmatched_globs=tuple(unmatched),
        truncated=truncated,
    )


def build_current_architecture_summary_payload(
    state: RepoDigestState,
    *,
    previous: Mapping[str, Any] | None = None,
    action: str = "created",
    refresh_reason: str = "no_previous_summary",
    generated_at: datetime | None = None,
    summary_id: str | None = None,
    include_prose: bool = False,
) -> dict[str, Any]:
    """Build a compact current summary payload from deterministic metadata."""

    clock = _coerce_datetime(generated_at) or datetime.now(timezone.utc)
    identity = state.identity()
    resolved_summary_id = summary_id or identity["identity_digest"]
    previous_summary_id = _summary_id(previous)
    source_freshness = evaluate_source_freshness(
        {
            "id": resolved_summary_id,
            "source_kind": "git_commit" if state.commit_sha else "repo_digest",
            "source_ref": state.source_ref,
            "source_digest": state.source_digest,
            "subject_ref": state.subject_ref,
            "subject_type": "repository_architecture",
            "observed_at": clock,
            "staleness_score": 0.0,
        },
        repo_root=state.repo_root,
        reference_digests={state.subject_ref: state.source_digest},
        now=clock,
    )
    architecture = build_hot_path_architecture_metadata(state)
    pending_actions: list[dict[str, Any]] = []
    prose_status = "metadata_only"
    if include_prose:
        prose_status = "pending_model"
        pending_actions.append(
            _action(
                "pending_escalation",
                state,
                reason="architecture_prose_requires_model",
                generated_at=clock,
                summary_id=resolved_summary_id,
                previous_summary_id=previous_summary_id,
                details={"requested_output": "repo_architecture_prose"},
            )
        )

    payload: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "summary_id": resolved_summary_id,
        "summary_kind": state.summary_kind,
        "status": "current",
        "lifecycle_status": "current",
        "action": action,
        "refresh_reason": refresh_reason,
        "generated_at": clock.isoformat(),
        "observed_at": clock.isoformat(),
        "valid_from": clock.isoformat(),
        "repo_root": state.repo_root.as_posix(),
        "branch": state.branch,
        "commit_sha": state.commit_sha,
        "path_globs": list(state.path_globs),
        "source_digest": state.source_digest,
        "source_digest_complete": state.source_digest_complete,
        "source_kind": "repo_summary",
        "source_ref": state.source_ref,
        "subject_type": "repository_architecture",
        "subject_ref": state.subject_ref,
        "freshness_score": 1.0,
        "staleness_score": 0.0,
        "summary_identity": identity,
        "architecture": architecture,
        "hot_path": architecture,
        "content": _deterministic_content(state),
        "content_kind": "deterministic_metadata",
        "prose_status": prose_status,
        "requires_model": False,
        "pending_actions": pending_actions,
        "source_freshness": source_freshness.to_dict(),
    }
    if previous_summary_id:
        payload["supersedes"] = previous_summary_id
    if action == "metadata_refreshed" and previous_summary_id:
        payload["summary_reused_from"] = previous_summary_id
    return payload


def build_hot_path_architecture_metadata(state: RepoDigestState) -> dict[str, Any]:
    """Return compact deterministic metadata suitable for hot-path loading."""

    top_dirs: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    for file in state.files:
        first = file.path.split("/", 1)[0] if "/" in file.path else "[root]"
        top_dirs[first] += 1
        extensions[file.extension] += 1

    largest = sorted(state.files, key=lambda item: (-item.size_bytes, item.path))[:5]
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "repo_name": state.repo_root.name,
        "repo_root": state.repo_root.as_posix(),
        "branch": state.branch,
        "commit_sha": state.commit_sha,
        "summary_kind": state.summary_kind,
        "path_globs": list(state.path_globs),
        "source_digest": state.source_digest,
        "source_digest_complete": state.source_digest_complete,
        "file_count": state.file_count,
        "byte_count": state.byte_count,
        "top_directories": _counter_payload(top_dirs),
        "extensions": _counter_payload(extensions, key_name="extension"),
        "sample_paths": [file.path for file in state.files[:HOT_PATH_SAMPLE_LIMIT]],
        "largest_files": [
            {"path": file.path, "size_bytes": file.size_bytes}
            for file in largest
        ],
        "skipped_file_count": len(state.skipped_files),
        "unmatched_globs": list(state.unmatched_globs),
    }


def build_current_architecture_summary_metadata(
    *,
    repo_root: str | os.PathLike[str],
    path_globs: Sequence[str] = DEFAULT_ARCHITECTURE_GLOBS,
    summary_kind: str = "architecture",
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Collect and return only the compact hot-path architecture metadata."""

    state = collect_repo_summary_state(
        RepoSummarySpec(
            repo_root=repo_root,
            path_globs=path_globs,
            summary_kind=summary_kind,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    )
    return build_hot_path_architecture_metadata(state)


def mark_summary_historical(
    previous: Mapping[str, Any],
    *,
    superseded_by: str,
    superseded_at: datetime | None = None,
    reason: str,
    current_state: RepoDigestState | None = None,
) -> dict[str, Any]:
    """Return a superseded copy of a previous summary payload."""

    clock = _coerce_datetime(superseded_at) or datetime.now(timezone.utc)
    historical = dict(previous)
    historical["status"] = "historical"
    historical["lifecycle_status"] = "historical"
    historical["historical"] = True
    historical["superseded_by"] = superseded_by
    historical["superseded_at"] = clock.isoformat()
    historical["supersession_reason"] = reason
    historical["refresh_action"] = "mark_historical"
    if current_state is not None:
        previous_digest = _previous_source_digest(previous)
        freshness = evaluate_source_freshness(
            {
                "id": _summary_id(previous),
                "source_kind": "git_commit" if _previous_commit(previous) else "repo_digest",
                "source_ref": _previous_source_ref(previous),
                "source_digest": previous_digest,
                "subject_ref": current_state.subject_ref,
                "subject_type": "repository_architecture",
            },
            repo_root=current_state.repo_root,
            reference_digests={current_state.subject_ref: current_state.source_digest},
            now=clock,
        )
        historical["source_freshness"] = freshness.to_dict()
    return historical


def summary_identity_digest(
    *,
    repo_root: str | os.PathLike[str],
    branch: str | None,
    commit_sha: str | None,
    path_globs: Sequence[str],
    source_digest: str,
    summary_kind: str = "architecture",
) -> str:
    """Return a stable digest for the full summary identity."""

    payload = {
        "version": IDENTITY_DIGEST_VERSION,
        "repo_root": Path(repo_root).expanduser().resolve(strict=False).as_posix(),
        "branch": branch,
        "commit_sha": commit_sha,
        "path_globs": list(_normalize_globs(path_globs)),
        "source_digest": _normalize_digest(source_digest),
        "summary_kind": summary_kind,
    }
    return _sha256_json(payload)


def _resolve_specs(
    specs: Sequence[RepoSummarySpec | Mapping[str, Any]] | None,
    *,
    repo_root: str | os.PathLike[str] | None,
    path_globs: Sequence[str] | None,
    include_prose: bool,
) -> list[RepoSummarySpec]:
    if specs:
        return [
            spec if isinstance(spec, RepoSummarySpec) else RepoSummarySpec.from_mapping(spec)
            for spec in specs
        ]
    return [
        RepoSummarySpec(
            repo_root=repo_root or Path.cwd(),
            path_globs=tuple(path_globs or DEFAULT_ARCHITECTURE_GLOBS),
            include_prose=include_prose,
        )
    ]


def _index_previous(previous_summaries: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for summary in previous_summaries:
        metadata = _previous_metadata(summary)
        scope_digest = metadata.get("scope_digest") or _scope_digest_from_metadata(metadata)
        if not scope_digest:
            continue
        if scope_digest not in indexed or _is_more_current(summary, indexed[scope_digest]):
            indexed[scope_digest] = summary
    return indexed


def _classify_change(previous: Mapping[str, Any] | None, state: RepoDigestState) -> dict[str, str]:
    if previous is None:
        return {"action": "created", "reason": "no_previous_summary"}

    metadata = _previous_metadata(previous)
    previous_digest = _normalize_digest(metadata.get("source_digest"))
    if previous_digest != state.source_digest:
        return {"action": "refreshed", "reason": "source_digest_changed"}

    previous_repo_root = _clean_text(metadata.get("repo_root"))
    if previous_repo_root and _safe_resolve(previous_repo_root) != state.repo_root.as_posix():
        return {"action": "metadata_refreshed", "reason": "repo_root_identity_changed"}

    previous_globs = _coerce_globs(metadata.get("path_globs"))
    if previous_globs and previous_globs != state.path_globs:
        return {"action": "metadata_refreshed", "reason": "path_globs_identity_changed"}

    previous_branch = metadata.get("branch")
    if previous_branch not in (None, "", state.branch):
        return {"action": "metadata_refreshed", "reason": "branch_identity_changed"}

    previous_commit = metadata.get("commit_sha")
    if previous_commit not in (None, "", state.commit_sha):
        return {"action": "metadata_refreshed", "reason": "commit_identity_changed"}

    if not metadata.get("identity_digest"):
        return {"action": "metadata_refreshed", "reason": "identity_metadata_missing"}

    return {"action": "unchanged", "reason": "source_digest_matches"}


def _previous_metadata(summary: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("metadata", "summary_identity", "identity", "source_metadata"):
        nested = summary.get(key)
        if isinstance(nested, Mapping):
            metadata.update(nested)
    metadata.update(summary)
    if "path_globs" not in metadata:
        for alias in ("file_path_globs", "globs"):
            if alias in metadata:
                metadata["path_globs"] = metadata[alias]
                break
    if "source_digest" in metadata:
        metadata["source_digest"] = _normalize_digest(metadata["source_digest"])
    if "repo_root" in metadata and metadata["repo_root"]:
        metadata["repo_root"] = _safe_resolve(str(metadata["repo_root"]))
    return metadata


def _scope_digest_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    repo_root = _clean_text(metadata.get("repo_root"))
    path_globs = _coerce_globs(metadata.get("path_globs"))
    if not repo_root or not path_globs:
        return None
    payload = {
        "version": SCOPE_DIGEST_VERSION,
        "repo_root": _safe_resolve(repo_root),
        "summary_kind": str(metadata.get("summary_kind") or "architecture"),
        "path_globs": list(path_globs),
    }
    return _sha256_json(payload)


def _is_more_current(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_status = str(left.get("lifecycle_status") or left.get("status") or "").lower()
    right_status = str(right.get("lifecycle_status") or right.get("status") or "").lower()
    if left_status == "current" and right_status != "current":
        return True
    if right_status == "current" and left_status != "current":
        return False
    return str(left.get("generated_at") or "") >= str(right.get("generated_at") or "")


def _match_scoped_files(repo_root: Path, path_globs: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matches: set[str] = set()
    unmatched: list[str] = []
    for pattern in path_globs:
        found = _glob_files(repo_root, pattern)
        if not found:
            unmatched.append(pattern)
        matches.update(found)
    return tuple(sorted(matches)), tuple(unmatched)


def _glob_files(repo_root: Path, pattern: str) -> list[str]:
    pattern = str(pattern).strip()
    if not pattern:
        return []
    if Path(pattern).is_absolute():
        candidates = glob.glob(pattern, recursive=True)
    else:
        candidates = glob.glob(str(repo_root / pattern), recursive=True)

    files: list[str] = []
    for candidate in candidates:
        path = Path(candidate)
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        if not _is_relative_to(resolved, repo_root) or not resolved.is_file():
            continue
        rel_path = resolved.relative_to(repo_root).as_posix()
        if ".git" in rel_path.split("/"):
            continue
        files.append(rel_path)
    return files


def _hash_file(path: Path) -> tuple[str | None, int | None]:
    digest = hashlib.sha256()
    line_count = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 128), b""):
                digest.update(chunk)
                line_count += chunk.count(b"\n")
    except OSError:
        return None, None
    return digest.hexdigest(), line_count


def _source_digest(*, files: Sequence[RepoFileDigest], skipped_files: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "version": SOURCE_DIGEST_VERSION,
        "files": [file.to_dict() for file in files],
        "skipped_files": [dict(item) for item in skipped_files],
    }
    return _sha256_json(payload)


def _deterministic_content(state: RepoDigestState) -> str:
    branch = state.branch or "unknown-branch"
    commit = (state.commit_sha or "unknown-commit")[:12]
    return (
        f"Repo architecture metadata for {state.repo_root.name} "
        f"on {branch}@{commit}: {state.file_count} scoped files, "
        f"{state.byte_count} bytes, source {state.source_digest}."
    )


def _action(
    action_type: str,
    state: RepoDigestState,
    *,
    reason: str,
    generated_at: datetime,
    summary_id: str,
    previous_summary_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": action_type,
        "reason": reason,
        "summary_id": summary_id,
        "summary_scope_digest": state.summary_scope_digest,
        "repo_root": state.repo_root.as_posix(),
        "branch": state.branch,
        "commit_sha": state.commit_sha,
        "path_globs": list(state.path_globs),
        "source_digest": state.source_digest,
        "created_at": generated_at.isoformat(),
    }
    if previous_summary_id:
        payload["previous_summary_id"] = previous_summary_id
    if details:
        payload["details"] = dict(details)
    return payload


def _counter_payload(counter: Counter[str], *, key_name: str = "name") -> list[dict[str, Any]]:
    return [
        {key_name: key, "files": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _summary_id(summary: Mapping[str, Any] | None) -> str | None:
    if not summary:
        return None
    metadata = _previous_metadata(summary)
    return _clean_text(
        summary.get("summary_id")
        or summary.get("id")
        or metadata.get("identity_digest")
    )


def _previous_source_digest(summary: Mapping[str, Any]) -> str | None:
    return _normalize_digest(_previous_metadata(summary).get("source_digest"))


def _previous_commit(summary: Mapping[str, Any]) -> str | None:
    return _clean_text(_previous_metadata(summary).get("commit_sha"))


def _previous_source_ref(summary: Mapping[str, Any]) -> str | None:
    metadata = _previous_metadata(summary)
    source_ref = _clean_text(metadata.get("source_ref"))
    if source_ref:
        return source_ref
    commit = _clean_text(metadata.get("commit_sha"))
    if commit:
        return f"git:{commit}"
    repo_root = _clean_text(metadata.get("repo_root"))
    return f"repo:{repo_root}" if repo_root else None


def _git_branch(repo_root: Path) -> str | None:
    result = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch in ("", "HEAD") else branch


def _git_head(repo_root: Path) -> str | None:
    result = _run_git(repo_root, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _run_git(repo_root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return run_subprocess_sync(
            ["git", "-C", str(repo_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=list(args), returncode=127, stdout="", stderr="")


def _normalize_globs(path_globs: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item).strip() for item in path_globs if str(item).strip()))


def _coerce_globs(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return _normalize_globs([value])
    if isinstance(value, Sequence):
        return _normalize_globs([str(item) for item in value])
    return ()


def _normalize_digest(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.startswith("sha256:"):
        return text
    if len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text):
        return f"sha256:{text.lower()}"
    return text


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_resolve(value: str) -> str:
    try:
        return Path(value).expanduser().resolve(strict=False).as_posix()
    except OSError:
        return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
