import hashlib


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_build_file_manifest_records_regular_file_content_and_ignores_draft_metadata(tmp_path):
    from brain.systems.cortex.project_context.drafts import build_file_manifest

    project = tmp_path / "project"
    metadata_dir = project / ".illo-project-draft"
    history_dir = project / ".illo-project-history"
    nested = project / "docs"
    nested.mkdir(parents=True)
    metadata_dir.mkdir()
    history_dir.mkdir()
    (project / "README.md").write_text("hello")
    (nested / "guide.md").write_text("there")
    (metadata_dir / "metadata.json").write_text("{}")
    (history_dir / "index.json").write_text("{}")

    manifest = build_file_manifest(project)

    assert manifest == {
        "README.md": {"kind": "file", "sha256": _sha256("hello"), "size": 5},
        "docs/guide.md": {"kind": "file", "sha256": _sha256("there"), "size": 5},
    }


def test_metadata_helpers_round_trip_hidden_draft_metadata(tmp_path):
    from brain.systems.cortex.project_context.drafts import load_draft_metadata, save_draft_metadata

    draft = tmp_path / "draft"

    saved = save_draft_metadata(
        draft,
        {"base_manifest": {"README.md": {"kind": "file", "sha256": "abc", "size": 3}}},
        source_root="/projects/root",
    )

    assert saved["schema_version"] == 1
    assert load_draft_metadata(draft) == saved
    assert not (draft / "metadata.json").exists()
    assert (draft / ".illo-project-draft" / "metadata.json").exists()


def test_sync_draft_from_root_refreshes_unmodified_files_from_latest_root(tmp_path):
    from brain.systems.cortex.project_context.drafts import load_draft_metadata, sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "notes.md").write_text("base")
    sync_draft_from_root(source, draft)

    (source / "notes.md").write_text("latest")
    result = sync_draft_from_root(source, draft)

    assert (draft / "notes.md").read_text() == "latest"
    assert result.copied == ["notes.md"]
    assert result.removed == []
    assert result.conflicts == []
    assert load_draft_metadata(draft)["base_manifest"] == result.updated_base_manifest


def test_sync_draft_from_root_removes_unmodified_draft_files_deleted_from_root(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "removed.md").write_text("base")
    sync_draft_from_root(source, draft)

    (source / "removed.md").unlink()
    result = sync_draft_from_root(source, draft)

    assert not (draft / "removed.md").exists()
    assert result.copied == []
    assert result.removed == ["removed.md"]
    assert result.conflicts == []


def test_sync_draft_from_root_preserves_draft_modification_when_root_changed(tmp_path):
    from brain.systems.cortex.project_context.drafts import load_draft_metadata, sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "brief.md").write_text("base")
    sync_draft_from_root(source, draft)

    (draft / "brief.md").write_text("thread draft")
    (source / "brief.md").write_text("root update")
    result = sync_draft_from_root(source, draft)

    assert (draft / "brief.md").read_text() == "thread draft"
    assert result.copied == []
    assert result.preserved == ["brief.md"]
    assert result.conflicts == ["brief.md"]
    assert result.out_of_date == ["brief.md"]
    assert load_draft_metadata(draft)["base_manifest"]["brief.md"]["sha256"] == _sha256("base")


def test_sync_draft_from_root_supports_single_file_roots(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source = tmp_path / "spec.md"
    draft = tmp_path / "draft"
    source.write_text("base")

    first = sync_draft_from_root(source, draft)

    assert first.copied == ["spec.md"]
    assert (draft / "spec.md").read_text() == "base"

    source.write_text("latest")
    second = sync_draft_from_root(source, draft)

    assert second.copied == ["spec.md"]
    assert (draft / "spec.md").read_text() == "latest"


def test_sync_draft_from_root_does_not_copy_project_history_metadata(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "brief.md").write_text("base")
    history = source / ".illo-project-history" / "versions" / "v1"
    history.mkdir(parents=True)
    (history / "version.json").write_text("{}")

    result = sync_draft_from_root(source, draft)

    assert result.copied == ["brief.md"]
    assert (draft / "brief.md").read_text() == "base"
    assert not (draft / ".illo-project-history").exists()


def test_sync_draft_from_root_preserves_draft_delete_when_root_changed(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "obsolete.md").write_text("base")
    sync_draft_from_root(source, draft)

    (draft / "obsolete.md").unlink()
    (source / "obsolete.md").write_text("root update")
    result = sync_draft_from_root(source, draft)

    assert not (draft / "obsolete.md").exists()
    assert result.preserved == ["obsolete.md"]
    assert result.conflicts == ["obsolete.md"]


def test_sync_draft_from_root_preserves_draft_modification_when_root_deleted(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "removed-upstream.md").write_text("base")
    sync_draft_from_root(source, draft)

    (draft / "removed-upstream.md").write_text("thread draft")
    (source / "removed-upstream.md").unlink()
    result = sync_draft_from_root(source, draft)

    assert (draft / "removed-upstream.md").read_text() == "thread draft"
    assert result.preserved == ["removed-upstream.md"]
    assert result.conflicts == ["removed-upstream.md"]


def test_plan_draft_publish_lists_draft_changes_and_conflicts(tmp_path):
    from brain.systems.cortex.project_context.drafts import plan_draft_publish, sync_draft_from_root

    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source.mkdir()
    (source / "edit.md").write_text("base edit")
    (source / "delete.md").write_text("base delete")
    (source / "conflict.md").write_text("base conflict")
    (source / "unchanged.md").write_text("base unchanged")
    sync_draft_from_root(source, draft)

    (draft / "edit.md").write_text("draft edit")
    (draft / "delete.md").unlink()
    (draft / "new.md").write_text("draft create")
    (source / "conflict.md").write_text("root update")
    (draft / "conflict.md").write_text("draft update")

    plan = plan_draft_publish(source, draft)

    assert plan.created == ["new.md"]
    assert plan.modified == ["edit.md"]
    assert plan.deleted == ["delete.md"]
    assert plan.conflicted == ["conflict.md"]
