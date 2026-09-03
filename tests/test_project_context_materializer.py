import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _ScalarRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None


def test_read_only_resource_path_reuses_sibling_managed_path(tmp_path):
    from brain.systems.cortex.project_context.resource_imports import backend_readable_resource_path

    sibling_path = tmp_path / "parent-worker" / ".illo-project-context" / "local" / "project"
    sibling_path.mkdir(parents=True)
    workspace_root = tmp_path / "child-worker"
    workspace_root.mkdir()

    existing_path, checked = backend_readable_resource_path(
        {"path": str(sibling_path)},
        workspace_root=workspace_root,
    )

    assert checked is True
    assert existing_path == sibling_path


def test_write_resource_path_rejects_sibling_but_preserves_other_path_behaviour(tmp_path):
    from brain.systems.cortex.project_context.resource_imports import (
        ResourcePathAccess,
        should_use_existing_resource_path,
    )

    workspace_root = tmp_path / "child-worker"
    own_path = workspace_root / ".illo-project-context" / "local" / "project"
    sibling_path = tmp_path / "parent-worker" / ".illo-project-context" / "local" / "project"
    external_path = tmp_path / "external-project"
    missing_path = tmp_path / "missing-project"
    for path in (own_path, sibling_path, external_path):
        path.mkdir(parents=True)

    assert should_use_existing_resource_path(
        str(sibling_path),
        workspace_root,
        access=ResourcePathAccess.WRITE,
    ) is None
    for access in ResourcePathAccess:
        assert should_use_existing_resource_path(
            str(own_path),
            workspace_root,
            access=access,
        ) == own_path
        assert should_use_existing_resource_path(
            str(external_path),
            workspace_root,
            access=access,
        ) == external_path
        assert should_use_existing_resource_path(
            str(missing_path),
            workspace_root,
            access=access,
        ) is None


def test_project_context_materialization_result_is_ready_when_evidence_is_degraded():
    from brain.systems.cortex.project_context.materializer import ProjectContextMaterializationResult

    result = ProjectContextMaterializationResult(
        workspaces=[
            {"name": "/", "path": "/tmp/project-root"},
            {"name": "repo-a", "path": "/tmp/repo-a"},
            {"name": "repo-b", "path": "/tmp/repo-b"},
        ],
        warnings=["Could not materialize GitHub repository example-org/missing: clone timed out."],
        degraded_resources=[
            {
                "kind": "repo",
                "name": "example-org/missing",
                "repo": "example-org/missing",
                "error": "Could not materialize GitHub repository example-org/missing: clone timed out.",
            }
        ],
    )

    assert result.ready is True
    assert result.ok is True
    assert result.status == "degraded"
    assert result.evidence_health["status"] == "degraded"
    assert result.evidence_health["degraded_resources"][0]["repo"] == "example-org/missing"


def test_thread_attachment_file_is_not_materialized_as_github_workspace():
    from brain.systems.cortex.project_context.materializer import _github_slug_from_resource

    assert _github_slug_from_resource({
        "kind": "file",
        "name": "agent.md",
        "path": "/app/brain/uploads/94fe1c1fe9134e6aaa7b4f9844c7a6a6.md",
        "source": "thread_attachment",
        "uri": "/static/uploads/94fe1c1fe9134e6aaa7b4f9844c7a6a6.md",
    }) is None


def test_project_context_materializable_resources_include_empty_project_roots():
    from brain.systems.cortex.project_context.materializer import project_context_has_materializable_resources

    assert project_context_has_materializable_resources({"resources": []}) is True
    assert project_context_has_materializable_resources({
        "resources": [
            {
                "id": "placeholder-folder",
                "kind": "folder",
                "name": "agent-mission-control/workspaces/linkedin-outbound",
                "uri": "browser-folder://pending",
            }
        ]
    }) is True
    assert project_context_has_materializable_resources({
        "resources": [
            {
                "id": "github-folder",
                "kind": "folder",
                "name": "agent-mission-control/workspaces/linkedin-outbound",
                "uri": "github://uwear-ai/agent-mission-control/workspaces/linkedin-outbound",
            }
        ]
    }) is True


def test_github_clone_uses_lightweight_command_and_configurable_timeout(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import _clone_github_repo

    calls = []

    def fake_run_subprocess(command, **kwargs):
        kwargs = {**kwargs, "env": dict(kwargs.get("env") or {})}
        calls.append({"command": command, **kwargs})
        Path(command[-1]).mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_git_output(_cwd, *args):
        if args == ("branch", "--show-current"):
            return "main"
        if args == ("rev-parse", "HEAD"):
            return "abc123"
        return None

    monkeypatch.setenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", "900")
    monkeypatch.setattr(materializer, "run_subprocess_sync", fake_run_subprocess)
    monkeypatch.setattr(materializer, "_git_output", fake_git_output)

    destination = tmp_path / "repo"
    result = _clone_github_repo(
        "example-org/private-repo",
        destination,
        token="secret-token",
        branch="main",
    )

    assert result == {"path": str(destination), "branch": "main", "commit": "abc123"}
    assert len(calls) == 1
    assert calls[0]["command"] == [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--filter",
        "blob:none",
        "--branch",
        "main",
        "https://github.com/example-org/private-repo.git",
        str(destination),
    ]
    assert calls[0]["timeout"] == 900
    assert calls[0]["capture_output"] is True
    assert calls[0]["text"] is True
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls[0]["env"]["ILLO_GITHUB_PROJECT_CONTEXT_TOKEN"] == "secret-token"
    assert "secret-token" not in " ".join(calls[0]["command"])


async def test_github_clone_keeps_event_loop_responsive(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer

    clone_started = threading.Event()
    release_clone = threading.Event()
    released_by_event_loop = []

    def fake_run_subprocess(command, **_kwargs):
        clone_started.set()
        # The timeout is deadlock protection, not a timing assertion: only the
        # event loop can set release_clone, so if the clone were still running
        # ON the loop this returns False instead of hanging the suite. Keep it
        # generous — a tight bound here would flake on a loaded CI box.
        released_by_event_loop.append(release_clone.wait(timeout=30))
        Path(command[-1]).mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_git_output(_cwd, *args):
        if args == ("branch", "--show-current"):
            return "main"
        if args == ("rev-parse", "HEAD"):
            return "abc123"
        return None

    async def release_once_loop_is_responsive():
        # This coroutine can only make progress while the clone is in flight if
        # the clone is NOT holding the event loop. That is the whole assertion:
        # no sleeps, no tick counting, just "did the loop keep scheduling".
        while not clone_started.is_set():
            await asyncio.sleep(0)
        release_clone.set()

    monkeypatch.setattr(materializer, "run_subprocess_sync", fake_run_subprocess)
    monkeypatch.setattr(materializer, "_git_output", fake_git_output)

    resource = {
        "kind": "repo",
        "name": "example-org/example-repo",
        "repo": "example-org/example-repo",
        "uri": "https://github.com/example-org/example-repo",
        "branch": "main",
    }
    materialization, _ = await asyncio.gather(
        materializer._materialize_resource(
            resource,
            workspace_root=tmp_path,
            user_id=None,
            org_id=None,
        ),
        release_once_loop_is_responsive(),
    )

    workspace, error = materialization
    assert error is None
    assert workspace == {
        "name": "example-org/example-repo",
        "path": str(tmp_path / ".illo-project-context" / "github" / "example-org" / "example-repo"),
    }
    # False here means the clone blocked the loop until the timeout expired.
    assert released_by_event_loop == [True]


def test_github_clone_timeout_env_is_bounded(monkeypatch):
    from brain.systems.cortex.project_context.materializer import _git_clone_timeout_seconds

    monkeypatch.delenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", raising=False)
    assert _git_clone_timeout_seconds() == 600
    monkeypatch.setenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", "5")
    assert _git_clone_timeout_seconds() == 30
    monkeypatch.setenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", "3600")
    assert _git_clone_timeout_seconds() == 1800
    monkeypatch.setenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", "not-an-int")
    assert _git_clone_timeout_seconds() == 600


def test_project_root_key_uses_canonical_project_key():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context({"project_key": "project-1"}) == "project-1"


def test_project_root_key_prefers_picker_profile_identity_over_slug_key():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context({
        "name": "test empty project",
        "project_key": "test-empty-project",
        "project_profile_id": "fec2d533-e4a0-40e7-9055-b5b619e91ab6",
        "selected_profile_id": "server:fec2d533-e4a0-40e7-9055-b5b619e91ab6",
    }) == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"
    assert project_key_from_context({
        "name": "test empty project",
        "selected_profile_id": "server:fec2d533-e4a0-40e7-9055-b5b619e91ab6",
    }) == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"


def test_project_root_key_ignores_current_thread_project_picker_sentinel():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context({
        "name": "test empty project",
        "selected_profile_id": "current-thread-project",
    }) == "test-empty-project"


def test_project_root_key_ignores_ambiguous_top_level_id():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context({"id": "context-1"}, fallback="run-73") == "run-73"


def test_project_root_key_does_not_use_child_resource_id():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context(
        {},
        resources=[{"id": "project-root", "kind": "folder", "mount_path": "/reports"}],
        fallback="run-73",
    ) == "run-73"


def test_project_root_fingerprint_ignores_child_resource_ids():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    first = project_key_from_context(
        {"description": "scratch"},
        resources=[{"id": "child-a", "kind": "file"}],
    )
    second = project_key_from_context(
        {"description": "scratch"},
        resources=[{"id": "child-b", "kind": "file"}],
    )

    assert first == second


def test_validated_snapshot_stamps_project_picker_profile_identity():
    from brain.systems.cortex.project_context.snapshot import validated_project_context_snapshot

    snapshot = validated_project_context_snapshot(
        {
            "name": "test empty project",
            "project_profile_id": "fec2d533-e4a0-40e7-9055-b5b619e91ab6",
            "selected_profile_id": "server:fec2d533-e4a0-40e7-9055-b5b619e91ab6",
            "resources": [],
        },
        validate_local_paths=False,
    )

    assert snapshot["project_id"] == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"
    assert snapshot["project_key"] == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"
    assert snapshot["project_workspace_manifest"]["project_id"] == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"
    assert snapshot["project_workspace_manifest"]["project_key"] == "fec2d533-e4a0-40e7-9055-b5b619e91ab6"


def test_runner_materializes_thread_attachment_files_as_project_resources(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=51,
        root_run_id=None,
        user_id="user-1",
        org_id="org-1",
        thread_id="idea-4",
        target_ref={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "attachment-1",
                        "kind": "file",
                        "name": "agent.md",
                        "path": "/app/brain/uploads/agent.md",
                        "source": "thread_attachment",
                        "uri": "/static/uploads/agent.md",
                    }
                ],
            }
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(runner, "_async_record_project_activity", AsyncMock())
    monkeypatch.setattr(
        runner,
        "materialize_project_context_workspaces",
        AsyncMock(return_value=SimpleNamespace(ok=True, workspaces=[{"name": "agent.md", "path": "/tmp/thread"}], errors=[])),
    )

    context_ready, status_payload = runner._materialize_project_context(51)

    assert context_ready is True
    assert status_payload is None


def test_runner_materializes_placeholder_projects_as_empty_roots(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=52,
        root_run_id=None,
        user_id="user-1",
        org_id="org-1",
        thread_id="idea-5",
        target_ref={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "placeholder-folder",
                        "kind": "folder",
                        "name": "not-yet-uploaded",
                        "uri": "browser-folder://not-yet-uploaded",
                    }
                ],
            }
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(runner, "_async_record_project_activity", AsyncMock())
    monkeypatch.setattr(
        runner,
        "materialize_project_context_workspaces",
        AsyncMock(return_value=SimpleNamespace(
            ok=True,
            workspaces=[{"name": "/", "path": "/tmp/project-root"}],
            errors=[],
        )),
    )

    context_ready, status_payload = runner._materialize_project_context(52)

    assert context_ready is True
    assert status_payload is None
    runner.materialize_project_context_workspaces.assert_called_once()


def test_runner_starts_when_project_context_materialization_is_degraded(monkeypatch):
    from brain.systems.cortex.project_context.materializer import ProjectContextMaterializationResult
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=281,
        root_run_id=None,
        user_id="user-1",
        org_id="org-1",
        thread_id="idea-281",
        target_ref={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {"kind": "repo", "repo": "example-org/repo-a"},
                    {"kind": "repo", "repo": "example-org/missing-repo"},
                ],
            }
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    result = ProjectContextMaterializationResult(
        workspaces=[
            {"name": "/", "path": "/tmp/project-root"},
            {"name": "example-org/repo-a", "path": "/tmp/repo-a"},
        ],
        warnings=["Could not materialize GitHub repository example-org/missing-repo: clone timed out."],
        degraded_resources=[
            {
                "kind": "repo",
                "repo": "example-org/missing-repo",
                "error": "Could not materialize GitHub repository example-org/missing-repo: clone timed out.",
            }
        ],
    )
    mark_failed = AsyncMock()

    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(runner, "_async_record_project_activity", AsyncMock())
    monkeypatch.setattr(runner, "materialize_project_context_workspaces", AsyncMock(return_value=result))
    monkeypatch.setattr(runner, "_mark_run_failed_after_runner_error_async", mark_failed)

    context_ready, status_payload = runner._materialize_project_context(281)

    assert context_ready is True
    assert status_payload is None
    assert result.status == "degraded"
    mark_failed.assert_not_awaited()


async def test_materialize_github_project_context_uses_vault_key_without_persisting_token(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=42,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/example-backend",
                        "repo": "example-org/example-backend",
                        "uri": "https://github.com/example-org/example-backend",
                        "branch": "main",
                        "credential_ref": {
                            "type": "vault_secret",
                            "provider": "github",
                            "key_name": "GITHUB_EXAMPLE_TOKEN",
                        },
                    },
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    clone_calls = []

    async def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_EXAMPLE_TOKEN"
        assert kwargs["actor_user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["accessed_by"] == "github_runtime_tool"
        return "test-private-token"

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append({"slug": slug, "destination": destination, "token": token, "branch": branch})
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch, "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "async_get_secret", fake_get_secret)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(
        42,
        workspace_root=str(tmp_path),
        user_id="user-1",
        org_id="org-1",
    )

    assert result.ok
    root_draft = tmp_path / ".illo-project-context" / "local" / "run-42" / "project-root"
    assert result.workspaces == [
        {"name": "/", "path": str(root_draft)},
        {
            "name": "example-org/example-backend",
            "path": str(tmp_path / ".illo-project-context" / "github" / "example-org" / "example-backend"),
        }
    ]
    assert clone_calls == [
        {
            "slug": "example-org/example-backend",
            "destination": tmp_path / ".illo-project-context" / "github" / "example-org" / "example-backend",
            "token": "test-private-token",
            "branch": "main",
        }
    ]
    resource = run.target_metadata["project_context_snapshot"]["resources"][1]
    assert run.target_metadata["project_context_snapshot"]["resources"][0]["mount_path"] == "/"
    assert resource["path"] == result.workspaces[1]["path"]
    assert resource["materialization"]["status"] == "ready"
    assert run.metadata_["workspaces"] == result.workspaces
    assert "test-private-token" not in str(run.metadata_)
    assert "test-private-token" not in str(run.target_metadata)


async def test_materialize_github_project_context_reports_explicit_vault_failure(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=55,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/private-repo",
                        "repo": "example-org/private-repo",
                        "uri": "https://github.com/example-org/private-repo",
                        "branch": "main",
                        "credential_ref": {
                            "type": "vault_secret",
                            "provider": "github",
                            "key_name": "GITHUB_EXAMPLE_TOKEN",
                        },
                    },
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_EXAMPLE_TOKEN"
        assert kwargs["actor_user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["accessed_by"] == "github_runtime_tool"
        return None

    def fake_clone(*_args, **_kwargs):
        raise AssertionError("explicit vault credentials should not fall through to public clone")

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "async_get_secret", fake_get_secret)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(
        55,
        workspace_root=str(tmp_path),
        user_id="user-1",
        org_id="org-1",
    )

    assert not result.ok
    assert len(result.errors) == 1
    assert "Vault key GITHUB_EXAMPLE_TOKEN: Vault secret was not found or is empty." in result.errors[0]
    assert "public clone" not in result.errors[0]
    resource = run.target_metadata["project_context_snapshot"]["resources"][1]
    assert resource["materialization"]["error"] == "Vault key GITHUB_EXAMPLE_TOKEN: Vault secret was not found or is empty."
    assert run.target_status == "invalid"


async def test_spawned_reader_materializes_with_parent_project_bound_github_credential(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=311,
        parent_run_id=42,
        user_id="user-1",
        org_id="org-1",
        metadata_={
            "origin": "spawn_worker",
            "spawned_by_tool": True,
            "worker_role": "repo_reader",
        },
        target_ref={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "repo": "uwear-ai/uwear-backend",
                        "uri": "https://github.com/uwear-ai/uwear-backend",
                    }
                ],
            }
        },
        workspace_ref={},
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append({"slug": slug, "token": token})
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("reader evidence", encoding="utf-8")
        return {"path": str(destination), "branch": branch or "main", "commit": "reader123"}

    resolve_project_bound = AsyncMock(
        return_value={"GITHUB_TOKEN__COORDINATOR": "parent-working-token"}
    )
    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        materializer,
        "async_resolve_project_bound_env_tokens",
        resolve_project_bound,
        raising=False,
    )
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(
        run.id,
        workspace_root=str(tmp_path / "headless-worker"),
        user_id=run.user_id,
        org_id=run.org_id,
    )

    assert result.ok
    assert clone_calls == [{"slug": "uwear-ai/uwear-backend", "token": "parent-working-token"}]
    resolve_project_bound.assert_awaited_once_with(
        actor_user_id="user-1",
        org_id="org-1",
        project_slug="uwear-ai/uwear-backend",
    )
    assert (Path(result.workspaces[1]["path"]) / "README.md").read_text(encoding="utf-8") == "reader evidence"
    assert "parent-working-token" not in str(run.target_ref)
    assert "parent-working-token" not in str(run.workspace_ref)
    assert "parent-working-token" not in str(run.metadata_)


async def test_materialize_github_folder_uri_mounts_repo_subpath(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=54,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "resource-2",
                        "kind": "folder",
                        "name": "agent-mission-control/workspaces/linkedin-outbound",
                        "label": "agent-mission-control/workspaces/linkedin-outbound",
                        "uri": "github://uwear-ai/agent-mission-control/workspaces/linkedin-outbound",
                    }
                ],
            },
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        assert slug == "uwear-ai/agent-mission-control"
        workspace = destination / "workspaces" / "linkedin-outbound"
        workspace.mkdir(parents=True)
        (workspace / "README.md").write_text("LinkedIn workflow", encoding="utf-8")
        return {"path": str(destination), "branch": branch or "main", "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "_token_candidates", AsyncMock(return_value=[(None, None)]))
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(
        54,
        workspace_root=str(tmp_path / "thread-root"),
        user_id="user-1",
        org_id="org-1",
    )

    expected_repo = tmp_path / "thread-root" / ".illo-project-context" / "github" / "uwear-ai" / "agent-mission-control"
    expected_workspace = expected_repo / "workspaces" / "linkedin-outbound"
    expected_root = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-54" / "project-root"
    assert result.ok
    assert result.workspaces == [
        {"name": "/", "path": str(expected_root)},
        {"name": "agent-mission-control/workspaces/linkedin-outbound", "path": str(expected_workspace)},
    ]
    resource = run.target_ref["project_context_snapshot"]["resources"][1]
    assert resource["repo"] == "uwear-ai/agent-mission-control"
    assert resource["path"] == str(expected_workspace)
    assert resource["materialization"]["repo_path"] == str(expected_repo)
    assert resource["materialization"]["subpath"] == "workspaces/linkedin-outbound"
    assert resource["materialization"]["workspace_path"] == str(expected_workspace)


async def test_materialize_github_project_context_finds_general_github_token(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=52,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "github_repo",
                        "name": "example-org/private-repo",
                        "uri": "https://github.com/example-org/private-repo",
                    },
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_list_secrets(actor_user_id, category=None, *, org_id=None):
        assert actor_user_id == "user-1"
        assert org_id == "org-1"
        if category == "github":
            return []
        assert category is None
        return [
            {"key_name": "STRIPE_SECRET", "category": "general"},
            {"key_name": "GITHUB_TOKEN", "category": "general"},
        ]

    async def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_TOKEN"
        assert kwargs["actor_user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["accessed_by"] == "github_runtime_tool"
        return "general-github-token"

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append({"slug": slug, "token": token, "branch": branch})
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", fake_list_secrets)
    monkeypatch.setattr(materializer, "async_get_secret", fake_get_secret)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(
        52,
        workspace_root=str(tmp_path),
        user_id="user-1",
        org_id="org-1",
    )

    assert result.ok
    assert clone_calls == [
        {
            "slug": "example-org/private-repo",
            "token": "general-github-token",
            "branch": None,
        }
    ]
    assert "general-github-token" not in str(run.metadata_)
    assert "general-github-token" not in str(run.target_metadata)


async def test_materialize_github_project_context_fails_closed_when_clone_unavailable(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=43,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/private",
                        "repo": "example-org/private",
                        "uri": "https://github.com/example-org/private",
                    },
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        materializer,
        "_clone_github_repo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Repository not found")),
    )

    result = await materialize_project_context_workspaces(43, workspace_root=str(tmp_path), user_id="user-1")

    assert not result.ok
    assert "Could not materialize GitHub repository example-org/private" in result.errors[0]
    assert run.target_status == "invalid"
    assert run.target_metadata["project_context_snapshot"]["status"] == "invalid"
    assert run.metadata_["project_context_materialization"]["status"] == "failed"


async def test_materialize_multi_repo_context_degrades_when_one_clone_is_unavailable(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    repos = [
        "example-org/repo-a",
        "example-org/repo-b",
        "example-org/missing-repo",
        "example-org/repo-c",
    ]
    run = SimpleNamespace(
        id=281,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": repo,
                        "repo": repo,
                        "uri": f"https://github.com/{repo}",
                    }
                    for repo in repos
                ],
            },
        },
        workspace_ref={},
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        if slug == "example-org/missing-repo":
            raise RuntimeError("clone timed out")
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": f"commit-{slug[-1]}"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(281, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ready is True
    assert result.ok is True
    assert result.status == "degraded"
    assert result.errors == []
    assert len(result.workspaces) == 4
    assert "example-org/missing-repo" in result.warnings[0]
    assert result.degraded_resources[0]["repo"] == "example-org/missing-repo"
    assert run.target_status == "resolved"
    assert run.target_validation_error is None
    assert run.target_metadata["project_context_snapshot"]["status"] == "validated"

    materialization_payload = run.metadata_["project_context_materialization"]
    assert materialization_payload["status"] == "degraded"
    assert materialization_payload["evidence_health"]["status"] == "degraded"
    assert materialization_payload["degraded_resources"][0]["repo"] == "example-org/missing-repo"
    assert (
        run.target_metadata["project_context_snapshot"]["resources"][3]["materialization"]["status"]
        == "failed"
    )

    runtime_materialization = run.workspace_ref["project_runtime_context"]["project_context_materialization"]
    assert runtime_materialization["evidence_health"]["status"] == "degraded"
    assert runtime_materialization["degraded_resources"][0]["repo"] == "example-org/missing-repo"


async def test_materialize_multi_repo_context_stays_materialized_when_all_clones_succeed(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    repos = [f"example-org/repo-{suffix}" for suffix in ("a", "b", "c", "d")]
    run = SimpleNamespace(
        id=282,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": repo,
                        "repo": repo,
                        "uri": f"https://github.com/{repo}",
                    }
                    for repo in repos
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": f"commit-{slug[-1]}"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(282, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ready is True
    assert result.status == "materialized"
    assert result.warnings == []
    assert result.degraded_resources == []
    assert len(result.workspaces) == 5
    assert run.metadata_["project_context_materialization"]["status"] == "materialized"
    assert run.metadata_["project_context_materialization"]["evidence_health"] == {"status": "ok"}


async def test_materialize_required_repo_failure_stays_fail_closed_with_other_usable_repo(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=283,
        user_id="user-1",
        metadata_={"org_id": "org-1"},
        target_metadata={
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/required-repo",
                        "repo": "example-org/required-repo",
                        "uri": "https://github.com/example-org/required-repo",
                        "required": True,
                    },
                    {
                        "kind": "repo",
                        "name": "example-org/healthy-repo",
                        "repo": "example-org/healthy-repo",
                        "uri": "https://github.com/example-org/healthy-repo",
                    },
                ],
            },
        },
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        if slug == "example-org/required-repo":
            raise RuntimeError("repository unavailable")
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "healthy123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(283, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ready is False
    assert result.status == "failed"
    assert "example-org/required-repo" in result.errors[0]
    assert len(result.workspaces) == 2
    assert run.target_status == "invalid"
    assert run.metadata_["project_context_materialization"]["status"] == "failed"



async def test_materialize_agent_run_workspace_ref_project_context_updates_workspace_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=44,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={"kind": "cortex_idea"},
        workspace_ref={
            "resources": [
                {
                    "kind": "repo",
                    "name": "example-org/example-backend",
                    "repo": "example-org/example-backend",
                    "uri": "https://github.com/example-org/example-backend",
                    "branch": "main",
                }
            ]
        },
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch, "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(44, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ok
    assert len(result.workspaces) == 2
    assert result.workspaces[0]["name"] == "/"
    assert result.workspaces[1]["name"] == "example-org/example-backend"
    assert run.workspace_ref["workspace_root"] == result.workspaces[1]["path"]
    assert run.workspace_ref["resolved_workspace_root"] == result.workspaces[1]["path"]
    assert run.workspace_ref["project_workspace_manifest"]["workspace_root"] == result.workspaces[1]["path"]
    assert run.workspace_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]
    assert run.workspace_ref["project_context_snapshot"]["resources"][1]["path"] == result.workspaces[1]["path"]
    assert run.target_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]
    assert run.target_ref["project_context_snapshot"]["resources"][1]["path"] == result.workspaces[1]["path"]


async def test_materialize_reuses_existing_thread_checkout_without_reclone(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    checkout = tmp_path / ".illo-project-context" / "github" / "example-org" / "example-repo"
    (checkout / ".git").mkdir(parents=True)
    sentinel = checkout / "local-change.txt"
    sentinel.write_text("preserve me")

    run = SimpleNamespace(
        id=45,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/example-repo",
                        "repo": "example-org/example-repo",
                        "uri": "https://github.com/example-org/example-repo",
                        "branch": "main",
                    }
                ],
            },
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def fake_git_output(cwd, *args):
        assert cwd == checkout
        if args == ("rev-parse", "--show-toplevel"):
            return str(checkout)
        if args == ("branch", "--show-current"):
            return "main"
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/example-org/example-repo.git"
        if args == ("rev-parse", "HEAD"):
            return "existing123"
        return None

    clone_calls = []

    def fake_clone(*args, **kwargs):
        clone_calls.append((args, kwargs))
        raise AssertionError("existing checkout should not be recloned")

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "_git_output", fake_git_output)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(45, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ok
    assert clone_calls == []
    assert sentinel.read_text() == "preserve me"
    root_draft = tmp_path / ".illo-project-context" / "local" / "run-45" / "project-root"
    assert result.workspaces == [
        {"name": "/", "path": str(root_draft)},
        {"name": "example-org/example-repo", "path": str(checkout)},
    ]
    resource = run.target_ref["project_context_snapshot"]["resources"][1]
    assert resource["path"] == str(checkout)
    assert resource["materialization"] == {
        "status": "ready",
        "provider": "github",
        "repo": "example-org/example-repo",
        "branch": "main",
        "commit": "existing123",
        "credential": "existing",
        "reused": True,
    }


async def test_materialize_reclones_instead_of_reusing_sibling_worker_checkout(tmp_path, monkeypatch):
    """#877 isolation: a sibling worker's managed checkout is never adopted as this worker's write path."""
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    sibling_checkout = (
        tmp_path / "parent-worker" / ".illo-project-context" / "github" / "example-org" / "example-repo"
    )
    (sibling_checkout / ".git").mkdir(parents=True)
    sibling_sentinel = sibling_checkout / "sibling-change.txt"
    sibling_sentinel.write_text("belongs to the sibling")
    workspace_root = tmp_path / "child-worker"
    workspace_root.mkdir()
    expected_checkout = workspace_root / ".illo-project-context" / "github" / "example-org" / "example-repo"

    run = SimpleNamespace(
        id=47,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/example-repo",
                        "repo": "example-org/example-repo",
                        "path": str(sibling_checkout),
                        "uri": "https://github.com/example-org/example-repo",
                        "branch": "main",
                    }
                ],
            },
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append(destination)
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "child123"}

    def fail_on_sibling_probe(cwd, *args):
        if Path(cwd).resolve() == sibling_checkout.resolve():
            raise AssertionError("sibling checkout must not be probed for reuse")
        return None

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_git_output", fail_on_sibling_probe)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(47, workspace_root=str(workspace_root), user_id="user-1")

    assert result.ok
    assert clone_calls == [expected_checkout]
    assert sibling_sentinel.read_text() == "belongs to the sibling"
    root_draft = workspace_root / ".illo-project-context" / "local" / "run-47" / "project-root"
    assert result.workspaces == [
        {"name": "/", "path": str(root_draft)},
        {"name": "example-org/example-repo", "path": str(expected_checkout)},
    ]
    resource = run.target_ref["project_context_snapshot"]["resources"][1]
    assert resource["path"] == str(expected_checkout)
    assert "reused" not in resource["materialization"]


async def test_materialize_ignores_stale_managed_run_path_and_uses_thread_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    stale_checkout = tmp_path / "run-99" / ".illo-project-context" / "github" / "example-org" / "example-repo"
    stale_checkout.mkdir(parents=True)
    thread_root = tmp_path / "ideas" / "idea-1"
    expected_checkout = thread_root / ".illo-project-context" / "github" / "example-org" / "example-repo"

    run = SimpleNamespace(
        id=46,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/example-repo",
                        "repo": "example-org/example-repo",
                        "path": str(stale_checkout),
                        "uri": "https://github.com/example-org/example-repo",
                    }
                ],
            },
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append(destination)
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "new123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(46, workspace_root=str(thread_root), user_id="user-1")

    assert result.ok
    assert clone_calls == [expected_checkout]
    root_draft = thread_root / ".illo-project-context" / "local" / "run-46" / "project-root"
    assert result.workspaces == [
        {"name": "/", "path": str(root_draft)},
        {"name": "example-org/example-repo", "path": str(expected_checkout)},
    ]
    resource = run.target_ref["project_context_snapshot"]["resources"][1]
    assert resource["path"] == str(expected_checkout)


async def test_materialize_refuses_to_overwrite_non_matching_thread_checkout(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    checkout = tmp_path / ".illo-project-context" / "github" / "example-org" / "example-repo"
    checkout.mkdir(parents=True)
    (checkout / "local-change.txt").write_text("do not delete")

    run = SimpleNamespace(
        id=47,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "kind": "repo",
                        "name": "example-org/example-repo",
                        "repo": "example-org/example-repo",
                        "uri": "https://github.com/example-org/example-repo",
                    }
                ],
            },
        },
        workspace_ref={},
        target_status="resolved",
        target_validation_error=None,
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(*args, **kwargs):
        clone_calls.append((args, kwargs))
        raise AssertionError("non-matching checkout should not be overwritten")

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "_git_output", lambda *_args: None)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = await materialize_project_context_workspaces(47, workspace_root=str(tmp_path), user_id="user-1")

    assert not result.ok
    assert clone_calls == []
    assert (checkout / "local-change.txt").read_text() == "do not delete"
    assert "refusing to overwrite live workspace state" in result.errors[0]
    assert run.target_status == "invalid"

def test_project_context_root_is_scoped_by_thread(monkeypatch, tmp_path):
    from brain.systems.runs.cortex.runner import _project_context_root

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert _project_context_root(101, thread_id="idea/with spaces") == str(tmp_path / "ideas" / "idea-with-spaces")
    assert _project_context_root(101, thread_id=None) == str(tmp_path / "run-101")


def test_project_context_root_uses_the_headless_worker_codec(monkeypatch, tmp_path):
    """The GC parses this directory name back to a run id and deletes it.

    Pin the exact path here, at the caller, so a runner cleanup cannot bypass
    the codec while the codec's own tests still pass.
    """

    from brain.systems.runs.cortex.runner import _project_context_root
    from brain.systems.runs.headless_worker_identity import (
        build_headless_worker_thread_id,
    )

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    thread_id = build_headless_worker_thread_id(16034, "0434cc5ed822d9f7")

    assert _project_context_root(103, thread_id=thread_id) == str(
        tmp_path / "ideas" / "headless-worker-16034-0434cc5ed822d9f7"
    )


def test_project_context_root_uses_workspace_root_in_deploy(monkeypatch, tmp_path):
    from brain.systems.runs.cortex.runner import _project_context_root

    monkeypatch.delenv("ILLO_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert _project_context_root(102, thread_id="idea-2") == str(tmp_path / "ideas" / "idea-2")


def test_runner_fails_fast_when_project_context_has_no_workspace(monkeypatch):
    from brain.systems.runs.failure_diagnostic import RunFailureStage
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=49,
        user_id="user-1",
        org_id="org-1",
        thread_id="idea-3",
        target_ref={
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "repo",
                        "repo": "example-org/missing-repo",
                        "uri": "https://github.com/example-org/missing-repo",
                    }
                ]
            }
        },
        workspace_ref={},
    )

    class FakeSession:
        async def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    captured = {}

    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    record_activity = AsyncMock()
    monkeypatch.setattr(runner, "_async_record_project_activity", record_activity)
    monkeypatch.setattr(
        runner,
        "materialize_project_context_workspaces",
        AsyncMock(return_value=SimpleNamespace(
            ok=False,
            workspaces=[],
            errors=["Could not materialize GitHub repository example-org/missing-repo: repository not found."],
        )),
    )

    async def fake_mark_failed(run_id, error, *, final_answer=None, failure_stage=None):
        captured.update({
            "run_id": run_id,
            "error": error,
            "final_answer": final_answer,
            "failure_stage": failure_stage,
        })
        return {"idea_id": "idea-3", "new_status": "failed"}

    monkeypatch.setattr(runner, "_mark_run_failed_after_runner_error_async", fake_mark_failed)

    context_ready, status_payload = runner._materialize_project_context(49)

    assert context_ready is False
    assert status_payload == {"idea_id": "idea-3", "new_status": "failed"}
    assert captured["run_id"] == 49
    assert "Project Context unavailable" in captured["error"]
    assert "did not provide a usable workspace" in captured["final_answer"]
    assert captured["failure_stage"] == RunFailureStage.PROJECT_CONTEXT_MATERIALIZATION
    public_activity = record_activity.await_args_list[-1]
    assert public_activity.args[2] == "Project context unavailable"
    assert public_activity.kwargs["issue_count"] == 1
    assert "errors" not in public_activity.kwargs
    assert "repository not found" not in str(public_activity)


@pytest.mark.parametrize("materialization_ok", [False, True])
async def test_spawned_reader_materialization_issue_degrades_parent_cycle_evidence_health(
    monkeypatch,
    materialization_ok,
):
    from brain.systems.runs.cortex import runner

    child = SimpleNamespace(
        id=49,
        parent_run_id=42,
        root_run_id=42,
        user_id="user-1",
        org_id="org-1",
        thread_id="headless-worker:42:reader",
        metadata_={
            "origin": "spawn_worker",
            "spawned_by_tool": True,
            "worker_role": "repo_reader",
        },
        target_ref={
            "project_context_snapshot": {
                "resources": [
                    {
                        "kind": "repo",
                        "repo": "example-org/missing-repo",
                        "uri": "https://github.com/example-org/missing-repo",
                    }
                ]
            }
        },
        workspace_ref={},
    )
    parent = SimpleNamespace(
        id=42,
        root_run_id=42,
        metadata_={
            "source": "cycle",
            "cycle_run_id": 12,
            "evidence_health": {"status": "pending"},
        },
    )
    cycle_run = SimpleNamespace(
        id=12,
        context_snapshot={"evidence_health": {"status": "pending", "expected_checks": ["github"]}},
    )

    lock_order = []

    rows_by_id = {49: child, 42: parent, 12: cycle_run}

    class FakeSession:
        async def get(self, _model, object_id, **_kwargs):
            if _kwargs.get("with_for_update"):
                lock_order.append((_model.__name__, object_id))
            return rows_by_id.get(object_id)

        async def scalars(self, stmt):
            entity = stmt.column_descriptions[0]["entity"]
            object_id = int(stmt.whereclause.right.value)
            if stmt.get_execution_options().get("populate_existing") is not True:
                raise AssertionError(
                    f"locked read of {entity.__name__} must refresh the identity map"
                )
            if stmt._for_update_arg is not None:
                lock_order.append((entity.__name__, object_id))
            return _ScalarRows([rows_by_id[object_id]] if object_id in rows_by_id else [])

    class FakeUow:
        session = FakeSession()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    events = []

    class FakeStore:
        def __init__(self, session, **_kwargs):
            self._session = session

        async def lock_run(self, run_id):
            # Stands in for RunStore.lock_run, whose observable contract here is
            # a locked read that refreshes the identity map. This parent is its
            # own root, so the real acquirer also takes exactly one lock.
            from sqlalchemy import select

            from brain.platform.db.models.agent_run import AgentRunRow

            rows = await self._session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.id == int(run_id))
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
            return rows.one_or_none()

        async def append_event(self, event):
            events.append(event)
            return SimpleNamespace(id=len(events), sequence_no=len(events), root_run_id=42)

    error = "Could not materialize GitHub repository example-org/missing-repo: credential denied."
    resource_failure = {
        "kind": "repo",
        "repo": "example-org/missing-repo",
        "error": error,
    }
    result = SimpleNamespace(
        ok=materialization_ok,
        workspaces=[{"path": "/tmp/project-root"}] if materialization_ok else [],
        errors=[] if materialization_ok else [error],
        warnings=[error] if materialization_ok else [],
        failed_resources=[] if materialization_ok else [resource_failure],
        degraded_resources=[resource_failure] if materialization_ok else [],
    )
    mark_failed = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(
        "brain.systems.runs.evidence_health.AsyncAgentRunStore",
        FakeStore,
    )
    monkeypatch.setattr(runner, "_async_record_project_activity", AsyncMock())
    monkeypatch.setattr(runner, "materialize_project_context_workspaces", AsyncMock(return_value=result))
    monkeypatch.setattr(runner, "_mark_run_failed_after_runner_error_async", mark_failed)

    context_ready, status_payload = await runner._async_materialize_project_context(child.id)

    assert context_ready is materialization_ok
    assert status_payload is None
    failure = parent.metadata_["evidence_health"]["failures"][0]
    assert parent.metadata_["evidence_health"]["status"] == "degraded"
    assert cycle_run.context_snapshot["evidence_health"]["status"] == "degraded"
    assert cycle_run.context_snapshot["evidence_health"]["expected_checks"] == ["github"]
    assert cycle_run.context_snapshot["evidence_health"]["failures"] == [failure]
    assert failure == {
        "kind": "worker_tool_failure",
        "tool": "spawn_worker",
        "child_run_id": 49,
        "worker_run_id": 49,
        "worker_role": "repo_reader",
        "shard": "example-org/missing-repo",
        "stage": "project_context_materialization",
        "error": error,
    }
    assert lock_order == [("AgentRunRow", 42), ("CycleRun", 12)]
    assert [(event.run_id, event.event_type, event.payload) for event in events] == [
        (42, "run.worker_failed", failure)
    ]
    if materialization_ok:
        mark_failed.assert_not_awaited()
    else:
        mark_failed.assert_awaited_once()
