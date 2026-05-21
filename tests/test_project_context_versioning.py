import hashlib


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_capture_project_root_version_records_manifest_and_copies_files(tmp_path):
    from brain.systems.cortex.project_context.versioning import (
        capture_project_root_version,
        list_project_root_versions,
        summarize_versions,
    )

    project = tmp_path / "project"
    docs = project / "docs"
    docs.mkdir(parents=True)
    (project / "README.md").write_text("hello", encoding="utf-8")
    (docs / "guide.md").write_text("there", encoding="utf-8")

    version = capture_project_root_version(project, label="before-publish", metadata={"run_id": 42})

    assert version.root == project
    assert version.root_kind == "folder"
    assert version.label == "before-publish"
    assert version.metadata == {"run_id": 42}
    assert version.manifest == {
        "README.md": {"kind": "file", "sha256": _sha256("hello"), "size": 5},
        "docs/guide.md": {"kind": "file", "sha256": _sha256("there"), "size": 5},
    }
    assert version.store_path.is_relative_to(project / ".illo-project-history" / "versions")
    assert (version.files_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert (version.files_path / "docs" / "guide.md").read_text(encoding="utf-8") == "there"

    listed = list_project_root_versions(project)
    assert [item.version_id for item in listed] == [version.version_id]
    assert listed[0].manifest == version.manifest
    assert listed[0].metadata == {"run_id": 42}

    summary = summarize_versions(project)
    assert summary["summary"]["version_count"] == 1
    assert summary["summary"]["latest_version_id"] == version.version_id
    assert summary["versions"][0]["file_count"] == 2
    assert summary["versions"][0]["total_size"] == 10
    assert summary["versions"][0]["paths"] == ["README.md", "docs/guide.md"]
    assert "manifest" not in summary["versions"][0]


def test_capture_project_root_version_excludes_hidden_history_from_later_manifests(tmp_path):
    from brain.systems.cortex.project_context.versioning import capture_project_root_version

    project = tmp_path / "project"
    project.mkdir()
    (project / "brief.md").write_text("stable", encoding="utf-8")

    first = capture_project_root_version(project, label="first")
    (project / ".illo-project-history" / "manual-note.txt").write_text("internal", encoding="utf-8")

    second = capture_project_root_version(project, label="second")

    assert second.manifest == first.manifest
    assert all(not path.startswith(".illo-project-history/") for path in second.manifest)
    assert not (second.files_path / ".illo-project-history").exists()


def test_restore_project_root_version_rolls_back_folder_root(tmp_path):
    from brain.systems.cortex.project_context.versioning import (
        capture_project_root_version,
        list_project_root_versions,
        restore_project_root_version,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.md").write_text("before", encoding="utf-8")
    (project / "remove-delete.md").write_text("present", encoding="utf-8")

    before = capture_project_root_version(project, label="before")
    (project / "keep.md").write_text("after", encoding="utf-8")
    (project / "remove-delete.md").unlink()
    (project / "new.md").write_text("new", encoding="utf-8")
    after = capture_project_root_version(project, label="after")

    assert [version.label for version in list_project_root_versions(project)] == ["before", "after"]

    restored = restore_project_root_version(project, before.version_id)

    assert restored.version_id == before.version_id
    assert (project / "keep.md").read_text(encoding="utf-8") == "before"
    assert (project / "remove-delete.md").read_text(encoding="utf-8") == "present"
    assert not (project / "new.md").exists()
    assert (project / ".illo-project-history" / "versions" / after.version_id).exists()


def test_compare_project_root_to_version_previews_folder_restore_without_history(tmp_path):
    from brain.systems.cortex.project_context.versioning import (
        capture_project_root_version,
        compare_project_root_to_version,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.md").write_text("before", encoding="utf-8")
    (project / "restore-me.md").write_text("from version", encoding="utf-8")
    (project / "same.md").write_text("stable", encoding="utf-8")

    version = capture_project_root_version(project, label="before")
    (project / "keep.md").write_text("after", encoding="utf-8")
    (project / "restore-me.md").unlink()
    (project / "delete-me.md").write_text("current only", encoding="utf-8")
    (project / ".illo-project-history" / "manual-note.txt").write_text("internal", encoding="utf-8")

    preview = compare_project_root_to_version(project, version.version_id)

    assert preview.created == ["restore-me.md"]
    assert preview.modified == ["keep.md"]
    assert preview.deleted == ["delete-me.md"]
    assert preview.current_only == ["delete-me.md"]
    assert preview.version_only == ["restore-me.md"]
    assert preview.unchanged == ["same.md"]
    assert preview.changed_paths == ["delete-me.md", "keep.md", "restore-me.md"]
    assert preview.summary == {
        "has_changes": True,
        "created_count": 1,
        "modified_count": 1,
        "deleted_count": 1,
        "current_only_count": 1,
        "version_only_count": 1,
        "unchanged_count": 1,
        "current_file_count": 3,
        "version_file_count": 3,
    }
    assert all(".illo-project-history" not in path for path in preview.changed_paths)
    assert (project / "keep.md").read_text(encoding="utf-8") == "after"
    assert not (project / "restore-me.md").exists()


def test_capture_project_root_version_supports_single_file_roots(tmp_path):
    from brain.systems.cortex.project_context.versioning import (
        capture_project_root_version,
        list_project_root_versions,
        restore_project_root_version,
    )

    spec = tmp_path / "spec.md"
    spec.write_text("version one", encoding="utf-8")

    first = capture_project_root_version(spec, label="before")
    spec.write_text("version two", encoding="utf-8")
    second = capture_project_root_version(spec, label="after")

    assert first.root_kind == "file"
    assert first.manifest == {
        "spec.md": {"kind": "file", "sha256": _sha256("version one"), "size": 11}
    }
    assert first.store_path.is_relative_to(tmp_path / ".illo-project-history")
    assert (first.files_path / "spec.md").read_text(encoding="utf-8") == "version one"
    assert [version.label for version in list_project_root_versions(spec)] == ["before", "after"]
    assert second.manifest == {
        "spec.md": {"kind": "file", "sha256": _sha256("version two"), "size": 11}
    }

    restore_project_root_version(spec, first.version_id)

    assert spec.read_text(encoding="utf-8") == "version one"


def test_compare_project_root_to_version_previews_single_file_restore(tmp_path):
    from brain.systems.cortex.project_context.versioning import (
        capture_project_root_version,
        compare_project_root_to_version,
        restore_project_root_version,
    )

    spec = tmp_path / "spec.md"
    spec.write_text("version one", encoding="utf-8")
    version = capture_project_root_version(spec, label="before")
    spec.unlink()

    preview = compare_project_root_to_version(spec, version.version_id)

    assert preview.root_kind == "file"
    assert preview.created == ["spec.md"]
    assert preview.modified == []
    assert preview.deleted == []
    assert preview.current_only == []
    assert preview.version_only == ["spec.md"]
    assert preview.summary["current_file_count"] == 0
    assert preview.summary["version_file_count"] == 1
    assert not spec.exists()

    restore_project_root_version(spec, version.version_id)

    assert spec.read_text(encoding="utf-8") == "version one"


def test_build_project_root_version_metadata_records_publish_operation_shape():
    from brain.systems.cortex.project_context.versioning import build_project_root_version_metadata

    metadata = build_project_root_version_metadata(
        run_id=42,
        idea_id="idea-1",
        resource_id="reports",
        mount_path="/reports",
        phase="before",
        operations=[
            {"operation": "update", "path": "brief.md", "target_path": "brief.md"},
            {"operation": "delete", "path": "old.md"},
        ],
        extra={"source": "test"},
    )

    assert metadata == {
        "run_id": "42",
        "idea_id": "idea-1",
        "resource_id": "reports",
        "mount_path": "/reports",
        "phase": "before",
        "operation_count": 2,
        "operation_kinds": ["update", "delete"],
        "operation_paths": ["brief.md", "old.md"],
        "operation_summary": {
            "operation_count": 2,
            "path_count": 2,
            "paths": ["brief.md", "old.md"],
            "by_operation": {"delete": 1, "update": 1},
        },
        "diff_summary": {
            "operation_count": 2,
            "path_count": 2,
            "paths": ["brief.md", "old.md"],
            "by_operation": {"delete": 1, "update": 1},
        },
        "publish_event": {
            "type": "project_root_publish",
            "agent_run_id": "42",
            "thread_id": "idea-1",
            "resource_id": "reports",
            "mount_path": "/reports",
            "phase": "before",
            "operation_count": 2,
            "operation_kinds": ["update", "delete"],
        },
        "source": "test",
    }
