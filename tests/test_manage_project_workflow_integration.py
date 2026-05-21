import json
from pathlib import Path
from types import SimpleNamespace

from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import projects


def _project_run(run_id: int, project_root: Path, *, project_id: str = "profile-e2e"):
    return SimpleNamespace(
        id=run_id,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "id": project_id,
                "status": "validated",
                "resources": [
                    {
                        "id": "resource-folder",
                        "kind": "folder",
                        "mount_path": "/reports",
                        "path": str(project_root),
                    }
                ],
            },
        },
        workspace_ref={},
    )


def _patch_runs(monkeypatch, runs: dict[int, SimpleNamespace]) -> None:
    from brain.systems.cortex.project_context import materializer

    class FakeSession:
        async def get(self, _model, run_id):
            return runs.get(run_id)

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())


def _resource_workspace(run) -> Path:
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    return Path(resource["materialization"]["workspace_path"])


def _resource_source_root(run) -> Path:
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    return Path(resource["materialization"]["source_path"])


async def test_manage_project_local_draft_publish_is_visible_to_next_thread(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    project_root = tmp_path / "project-root"
    project_root.mkdir()
    (project_root / "brief.md").write_text("root v1", encoding="utf-8")
    (project_root / "delete.md").write_text("remove me", encoding="utf-8")

    first_run = _project_run(501, project_root)
    runs = {501: first_run}
    _patch_runs(monkeypatch, runs)

    materialized = await materialize_project_context_workspaces(
        501,
        workspace_root=str(tmp_path / "thread-one"),
        user_id="user-1",
        org_id="org-1",
    )
    draft_root = _resource_workspace(first_run)
    source_root = _resource_source_root(first_run)
    (draft_root / "brief.md").write_text("draft edit", encoding="utf-8")
    (draft_root / "new.md").write_text("new draft file", encoding="utf-8")
    (draft_root / "delete.md").unlink()

    with bind_agent_context({"run": first_run, "idea_id": "thread-one"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        published = json.loads(await projects._handle_manage_project(action="publish_draft"))
        plan_after_publish = json.loads(await projects._handle_manage_project(action="plan_publish"))

    assert materialized.ok
    assert status["resources"][0]["status"] == "modified"
    assert status["resources"][0]["changes"] == {
        "changed_paths": ["brief.md"],
        "new_paths": ["new.md"],
        "deleted_paths": ["delete.md"],
        "conflicted_paths": [],
    }
    assert plan["summary"] == {"resource_count": 1, "operation_count": 3, "blocked_count": 0}
    assert {(operation["operation"], operation["path"]) for operation in plan["groups"][0]["operations"]} == {
        ("update", "brief.md"),
        ("create", "new.md"),
        ("delete", "delete.md"),
    }
    assert published["ok"] is True
    assert published["mutated_project_root"] is True
    assert published["summary"] == {"published_groups": 1, "operation_count": 3, "blocked_count": 0}
    assert plan_after_publish["summary"] == {"resource_count": 1, "operation_count": 0, "blocked_count": 0}
    assert source_root != project_root
    assert (source_root / "brief.md").read_text(encoding="utf-8") == "draft edit"
    assert (source_root / "new.md").read_text(encoding="utf-8") == "new draft file"
    assert not (source_root / "delete.md").exists()
    assert (project_root / "brief.md").read_text(encoding="utf-8") == "root v1"

    second_run = _project_run(502, project_root)
    runs[502] = second_run
    second_materialized = await materialize_project_context_workspaces(
        502,
        workspace_root=str(tmp_path / "thread-two"),
        user_id="user-1",
        org_id="org-1",
    )

    second_draft = _resource_workspace(second_run)
    with bind_agent_context({"run": second_run, "idea_id": "thread-two"}):
        second_status = json.loads(await projects._handle_manage_project(action="draft_status"))

    assert second_materialized.ok
    assert (second_draft / "brief.md").read_text(encoding="utf-8") == "draft edit"
    assert (second_draft / "new.md").read_text(encoding="utf-8") == "new draft file"
    assert not (second_draft / "delete.md").exists()
    assert second_status["resources"][0]["status"] == "clean"
    assert second_status["changes"]["counts"] == {
        "changed_paths": 0,
        "new_paths": 0,
        "deleted_paths": 0,
        "conflicted_paths": 0,
    }


async def test_manage_project_out_of_date_draft_blocks_publish(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    project_root = tmp_path / "project-root"
    project_root.mkdir()
    (project_root / "brief.md").write_text("root v1", encoding="utf-8")

    run = _project_run(601, project_root, project_id="profile-conflict")
    _patch_runs(monkeypatch, {601: run})

    first_materialized = await materialize_project_context_workspaces(
        601,
        workspace_root=str(tmp_path / "thread-one"),
        user_id="user-1",
        org_id="org-1",
    )
    draft_root = _resource_workspace(run)
    source_root = _resource_source_root(run)
    (draft_root / "brief.md").write_text("thread draft edit", encoding="utf-8")
    (source_root / "brief.md").write_text("root v2", encoding="utf-8")

    refreshed = await materialize_project_context_workspaces(
        601,
        workspace_root=str(tmp_path / "thread-one"),
        user_id="user-1",
        org_id="org-1",
    )

    with bind_agent_context({"run": run, "idea_id": "thread-one"}):
        status = json.loads(await projects._handle_manage_project(action="draft_status"))
        plan = json.loads(await projects._handle_manage_project(action="plan_publish"))
        publish = json.loads(await projects._handle_manage_project(action="publish_draft"))

    assert first_materialized.ok
    assert refreshed.ok
    assert (draft_root / "brief.md").read_text(encoding="utf-8") == "thread draft edit"
    materialization_status = run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["draft_status"]
    assert materialization_status["conflicts"] == ["brief.md"]
    assert materialization_status["out_of_date"] == ["brief.md"]
    assert status["resources"][0]["status"] == "conflicted"
    assert status["resources"][0]["changes"]["conflicted_paths"] == ["brief.md"]
    assert status["resources"][0]["changes"]["out_of_date_paths"] == ["brief.md"]
    assert status["resources"][0]["out_of_date"] is True
    assert plan["summary"] == {"resource_count": 1, "operation_count": 1, "blocked_count": 1}
    assert plan["groups"][0]["status"] == "blocked"
    assert plan["groups"][0]["blocked_reasons"] == ["conflicted_paths_require_resolution"]
    assert publish["ok"] is False
    assert publish["mutated_project_root"] is False
    assert publish["summary"] == {"published_groups": 0, "operation_count": 0, "blocked_count": 1}
    assert (source_root / "brief.md").read_text(encoding="utf-8") == "root v2"
    assert (project_root / "brief.md").read_text(encoding="utf-8") == "root v1"
