from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from brain.systems.cortex.project_context.browser import (
    project_file_blob,
    project_file_payload,
    project_resource_file_browser,
    update_project_draft_file,
    with_project_file_browser,
)
from brain.systems.cortex.project_context.drafts import build_file_manifest, save_draft_metadata
from brain.systems.cortex.project_context.profile_browser import (
    project_profile_draft_state_payload,
    project_profile_file_payload,
    update_project_profile_draft_file,
)
from brain.systems.cortex.project_context.project_root import project_draft_root_path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_project_resource_file_browser_marks_root_draft_statuses(tmp_path):
    root = tmp_path / "root"
    draft = tmp_path / "draft"
    _write(root / "docs" / "brief.md", "root brief\n")
    _write(root / "docs" / "deleted.md", "delete me\n")
    _write(root / "docs" / "same.md", "same\n")
    _write(draft / "docs" / "brief.md", "draft brief\n")
    _write(draft / "docs" / "new.md", "new\n")
    _write(draft / "docs" / "same.md", "same\n")

    save_draft_metadata(draft, base_manifest=build_file_manifest(root))

    browser = project_resource_file_browser({
        "id": "project-root",
        "mount_path": "/",
        "source_path": str(root),
        "workspace_path": str(draft),
        "changes": {
            "changed_paths": ["docs/brief.md"],
            "new_paths": ["docs/new.md"],
            "deleted_paths": ["docs/deleted.md"],
            "conflicted_paths": [],
            "out_of_date_paths": ["docs/stale.md"],
        },
    })

    entries = {entry["path"]: entry for entry in browser["entries"]}
    assert entries["docs/brief.md"]["status"] == "changed"
    assert entries["docs/new.md"]["status"] == "new"
    assert entries["docs/deleted.md"]["status"] == "deleted"
    assert entries["docs/same.md"]["status"] == "clean"
    assert entries["docs/stale.md"]["status"] == "out_of_date"
    assert browser["summary"]["file_count"] == 5


def test_with_project_file_browser_keeps_browser_payload_outside_tools_shape(tmp_path):
    root = tmp_path / "root"
    draft = tmp_path / "draft"
    _write(root / "README.md", "root\n")
    _write(draft / "README.md", "root\n")
    save_draft_metadata(draft, base_manifest=build_file_manifest(root))

    payload = with_project_file_browser({
        "ok": True,
        "resources": [{
            "id": "root",
            "mount_path": "/",
            "source_path": str(root),
            "workspace_path": str(draft),
        }],
    })

    assert payload["file_browser"]["summary"]["file_count"] == 1
    assert payload["resources"][0]["file_browser"]["entries"][0]["path"] == "README.md"


def test_project_file_payload_reads_root_base_and_draft_layers(tmp_path):
    root = tmp_path / "root"
    draft = tmp_path / "draft"
    _write(root / "report.md", "root v1\n")
    _write(draft / "report.md", "draft v2\n")
    _write(draft / ".illo-project-draft" / "base" / "report.md", "root v1\n")
    save_draft_metadata(draft, base_manifest=build_file_manifest(root))

    payload = project_file_payload(
        {
            "resources": [{
                "id": "root",
                "mount_path": "/",
                "source_path": str(root),
                "workspace_path": str(draft),
                "changes": {"changed_paths": ["report.md"]},
            }],
        },
        resource_id="root",
        path="report.md",
    )

    assert payload["ok"] is True
    assert payload["entry"]["status"] == "changed"
    assert payload["layers"]["root"]["content"] == "root v1\n"
    assert payload["layers"]["base"]["content"] == "root v1\n"
    assert payload["layers"]["draft"]["content"] == "draft v2\n"


def test_update_project_draft_file_writes_only_thread_overlay(tmp_path):
    root = tmp_path / "root"
    draft = tmp_path / "draft"
    _write(root / "report.md", "root v1\n")
    _write(draft / ".illo-project-draft" / "base" / "report.md", "root v1\n")
    save_draft_metadata(draft, base_manifest=build_file_manifest(root))

    payload = update_project_draft_file(
        {
            "resources": [{
                "id": "root",
                "mount_path": "/",
                "source_path": str(root),
                "workspace_path": str(draft),
            }],
        },
        resource_id="root",
        path="report.md",
        content="manual edit\n",
    )

    assert payload["updated"] is True
    assert payload["layers"]["root"]["content"] == "root v1\n"
    assert payload["layers"]["draft"]["content"] == "manual edit\n"
    assert (root / "report.md").read_text(encoding="utf-8") == "root v1\n"
    assert (draft / "report.md").read_text(encoding="utf-8") == "manual edit\n"


def test_project_file_blob_resolves_previewable_layers(tmp_path):
    root = tmp_path / "root"
    draft = tmp_path / "draft"
    _write(root / "image.svg", "<svg></svg>")
    _write(draft / "image.svg", "<svg><title>draft</title></svg>")
    save_draft_metadata(draft, base_manifest=build_file_manifest(root))

    blob = project_file_blob(
        {
            "resources": [{
                "id": "root",
                "mount_path": "/",
                "source_path": str(root),
                "workspace_path": str(draft),
            }],
        },
        resource_id="root",
        path="image.svg",
        layer="draft",
    )

    assert blob["path"] == draft / "image.svg"
    assert blob["filename"] == "image.svg"
    assert blob["media_type"] == "image/svg+xml"


def test_project_file_payload_rejects_escaping_paths(tmp_path):
    with pytest.raises(ValueError, match="inside the mounted Project resource"):
        project_file_payload({"resources": []}, resource_id=None, path="../secret.md")
    with pytest.raises(ValueError, match="internal metadata"):
        project_file_payload({"resources": []}, resource_id=None, path=".illo-project-history/version.json")


def test_project_profile_browser_reads_root_and_creates_thread_draft_on_edit(tmp_path):
    thread_root = tmp_path / "ideas" / "thread-a"
    project_root = tmp_path / "project-roots" / "project-1"
    _write(project_root / "README.md", "root readme\n")
    profile = SimpleNamespace(
        id="project-1",
        org_id="org-1",
        user_id="user-1",
        slug="payments",
        name="Payments",
        description=None,
        project_context={"version": 1, "resources": []},
        visibility="public",
        default_environment_binding_id=None,
        active=True,
        metadata_={},
        created_at=datetime.now(timezone.utc),
    )
    run = SimpleNamespace(
        id=17,
        workspace_ref={
            "project_workspace_manifest": {
                "mounts": [{
                    "draft_identity": {"thread_workspace_root": str(thread_root)},
                }],
            },
        },
        metadata_={},
        target_ref={},
    )

    state = project_profile_draft_state_payload(profile, run, idea_id="idea-1")
    entries = {entry["path"]: entry for entry in state["draft_status"]["file_browser"]["entries"]}
    assert entries["README.md"]["has_root"] is True
    assert entries["README.md"]["has_draft"] is False
    assert entries["README.md"]["status"] == "clean"
    assert project_profile_file_payload(profile, run, idea_id="idea-1", path="README.md")["layers"]["root"]["content"] == "root readme\n"

    updated = update_project_profile_draft_file(
        profile,
        run,
        idea_id="idea-1",
        path="README.md",
        content="draft readme\n",
    )

    draft_root = project_draft_root_path(thread_root, "project-1")
    assert updated["updated"] is True
    assert (project_root / "README.md").read_text(encoding="utf-8") == "root readme\n"
    assert (draft_root / "README.md").read_text(encoding="utf-8") == "draft readme\n"
    refreshed = project_profile_draft_state_payload(profile, run, idea_id="idea-1")
    refreshed_entries = {entry["path"]: entry for entry in refreshed["draft_status"]["file_browser"]["entries"]}
    assert refreshed_entries["README.md"]["status"] == "changed"
