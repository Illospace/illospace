from pathlib import Path

import pytest

from brain.systems.cortex.project_context.browser import (
    project_file_payload,
    project_resource_file_browser,
    with_project_file_browser,
)
from brain.systems.cortex.project_context.drafts import build_file_manifest, save_draft_metadata


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


def test_project_file_payload_rejects_escaping_paths(tmp_path):
    with pytest.raises(ValueError, match="inside the mounted Project resource"):
        project_file_payload({"resources": []}, resource_id=None, path="../secret.md")
    with pytest.raises(ValueError, match="internal metadata"):
        project_file_payload({"resources": []}, resource_id=None, path=".illo-project-history/version.json")
