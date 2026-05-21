from types import SimpleNamespace

from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import files


def _project_context(source_dir, draft_dir, *, manifest_mounts=True):
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
    manifest = {
        "workspace_root": str(draft_dir),
        "resolved_workspace_root": str(draft_dir),
        "workspaces": [{"name": "/reports", "path": str(draft_dir)}],
    }
    if manifest_mounts:
        manifest["mounts"] = [
            {
                "id": "/reports",
                "resource_id": "reports",
                "kind": "folder",
                "mount_path": "/reports",
                "workspace_path": str(draft_dir),
                "resource_path": str(draft_dir),
                "source_path": str(source_dir),
            }
        ]
    workspace_ref = {
        "workspace_root": str(draft_dir),
        "resolved_workspace_root": str(draft_dir),
        "workspaces": [{"name": "/reports", "path": str(draft_dir)}],
        "project_workspace_manifest": manifest,
        "project_context_snapshot": {"resources": [resource]},
    }
    return {
        "workspace_ref": workspace_ref,
        "target_ref": {"project_context_snapshot": {"resources": [resource]}},
        "run": SimpleNamespace(
            id=123,
            workspace_ref=workspace_ref,
            target_ref={"project_context_snapshot": {"resources": [resource]}},
            metadata_={"project_workspace_manifest": manifest},
        ),
    }


def test_read_file_resolves_project_mount_path_to_draft_workspace(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    other_workspace = tmp_path / "other"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    other_workspace.mkdir()
    (source_dir / "brief.md").write_text("source copy\n", encoding="utf-8")
    (draft_dir / "brief.md").write_text("draft copy\n", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_read_file("/reports/brief.md", _workspace=str(other_workspace))

    assert "error" not in result
    assert result["path"] == str(draft_dir / "brief.md")
    assert "draft copy" in result["content"]
    assert "source copy" not in result["content"]


def test_write_file_resolves_project_mount_path_to_draft_workspace(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_write_file("/reports/new.md", "draft only", _workspace=str(tmp_path))

    assert result["written"] is True
    assert result["path"] == str(draft_dir / "new.md")
    assert (draft_dir / "new.md").read_text(encoding="utf-8") == "draft only"
    assert not (source_dir / "new.md").exists()


def test_write_file_blocks_direct_project_source_path(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_write_file(str(source_file), "should not land", _workspace=str(tmp_path))

    assert "error" in result
    assert "Blocked write to Project source path" in result["error"]
    assert source_file.read_text(encoding="utf-8") == "source copy"
    assert not (draft_dir / "brief.md").exists()


def test_project_context_snapshot_resolves_mount_when_manifest_only_lists_workspaces(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    (draft_dir / "brief.md").write_text("draft copy\n", encoding="utf-8")
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy\n", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir, manifest_mounts=False)):
        read_result = files._handle_read_file("/reports/brief.md", _workspace=str(tmp_path))
        write_result = files._handle_write_file(str(source_file), "blocked", _workspace=str(tmp_path))

    assert "error" not in read_result
    assert read_result["path"] == str(draft_dir / "brief.md")
    assert "draft copy" in read_result["content"]
    assert "error" in write_result
    assert "Blocked write to Project source path" in write_result["error"]
    assert source_file.read_text(encoding="utf-8") == "source copy\n"


def test_edit_file_blocks_direct_project_source_path(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_edit_file(str(source_file), "source", "edited", _workspace=str(tmp_path))

    assert "error" in result
    assert "Blocked write to Project source path" in result["error"]
    assert source_file.read_text(encoding="utf-8") == "source copy"


def test_exec_command_blocks_absolute_project_source_write(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_exec_command(f"printf hacked > {source_file}", _workspace=str(tmp_path))

    assert result["blocked"] is True
    assert "Blocked command that may write to Project source path" in result["stderr"]
    assert source_file.read_text(encoding="utf-8") == "source copy"


def test_exec_command_blocks_relative_project_source_write_from_source_cwd(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy", encoding="utf-8")

    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_exec_command("printf hacked > brief.md", working_dir=str(source_dir), _workspace=str(tmp_path))

    assert result["blocked"] is True
    assert "Blocked command that may write to Project source path" in result["stderr"]
    assert source_file.read_text(encoding="utf-8") == "source copy"


def test_run_script_blocks_project_source_write(tmp_path):
    source_dir = tmp_path / "source" / "reports"
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    source_dir.mkdir(parents=True)
    draft_dir.mkdir(parents=True)
    source_file = source_dir / "brief.md"
    source_file.write_text("source copy", encoding="utf-8")

    script = f"from pathlib import Path\nPath({str(source_file)!r}).write_text('hacked')\n"
    with bind_agent_context(_project_context(source_dir, draft_dir)):
        result = files._handle_run_script(script, _workspace=str(tmp_path))

    assert result["exit_code"] == -1
    assert "Blocked script that may write to Project source path" in result["stderr"]
    assert source_file.read_text(encoding="utf-8") == "source copy"
