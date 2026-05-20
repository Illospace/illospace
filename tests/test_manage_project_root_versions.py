import json
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
        id=223,
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


async def test_publish_draft_captures_root_versions_and_restore_refreshes_draft(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("draft edit", encoding="utf-8")

    with bind_agent_context({
        "run": _project_run(source_dir, draft_dir),
        "idea_id": "idea-versions",
        "user_id": "user-1",
        "org_id": "org-1",
    }):
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))
        listed = json.loads(await projects._handle_manage_project(action="root_versions", resource_id="reports"))
        before_id = published["published_groups"][0]["root_versions"]["before"]["version_id"]
        restored = json.loads(
            await projects._handle_manage_project(
                action="restore_root_version",
                resource_id="reports",
                version_id=before_id,
            )
        )
        plan_after_restore = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert published["ok"] is True
    versions = published["published_groups"][0]["root_versions"]
    assert versions["before"]["label"] == "before-draft-publish"
    assert versions["after"]["label"] == "after-draft-publish"
    assert versions["before"]["metadata"]["idea_id"] == "idea-versions"
    assert versions["before"]["metadata"]["publish_event"]["actor_id"] == "user-1"
    assert versions["before"]["metadata"]["publish_event"]["org_id"] == "org-1"
    assert versions["after"]["metadata"]["operation_count"] == 1
    assert [version["label"] for version in listed["groups"][0]["versions"]] == [
        "before-draft-publish",
        "after-draft-publish",
    ]
    assert restored["ok"] is True
    assert restored["restored_version"]["version_id"] == before_id
    assert restored["root_versions"]["before"]["label"] == "before-root-restore"
    assert restored["root_versions"]["after"]["label"] == "after-root-restore"
    assert restored["root_versions"]["before"]["metadata"]["publish_event"]["type"] == "project_root_restore"
    assert restored["root_versions"]["before"]["metadata"]["publish_event"]["actor_id"] == "user-1"
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "base"
    assert (draft_dir / "brief.md").read_text(encoding="utf-8") == "base"
    assert not (draft_dir / ".illo-project-history").exists()
    assert plan_after_restore["summary"] == {"resource_count": 1, "operation_count": 0, "blocked_count": 0}


async def test_preview_root_version_reports_restore_diff_without_mutating(tmp_path):
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "source"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("base", encoding="utf-8")
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("draft edit", encoding="utf-8")

    with bind_agent_context({"run": _project_run(source_dir, draft_dir), "idea_id": "idea-versions"}):
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))
        before_id = published["published_groups"][0]["root_versions"]["before"]["version_id"]
        preview = json.loads(
            await projects._handle_manage_project(
                action="preview_root_version",
                resource_id="reports",
                version_id=before_id,
            )
        )

    assert preview["ok"] is True
    assert preview["mutated_project_root"] is False
    assert preview["comparison"]["summary"]["modified_count"] == 1
    assert preview["comparison"]["modified"] == ["brief.md"]
    assert (source_dir / "brief.md").read_text(encoding="utf-8") == "draft edit"


async def test_restore_root_version_requires_resource_id_when_multiple_roots_are_attached(tmp_path):
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    draft_a = tmp_path / "thread" / ".illo-project-context" / "local" / "a"
    draft_b = tmp_path / "thread" / ".illo-project-context" / "local" / "b"
    source_a.mkdir()
    source_b.mkdir()
    draft_a.mkdir(parents=True)
    draft_b.mkdir(parents=True)

    run = _project_run(source_a, draft_a)
    resources = run.workspace_ref["project_context_snapshot"]["resources"]
    second = {
        **resources[0],
        "id": "memos",
        "mount_path": "/memos",
        "path": str(draft_b),
        "materialization": {
            **resources[0]["materialization"],
            "path": str(draft_b),
            "source_path": str(source_b),
            "workspace_path": str(draft_b),
        },
    }
    resources.append(second)
    run.target_ref["project_context_snapshot"]["resources"] = resources

    with bind_agent_context({"run": run, "idea_id": "idea-versions"}):
        payload = json.loads(await projects._handle_manage_project(action="restore_root_version", version_id="missing"))

    assert payload["ok"] is False
    assert payload["code"] == "resource_id_required"
    assert {item["resource_id"] for item in payload["resources"]} == {"reports", "memos"}
