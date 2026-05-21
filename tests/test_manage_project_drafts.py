import json
from pathlib import Path
from types import SimpleNamespace

from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import projects


def _project_run(source_dir, draft_dir):
    resource = {
        "id": "reports",
        "kind": "folder",
        "mount_path": "/reports",
        "path": str(draft_dir),
        "materialization": {
            "status": "ready",
            "provider": "local",
            "kind": "folder",
            "path": str(draft_dir),
            "source_path": str(source_dir),
            "workspace_path": str(draft_dir),
            "draft": True,
        },
    }
    return SimpleNamespace(
        id=123,
        target_ref={"project_context_snapshot": {"resources": [resource]}},
        workspace_ref={
            "workspaces": [{"name": "/reports", "path": str(draft_dir)}],
            "project_context_snapshot": {"resources": [resource]},
            "project_context_materialization": {
                "status": "materialized",
                "workspaces": [{"name": "/reports", "path": str(draft_dir)}],
                "errors": [],
            },
        },
        metadata_={},
    )


def _file_project_run(source_file, draft_dir):
    resource = {
        "id": "resource-1",
        "kind": "file",
        "mount_path": f"/{Path(source_file).name}",
        "path": str(draft_dir / Path(source_file).name),
        "materialization": {
            "status": "ready",
            "provider": "local",
            "kind": "file",
            "path": str(draft_dir / Path(source_file).name),
            "source_path": str(source_file),
            "workspace_path": str(draft_dir),
            "draft": True,
        },
    }
    return SimpleNamespace(
        id=124,
        target_ref={"project_context_snapshot": {"resources": [resource]}},
        workspace_ref={
            "workspaces": [{"name": resource["mount_path"], "path": str(draft_dir)}],
            "project_context_snapshot": {"resources": [resource]},
            "project_context_materialization": {
                "status": "materialized",
                "workspaces": [{"name": resource["mount_path"], "path": str(draft_dir)}],
                "errors": [],
            },
        },
        metadata_={},
    )


def _repo_project_run(repo_dir):
    resource = {
        "id": "repo",
        "kind": "repo",
        "mount_path": "/repos/backend",
        "repo": "example/backend",
        "path": str(repo_dir),
        "materialization": {
            "status": "ready",
            "provider": "github",
            "repo": "example/backend",
            "path": str(repo_dir),
            "workspace_path": str(repo_dir),
            "branch": "main",
        },
    }
    return SimpleNamespace(
        id=125,
        target_ref={"project_context_snapshot": {"resources": [resource]}},
        workspace_ref={
            "workspaces": [{"name": "/repos/backend", "path": str(repo_dir)}],
            "project_context_snapshot": {"resources": [resource]},
        },
        metadata_={},
    )


def _multi_project_run(resources):
    resource_entries = []
    workspaces = []
    for resource_id, mount_path, source_dir, draft_dir in resources:
        resource = {
            "id": resource_id,
            "kind": "folder",
            "mount_path": mount_path,
            "path": str(draft_dir),
            "materialization": {
                "status": "ready",
                "provider": "local",
                "kind": "folder",
                "path": str(draft_dir),
                "source_path": str(source_dir),
                "workspace_path": str(draft_dir),
                "draft": True,
            },
        }
        resource_entries.append(resource)
        workspaces.append({"name": mount_path, "path": str(draft_dir)})
    return SimpleNamespace(
        id=126,
        target_ref={"project_context_snapshot": {"resources": resource_entries}},
        workspace_ref={
            "workspaces": workspaces,
            "project_context_snapshot": {"resources": resource_entries},
            "project_context_materialization": {
                "status": "materialized",
                "workspaces": workspaces,
                "errors": [],
            },
        },
        metadata_={},
    )


async def test_manage_project_draft_status_reports_local_draft_changes(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("original", encoding="utf-8")
    (source_dir / "remove.md").write_text("remove me", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("changed", encoding="utf-8")
    (draft_dir / "new.md").write_text("new file", encoding="utf-8")
    (draft_dir / "remove.md").unlink()

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        payload = json.loads(await projects._handle_manage_project(action="draft_status"))

    resource = payload["resources"][0]
    assert payload["ok"] is True
    assert payload["run_id"] == "123"
    assert payload["idea_id"] == "idea-1"
    assert resource["mount_path"] == "/reports"
    assert resource["is_draft_workspace"] is True
    assert resource["change_source"] == "draft_manifest"
    assert resource["changes"]["changed_paths"] == ["brief.md"]
    assert resource["changes"]["new_paths"] == ["new.md"]
    assert resource["changes"]["deleted_paths"] == ["remove.md"]
    assert resource["changes"]["conflicted_paths"] == []
    assert payload["changes"]["counts"] == {
        "changed_paths": 1,
        "new_paths": 1,
        "deleted_paths": 1,
        "conflicted_paths": 0,
    }


async def test_manage_project_plan_publish_groups_changes_without_writing_source(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("original", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("changed", encoding="utf-8")
    (draft_dir / "new.md").write_text("new file", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        payload = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "original"
    assert not (source_dir / "new.md").exists()
    assert payload["ok"] is True
    assert payload["mutates_project_root"] is False
    assert payload["plan_only"] is True
    assert payload["summary"] == {"resource_count": 1, "operation_count": 2, "blocked_count": 0}
    group = payload["groups"][0]
    assert group["mount_path"] == "/reports"
    assert group["publish_target"] == {"kind": "local_path", "path": str(source_dir)}
    assert group["status"] == "ready"
    assert {(op["operation"], op["path"]) for op in group["operations"]} == {
        ("update", "brief.md"),
        ("create", "new.md"),
    }
    assert group["diff"]["path_count"] == 2
    assert group["diff"]["root_to_draft"]


async def test_manage_project_draft_status_uses_manifest_mount_paths(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root
    from brain.systems.cortex.project_context.workspace_manifest import ProjectWorkspaceManifest

    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    draft_a = tmp_path / "thread" / ".illo-project-context" / "local" / "reports-a"
    draft_b = tmp_path / "thread" / ".illo-project-context" / "local" / "reports-b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "brief.md").write_text("base a", encoding="utf-8")
    (source_b / "brief.md").write_text("base b", encoding="utf-8")
    sync_draft_from_root(source_a, draft_a)
    sync_draft_from_root(source_b, draft_b)
    (draft_b / "brief.md").write_text("draft b", encoding="utf-8")

    run = _multi_project_run(
        [
            ("reports-a", "/reports", source_a, draft_a),
            ("reports-b", "/reports", source_b, draft_b),
        ]
    )
    snapshot = run.workspace_ref["project_context_snapshot"]
    manifest = ProjectWorkspaceManifest.from_project_context(snapshot).to_dict()
    run.workspace_ref["project_workspace_manifest"] = manifest
    run.metadata_["project_workspace_manifest"] = manifest

    with bind_agent_context({"run": run, "idea_id": "idea-1"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        published = json.loads(
            await projects._handle_manage_project(
                action="publish_draft",
                publish_paths=["/reports-2/brief.md"],
            )
        )

    assert [resource["mount_path"] for resource in status["resources"]] == ["/reports", "/reports-2"]
    assert published["ok"] is True
    assert published["summary"] == {"published_groups": 1, "operation_count": 1, "blocked_count": 0}
    assert (source_a / "brief.md").read_text(encoding="utf-8") == "base a"
    assert (source_b / "brief.md").read_text(encoding="utf-8") == "draft b"


async def test_manage_project_plan_publish_blocks_conflicts_from_draft_manifest(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (source_dir / "brief.md").write_text("root update", encoding="utf-8")
    (draft_dir / "brief.md").write_text("draft update", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert status["resources"][0]["status"] == "conflicted"
    assert status["resources"][0]["change_source"] == "draft_manifest"
    assert status["resources"][0]["changes"]["conflicted_paths"] == ["brief.md"]
    assert status["resources"][0]["changes"]["out_of_date_paths"] == ["brief.md"]
    assert status["resources"][0]["out_of_date"] is True
    assert status["changes"]["out_of_date_paths"] == [
        {"resource_id": "reports", "mount_path": "/reports", "path": "brief.md"}
    ]
    assert plan["summary"]["blocked_count"] == 1
    assert plan["groups"][0]["status"] == "blocked"
    assert plan["groups"][0]["blocked_reasons"] == ["conflicted_paths_require_resolution"]


async def test_manage_project_draft_status_is_read_only_for_unmodified_files_changed_in_root(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("root v1", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (source_dir / "brief.md").write_text("root v2", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))

    assert status["ok"] is True
    assert (draft_dir / "brief.md").read_text(encoding="utf-8") == "root v1"
    assert status["resources"][0]["status"] == "out_of_date"
    assert status["resources"][0]["out_of_date_paths"] == ["brief.md"]
    assert status["resources"][0]["changes"]["conflicted_paths"] == []


async def test_manage_project_refresh_draft_from_root_updates_unmodified_files(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("root v1", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (source_dir / "brief.md").write_text("root v2", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        refreshed = json.loads(await projects._handle_manage_project(action="refresh_draft_from_root"))
        status = json.loads(await projects._handle_manage_project(action="draft_status"))

    assert refreshed["ok"] is True
    assert refreshed["summary"] == {"refreshed_count": 1, "skipped_count": 0, "conflicted_count": 0}
    assert refreshed["refreshed_resources"][0]["updated_from_root"] == ["brief.md"]
    assert (draft_dir / "brief.md").read_text(encoding="utf-8") == "root v2"
    assert status["resources"][0]["status"] == "clean"


async def test_manage_project_publish_draft_applies_local_changes_and_refreshes_plan(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    (source_dir / "delete.md").write_text("delete", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("draft edit", encoding="utf-8")
    (draft_dir / "new.md").write_text("new", encoding="utf-8")
    (draft_dir / "delete.md").unlink()

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))
        plan_after = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert published["ok"] is True
    assert published["mutated_project_root"] is True
    assert published["summary"] == {"published_groups": 1, "operation_count": 3, "blocked_count": 0}
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "draft edit"
    assert (source_dir / "new.md").read_text(encoding="utf-8") == "new"
    assert not (source_dir / "delete.md").exists()
    assert plan_after["summary"] == {"resource_count": 1, "operation_count": 0, "blocked_count": 0}


async def test_manage_project_blocks_new_sibling_publish_for_file_roots(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "uploads"
    source_dir.mkdir()
    source_file = source_dir / "unified_payments.csv"
    source_file.write_text("base csv", encoding="utf-8")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "resource-1"
    sync_draft_from_root(source_file, draft_dir)
    (draft_dir / "customer_analysis_past_4_weeks.md").write_text("analysis", encoding="utf-8")

    with bind_agent_context({"run": _file_project_run(source_file, draft_dir), "idea_id": "idea-1"}):
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        published = json.loads(
            await projects._handle_manage_project(
                action="publish_draft",
                publish_paths=["customer_analysis_past_4_weeks.md"],
            )
        )

    operation = plan["groups"][0]["operations"][0]
    assert plan["summary"] == {"resource_count": 1, "operation_count": 1, "blocked_count": 1}
    assert plan["groups"][0]["status"] == "blocked"
    assert plan["groups"][0]["blocked_reasons"] == ["file_project_root_cannot_publish_additional_paths"]
    assert operation["draft_path"] == str(draft_dir / "customer_analysis_past_4_weeks.md")
    assert operation["target_path"] is None
    assert published["ok"] is False
    assert published["code"] == "project_draft_publish_blocked"
    assert source_file.read_text(encoding="utf-8") == "base csv"
    assert not (source_dir / "customer_analysis_past_4_weeks.md").exists()


async def test_manage_project_allows_original_file_root_update(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "uploads"
    source_dir.mkdir()
    source_file = source_dir / "unified_payments.csv"
    source_file.write_text("base csv", encoding="utf-8")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "resource-1"
    sync_draft_from_root(source_file, draft_dir)
    (draft_dir / "unified_payments.csv").write_text("draft csv", encoding="utf-8")

    with bind_agent_context({"run": _file_project_run(source_file, draft_dir), "idea_id": "idea-1"}):
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))

    assert plan["summary"] == {"resource_count": 1, "operation_count": 1, "blocked_count": 0}
    assert plan["groups"][0]["operations"][0]["target_path"] == str(source_file)
    assert published["ok"] is True
    assert source_file.read_text(encoding="utf-8") == "draft csv"


async def test_manage_project_publish_draft_rolls_back_local_root_on_failure(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import publish
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "first.md").write_text("base first", encoding="utf-8")
    (source_dir / "second.md").write_text("base second", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "first.md").write_text("draft first", encoding="utf-8")
    (draft_dir / "second.md").write_text("draft second", encoding="utf-8")

    real_copy = publish._copy_publish_path

    def fail_on_second(draft_path, target_path):
        if str(target_path).endswith("second.md"):
            raise OSError("disk full")
        real_copy(draft_path, target_path)

    monkeypatch.setattr(publish, "_copy_publish_path", fail_on_second)

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        payload = json.loads(await projects._handle_manage_project(action="publish_draft"))

    assert payload["ok"] is False
    assert payload["mutated_project_root"] is False
    assert payload["blocked_groups"][0]["blocked_reasons"] == ["publish_failed_rolled_back"]
    assert payload["blocked_groups"][0]["error"] == "disk full"
    assert (source_dir / "first.md").read_text(encoding="utf-8") == "base first"
    assert (source_dir / "second.md").read_text(encoding="utf-8") == "base second"


async def test_manage_project_publish_draft_publishes_selected_local_path_only(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base brief", encoding="utf-8")
    (source_dir / "notes.md").write_text("base notes", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("draft brief", encoding="utf-8")
    (draft_dir / "notes.md").write_text("draft notes", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        published = json.loads(await projects._handle_manage_project(action="publish_draft", publish_paths=["brief.md"]))
        plan_after = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert published["ok"] is True
    assert published["mutated_project_root"] is True
    assert published["summary"] == {"published_groups": 1, "operation_count": 1, "blocked_count": 0}
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "draft brief"
    assert (source_dir / "notes.md").read_text(encoding="utf-8") == "base notes"
    assert (draft_dir / "notes.md").read_text(encoding="utf-8") == "draft notes"
    assert plan_after["summary"] == {"resource_count": 1, "operation_count": 1, "blocked_count": 0}
    assert plan_after["groups"][0]["operations"] == [
        {
            "operation": "update",
            "path": "notes.md",
            "draft_path": str(draft_dir / "notes.md"),
            "target_path": str(source_dir / "notes.md"),
        }
    ]


async def test_manage_project_publish_draft_publishes_selected_resource_only(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    draft_a = tmp_path / "thread" / ".illo-project-context" / "local" / "reports-a"
    draft_b = tmp_path / "thread" / ".illo-project-context" / "local" / "reports-b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "brief.md").write_text("base a", encoding="utf-8")
    (source_b / "brief.md").write_text("base b", encoding="utf-8")
    sync_draft_from_root(source_a, draft_a)
    sync_draft_from_root(source_b, draft_b)
    (draft_a / "brief.md").write_text("draft a", encoding="utf-8")
    (draft_b / "brief.md").write_text("draft b", encoding="utf-8")

    run = _multi_project_run(
        [
            ("reports-a", "/reports-a", source_a, draft_a),
            ("reports-b", "/reports-b", source_b, draft_b),
        ]
    )
    with bind_agent_context({"run": run, "idea_id": "idea-1"}):
        published = json.loads(await projects._handle_manage_project(action="publish_draft", resource_ids=["reports-a"]))
        plan_after = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert published["ok"] is True
    assert published["mutated_project_root"] is True
    assert published["summary"] == {"published_groups": 1, "operation_count": 1, "blocked_count": 0}
    assert (source_a / "brief.md").read_text(encoding="utf-8") == "draft a"
    assert (source_b / "brief.md").read_text(encoding="utf-8") == "base b"
    assert {
        (group["resource_id"], tuple((op["operation"], op["path"]) for op in group["operations"]))
        for group in plan_after["groups"]
    } == {
        ("reports-a", ()),
        ("reports-b", (("update", "brief.md"),)),
    }


async def test_manage_project_publish_draft_with_unmatched_filters_returns_error(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("draft edit", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        payload = json.loads(await projects._handle_manage_project(action="publish_draft", publish_paths=["missing.md"]))

    assert payload["ok"] is False
    assert payload["code"] == "project_draft_publish_selection_empty"
    assert payload["mutated_project_root"] is False
    assert payload["summary"] == {"published_groups": 0, "operation_count": 0, "blocked_count": 0}
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "base"


async def test_manage_project_publish_draft_refuses_conflicts(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (source_dir / "brief.md").write_text("root update", encoding="utf-8")
    (draft_dir / "brief.md").write_text("draft update", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        payload = json.loads(await projects._handle_manage_project(action="publish_draft"))

    assert payload["ok"] is False
    assert payload["code"] == "project_draft_conflicts_require_resolution"
    assert payload["summary"]["blocked_count"] == 1
    assert payload["conflict_resolution"]["required"] is True
    assert "preserves the user's intent" in payload["conflict_resolution"]["instructions"]
    assert payload["conflict_resolution"]["retry_action"]["arguments"] == {"action": "publish_draft"}
    conflict = payload["conflict_resolution"]["conflicts"][0]
    assert conflict["path"] == "brief.md"
    assert conflict["root_path"] == str(source_dir / "brief.md")
    assert conflict["draft_path"] == str(draft_dir / "brief.md")
    assert "-root update" in conflict["root_to_draft_diff"][0]["patch"]
    assert "+draft update" in conflict["root_to_draft_diff"][0]["patch"]
    assert "-base" in conflict["base_to_draft_diff"][0]["patch"]
    assert "+draft update" in conflict["base_to_draft_diff"][0]["patch"]
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "root update"


async def test_manage_project_publish_draft_retries_after_conflict_checkpoint_when_root_is_stable(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (source_dir / "brief.md").write_text("root update", encoding="utf-8")
    (draft_dir / "brief.md").write_text("draft update", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-1"}):
        first = json.loads(await projects._handle_manage_project(action="publish_draft"))
        second = json.loads(await projects._handle_manage_project(action="publish_draft"))

    assert first["ok"] is False
    assert first["code"] == "project_draft_conflicts_require_resolution"
    assert second["ok"] is True
    assert second["summary"] == {"published_groups": 1, "operation_count": 1, "blocked_count": 0}
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "draft update"


async def test_manage_project_publish_draft_uses_repo_adapter_for_git_resources(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import publish

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    calls = []

    def fake_repo_draft_status(path):
        assert path == repo_dir
        return SimpleNamespace(changed_paths=["app.py"], unmerged_paths=[])

    def fake_publish_repo_draft(repo_path, **kwargs):
        calls.append({"repo_path": repo_path, **kwargs})
        return SimpleNamespace(
            ok=True,
            branch="illo/idea-1",
            commit_sha="abc123",
            changed_paths=["app.py"],
            pushed=True,
            pr_url="https://github.com/example/backend/pull/1",
            errors=[],
        )

    monkeypatch.setattr(publish, "repo_draft_status", fake_repo_draft_status)
    monkeypatch.setattr(publish, "publish_repo_draft", fake_publish_repo_draft)

    with bind_agent_context({"run": _repo_project_run(repo_dir), "idea_id": "idea-1"}):
        payload = json.loads(
            await projects._handle_manage_project(
                action="publish_draft",
                branch_name="illo/idea-1",
                commit_message="Update project draft",
                push=True,
                create_pr=True,
                pr_title="Update backend",
            )
        )

    assert payload["ok"] is True
    assert payload["summary"] == {"published_groups": 1, "operation_count": 1, "blocked_count": 0}
    assert calls == [
        {
            "repo_path": Path(repo_dir),
            "branch_name": "illo/idea-1",
            "commit_message": "Update project draft",
            "push": True,
            "create_pr": True,
            "pr_title": "Update backend",
            "pr_body": None,
            "check_upstream": True,
            "base_branch": None,
            "selected_paths": None,
        }
    ]
    group = payload["published_groups"][0]
    assert group["status"] == "published"
    assert group["repo_publish"]["pr_url"] == "https://github.com/example/backend/pull/1"


async def test_manage_project_publish_draft_passes_selected_paths_to_repo_adapter(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import publish

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    calls = []

    def fake_repo_draft_status(path):
        assert path == repo_dir
        return SimpleNamespace(changed_paths=["app.py", "README.md"], unmerged_paths=[])

    def fake_publish_repo_draft(repo_path, **kwargs):
        calls.append({"repo_path": repo_path, **kwargs})
        return SimpleNamespace(
            ok=True,
            branch="illo/idea-1",
            commit_sha="abc123",
            changed_paths=["app.py"],
            errors=[],
        )

    monkeypatch.setattr(publish, "repo_draft_status", fake_repo_draft_status)
    monkeypatch.setattr(publish, "publish_repo_draft", fake_publish_repo_draft)

    with bind_agent_context({"run": _repo_project_run(repo_dir), "idea_id": "idea-1"}):
        payload = json.loads(
            await projects._handle_manage_project(
                action="publish_draft",
                publish_paths=["/repos/backend/app.py"],
                branch_name="illo/idea-1",
                commit_message="Update app",
            )
        )

    assert payload["ok"] is True
    assert calls[0]["selected_paths"] == ["app.py"]


async def test_manage_project_repo_status_errors_block_publish(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import draft_state, publish

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    def fake_repo_draft_status(path):
        assert path == repo_dir
        return SimpleNamespace(
            changed_paths=[],
            unmerged_paths=[],
            errors=["git status failed: not a git repository"],
        )

    monkeypatch.setattr(draft_state, "repo_draft_status", fake_repo_draft_status)
    monkeypatch.setattr(publish, "repo_draft_status", fake_repo_draft_status)

    with bind_agent_context({"run": _repo_project_run(repo_dir), "idea_id": "idea-1"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))

    resource = status["resources"][0]
    assert resource["status"] == "error"
    assert resource["errors"] == ["git status failed: not a git repository"]
    assert plan["summary"] == {"resource_count": 1, "operation_count": 0, "blocked_count": 1}
    assert plan["groups"][0]["blocked_reasons"] == ["repo_status_failed"]
    assert published["ok"] is False
    assert published["summary"] == {"published_groups": 0, "operation_count": 0, "blocked_count": 1}


async def test_manage_project_repo_status_reports_upstream_freshness(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import draft_state, publish

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    def fake_repo_draft_status(path):
        assert path == repo_dir
        return SimpleNamespace(changed_paths=[], unmerged_paths=[], errors=[])

    def fake_repo_upstream_status(path, *, changed_paths, base_branch, fetch):
        assert path == repo_dir
        assert changed_paths == []
        assert fetch is False
        return SimpleNamespace(
            status="changed",
            upstream_changed_paths=["README.md"],
            upstream_conflicted_paths=[],
            errors=[],
        )

    monkeypatch.setattr(draft_state, "repo_draft_status", fake_repo_draft_status)
    monkeypatch.setattr(publish, "repo_draft_status", fake_repo_draft_status)
    monkeypatch.setattr(draft_state, "repo_draft_upstream_status", fake_repo_upstream_status)

    with bind_agent_context({"run": _repo_project_run(repo_dir), "idea_id": "idea-1"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))

    resource = status["resources"][0]
    assert resource["status"] == "out_of_date"
    assert resource["out_of_date"] is True
    assert resource["changes"]["out_of_date_paths"] == ["README.md"]
    assert resource["details"]["upstream_status"] == "changed"
    assert plan["groups"][0]["status"] == "out_of_date"
    assert plan["groups"][0]["blocked_reasons"] == []


async def test_manage_project_draft_status_reports_clear_unbound_error():
    context = {"run": None, "idea_id": None, "workspace_ref": None, "target_ref": None, "execution_metadata": {}}
    with bind_agent_context(context):
        payload = json.loads(await projects._handle_manage_project(action="draft_status"))

    assert payload["ok"] is False
    assert payload["code"] == "project_thread_not_bound"
    assert "current AgentRun or Cortex thread" in payload["error"]


async def test_manage_project_draft_status_allows_empty_project():
    context = {
        "run": SimpleNamespace(id=123, metadata_={}),
        "idea_id": "idea-1",
        "workspace_ref": {
            "project_context_snapshot": {
                "name": "Empty project",
                "status": "validated",
                "resources": [],
            },
            "project_workspace_manifest": {
                "schema_version": 1,
                "mounts": [],
                "workspaces": [],
            },
        },
        "target_ref": {},
        "execution_metadata": {},
    }
    with bind_agent_context(context):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        versions = json.loads(await projects._handle_manage_project(action="root_versions"))

    assert status["ok"] is True
    assert status["resources"] == []
    assert status["changes"]["total"] == 0
    assert plan["ok"] is True
    assert plan["summary"] == {"resource_count": 0, "operation_count": 0, "blocked_count": 0}
    assert versions["ok"] is True
    assert versions["summary"] == {"resource_count": 0, "version_count": 0}


async def test_manage_project_schema_exposes_draft_operations():
    from brain.systems.runs.tool_definitions import PROJECT_TOOLS

    tool = next(item for item in PROJECT_TOOLS if item["name"] == "manage_project")
    actions = tool["input_schema"]["properties"]["action"]["enum"]

    assert "draft_status" in actions
    assert "plan_publish" in actions
    assert "refresh_draft_from_root" in actions
    assert "publish_draft" in actions
    assert "preview_root_version" in actions
    assert "publish_paths" in tool["input_schema"]["properties"]
    assert "path" in tool["input_schema"]["properties"]
    assert "check_upstream" in tool["input_schema"]["properties"]
    assert "base_branch" in tool["input_schema"]["properties"]

    guide = json.loads(await projects._handle_manage_project(action="schema", operation="draft_status"))
    assert guide["operation"] == "draft_status"
    assert "without mutating" in guide["effect"]
