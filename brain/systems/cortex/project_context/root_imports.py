"""Locked and versioned Project root imports."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.project_root import (
    ProjectRootImportCandidate,
    import_candidates_into_project_root,
    load_project_root_imports,
)
from brain.systems.cortex.project_context.root_transaction import project_root_lock
from brain.systems.cortex.project_context.versioning import (
    build_project_root_version_metadata,
    capture_project_root_version,
)


def _version_dict(version: Any | None) -> dict[str, Any] | None:
    if version is None:
        return None
    if hasattr(version, "to_dict"):
        return version.to_dict()
    if isinstance(version, Mapping):
        return dict(version)
    return {"id": str(version), "version_id": str(version)}


def import_candidates_into_project_root_versioned(
    project_root: Path,
    candidates: Sequence[ProjectRootImportCandidate],
    *,
    run_id: int | str | None = None,
    idea_id: str | None = None,
    actor_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Import seed resources into a Project root under lock and record root history."""

    root = Path(project_root).expanduser()
    if not candidates:
        return {"imported": [], "skipped": []}
    root.mkdir(parents=True, exist_ok=True)
    with project_root_lock(root):
        metadata = load_project_root_imports(root)
        imports = metadata.get("imports") if isinstance(metadata.get("imports"), Mapping) else {}
        has_new_candidate = False
        for candidate in candidates:
            source = Path(candidate.source_path).expanduser()
            if (
                source.exists()
                and not source.is_symlink()
                and (source.is_file() or source.is_dir())
                and candidate.key not in imports
            ):
                has_new_candidate = True
                break
        if not has_new_candidate:
            return import_candidates_into_project_root(root, candidates)

        before_version = capture_project_root_version(
            root,
            label="before-root-import",
            metadata=build_project_root_version_metadata(
                run_id=run_id,
                idea_id=idea_id,
                actor_id=actor_id,
                org_id=org_id,
                phase="before",
                operations=[],
            ),
        )
        summary = import_candidates_into_project_root(root, candidates)
        imported = [item for item in summary.get("imported") or [] if isinstance(item, Mapping)]
        if not imported:
            return summary

        operations = [
            {
                "operation": "import",
                "path": item.get("relative_path"),
                "source_path": item.get("source_path"),
                "kind": item.get("kind"),
            }
            for item in imported
        ]
        after_version = capture_project_root_version(
            root,
            label="after-root-import",
            metadata=build_project_root_version_metadata(
                run_id=run_id,
                idea_id=idea_id,
                actor_id=actor_id,
                org_id=org_id,
                phase="after",
                operations=operations,
            ),
        )
    summary["root_versions"] = {
        "before": _version_dict(before_version),
        "after": _version_dict(after_version),
    }
    return summary


__all__ = ["import_candidates_into_project_root_versioned"]
