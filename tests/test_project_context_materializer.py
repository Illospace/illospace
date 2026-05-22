from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def test_project_root_key_uses_top_level_context_id():
    from brain.systems.cortex.project_context.project_root import project_key_from_context

    assert project_key_from_context({"id": "context-1"}, fallback="run-73") == "context-1"


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

    def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_EXAMPLE_TOKEN"
        assert kwargs["actor_user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["accessed_by"] == "api"
        return "test-private-token"

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append({"slug": slug, "destination": destination, "token": token, "branch": branch})
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch, "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "get_secret", fake_get_secret)
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

    def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_TOKEN"
        assert kwargs["actor_user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["accessed_by"] == "api"
        return "general-github-token"

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append({"slug": slug, "token": token, "branch": branch})
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", fake_list_secrets)
    monkeypatch.setattr(materializer, "get_secret", fake_get_secret)
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
    assert result.workspaces
    assert run.workspace_ref["workspace_root"] == result.workspaces[0]["path"]
    assert run.workspace_ref["resolved_workspace_root"] == result.workspaces[0]["path"]
    assert run.workspace_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]
    assert run.target_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]


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


async def test_materialize_empty_project_context_creates_project_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=48,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [],
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(48, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ok
    draft_dir = tmp_path / ".illo-project-context" / "local" / "run-48" / "project-root"
    source_root = tmp_path.parent / "project-roots" / "run-48"
    assert result.empty_project is True
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert result.errors == []
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert resource["id"] == "project-root"
    assert resource["mount_path"] == "/"
    assert resource["materialization"]["source_path"] == str(source_root)
    assert run.workspace_ref["project_workspace_manifest"]["mounts"][0]["mount_path"] == "/"
    assert run.workspace_ref["project_workspace_manifest"]["workspaces"] == [{"name": "/", "path": str(draft_dir)}]
    assert run.workspace_ref["project_context_materialization"]["status"] == "materialized"
    assert run.workspace_ref["project_context_materialization"]["empty_project"] is True
    runtime = run.workspace_ref["project_runtime_context"]
    assert runtime["project_context_snapshot"]["resources"][0]["id"] == "project-root"
    assert runtime["project_workspace_manifest"]["mounts"][0]["mount_path"] == "/"
    assert runtime["project_context_materialization"]["status"] == "materialized"
    assert run.workspace_ref["workspace_root"] == str(draft_dir)


async def test_materialize_missing_project_context_snapshot_reports_error(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    run = SimpleNamespace(
        id=50,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={"kind": "cortex_idea"},
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(50, workspace_root=str(tmp_path), user_id="user-1")

    assert not result.ok
    assert result.errors == ["Project Context snapshot is missing."]


async def test_materialize_backend_readable_folder_becomes_thread_draft_workspace(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    project_dir = tmp_path / "project-root"
    project_dir.mkdir()
    (project_dir / "brief.md").write_text("runtime context")

    run = SimpleNamespace(
        id=51,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "project_id": "profile-abc",
                "status": "validated",
                "resources": [
                    {
                        "id": "resource-folder",
                        "kind": "folder",
                        "mount_path": "/reports",
                        "path": str(project_dir),
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(
        materializer,
        "_clone_github_repo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local folders should not be cloned as GitHub repositories")
        ),
    )

    result = await materialize_project_context_workspaces(51, workspace_root=str(tmp_path / "thread-root"), user_id="user-1")

    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "profile-abc" / "project-root"
    source_root = tmp_path / "project-roots" / "profile-abc"
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert (draft_dir / "brief.md").read_text() == "runtime context"
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert resource["id"] == "project-root"
    assert resource["mount_path"] == "/"
    assert resource["path"] == str(draft_dir)
    assert resource["materialization"]["source_path"] == str(source_root)
    assert (source_root / "brief.md").read_text() == "runtime context"
    assert resource["materialization"]["workspace_path"] == str(draft_dir)
    assert resource["materialization"]["draft"] is True
    assert resource["materialization"]["project_key"] == "profile-abc"
    assert run.workspace_ref["workspace_root"] == str(draft_dir)
    assert run.workspace_ref["project_workspace_manifest"]["workspaces"][0]["name"] == "/"


async def test_materialize_child_resource_id_project_root_does_not_claim_root_identity(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("brief", encoding="utf-8")

    run = SimpleNamespace(
        id=52,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "project-root",
                        "kind": "folder",
                        "mount_path": "/reports",
                        "path": str(source_dir),
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        52,
        workspace_root=str(tmp_path / "thread-root"),
        user_id="user-1",
    )

    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-52" / "project-root"
    source_root = tmp_path / "project-roots" / "run-52"
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert (source_root / "brief.md").read_text(encoding="utf-8") == "brief"
    assert (draft_dir / "brief.md").read_text(encoding="utf-8") == "brief"
    root_resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert root_resource["id"] == "project-root"
    assert root_resource["kind"] == "project_root"
    assert root_resource["materialization"]["project_key"] == "run-52"


async def test_materialize_root_mounted_local_resource_is_not_skipped(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    source_dir = tmp_path / "selected-root"
    source_dir.mkdir()
    (source_dir / "README.md").write_text("local root", encoding="utf-8")

    run = SimpleNamespace(
        id=55,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "selected-root",
                        "kind": "folder",
                        "mount_path": "/",
                        "path": str(source_dir),
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        55,
        workspace_root=str(tmp_path / "thread-root"),
        user_id="user-1",
    )

    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-55" / "project-root"
    source_root = tmp_path / "project-roots" / "run-55"
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert (source_root / "README.md").read_text(encoding="utf-8") == "local root"
    assert (draft_dir / "README.md").read_text(encoding="utf-8") == "local root"
    assert run.workspace_ref["project_workspace_manifest"]["mounts"][0]["mount_path"] == "/"


async def test_materialize_single_uploaded_file_uses_project_native_folder_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    spec_file = attachment_dir / "spec.md"
    spec_file.write_text("# Spec")

    run = SimpleNamespace(
        id=53,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "attachment-1",
                        "kind": "file",
                        "name": "spec.md",
                        "path": str(spec_file),
                        "source": "thread_attachment",
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(53, workspace_root=str(tmp_path / "thread-root"), user_id="user-1")

    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-53" / "project-root"
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert (draft_dir / "spec.md").read_text() == "# Spec"
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    source_root = Path(resource["materialization"]["source_path"])
    assert source_root != spec_file
    assert source_root.is_dir()
    assert (source_root / "spec.md").read_text() == "# Spec"
    assert resource["id"] == "project-root"
    assert resource["mount_path"] == "/"
    assert resource["path"] == str(draft_dir)
    assert resource["materialization"]["path"] == str(draft_dir)
    assert resource["materialization"]["kind"] == "project_root"
    assert resource["materialization"]["workspace_path"] == str(draft_dir)
    assert resource["materialization"]["draft"] is True
    assert run.workspace_ref["workspace_root"] == str(draft_dir)
    mount = run.workspace_ref["project_workspace_manifest"]["mounts"][0]
    assert mount["kind"] == "project_root"
    assert mount["source_path"] == str(source_root)
    assert mount["resource_path"] == str(draft_dir)


async def test_materialize_uploaded_folder_imports_files_into_project_native_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    upload_dir = tmp_path / "uploads"
    (upload_dir / "folder").mkdir(parents=True)
    first_file = upload_dir / "folder" / "brief.md"
    second_file = upload_dir / "folder" / "data" / "metrics.csv"
    second_file.parent.mkdir()
    first_file.write_text("brief", encoding="utf-8")
    second_file.write_text("metric,value\n", encoding="utf-8")

    run = SimpleNamespace(
        id=57,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "folder-upload",
                        "kind": "folder",
                        "name": "folder",
                        "uri": "project-context-upload://folder",
                        "uploaded_files": [
                            {
                                "filename": "brief.md",
                                "relative_path": "folder/brief.md",
                                "storage_path": str(first_file),
                            },
                            {
                                "filename": "metrics.csv",
                                "relative_path": "folder/data/metrics.csv",
                                "storage_path": str(second_file),
                            },
                        ],
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        57,
        workspace_root=str(tmp_path / "thread-root"),
        user_id="user-1",
    )

    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-57" / "project-root"
    source_root = Path(run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["source_path"])
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert (source_root / "folder" / "brief.md").read_text(encoding="utf-8") == "brief"
    assert (source_root / "folder" / "data" / "metrics.csv").read_text(encoding="utf-8") == "metric,value\n"
    assert (draft_dir / "folder" / "brief.md").read_text(encoding="utf-8") == "brief"
    assert (draft_dir / "folder" / "data" / "metrics.csv").read_text(encoding="utf-8") == "metric,value\n"
    imports = run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["imports"]
    assert imports["root_versions"]["before"]["label"] == "before-root-import"
    assert imports["root_versions"]["after"]["label"] == "after-root-import"


async def test_materialize_saved_project_root_identity_survives_resource_changes(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    seed = tmp_path / "seed.md"
    seed.write_text("seed", encoding="utf-8")
    runs: dict[int, SimpleNamespace] = {}

    def run_for(run_id: int, resources: list[dict[str, str]]):
        run = SimpleNamespace(
            id=run_id,
            user_id="user-1",
            org_id="org-1",
            metadata_={},
            target_ref={
                "kind": "cortex_idea",
                "project_context_snapshot": {
                    "project_key": "profile-stable",
                    "project_id": "profile-stable",
                    "slug": "strategy-room",
                    "status": "validated",
                    "resources": resources,
                },
            },
            workspace_ref={},
        )
        runs[run_id] = run
        return run

    first_run = run_for(71, [])
    second_run = run_for(72, [{"id": "seed", "kind": "file", "name": "seed.md", "path": str(seed)}])

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

    first = await materialize_project_context_workspaces(
        71,
        workspace_root=str(tmp_path / "ideas" / "thread-one"),
        user_id="user-1",
    )
    second = await materialize_project_context_workspaces(
        72,
        workspace_root=str(tmp_path / "ideas" / "thread-two"),
        user_id="user-1",
    )

    first_source = Path(first_run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["source_path"])
    second_source = Path(second_run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["source_path"])
    assert first.ok
    assert second.ok
    assert first.empty_project is True
    assert second.empty_project is False
    assert first_source == second_source == tmp_path / "project-roots" / "profile-stable"
    assert (second_source / "seed.md").read_text(encoding="utf-8") == "seed"
    assert first_run.workspace_ref["project_context_materialization"]["empty_project"] is True
    assert second_run.workspace_ref["project_context_materialization"]["empty_project"] is False
    assert second_run.workspace_ref["project_context_materialization"]["seed_resource_count"] == 1
    assert second_run.workspace_ref["project_context_materialization"]["project_root_file_count"] == 1


async def test_materialize_picker_project_context_uses_profile_id_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    profile_id = "fec2d533-e4a0-40e7-9055-b5b619e91ab6"
    run = SimpleNamespace(
        id=73,
        user_id="user-1",
        org_id="org-1",
        metadata_={
            "project_context": {
                "name": "test empty project",
                "project_profile_id": profile_id,
                "selected_profile_id": f"server:{profile_id}",
                "resources": [],
            },
        },
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "name": "test empty project",
                "project_key": "test-empty-project",
                "resources": [],
            },
        },
        workspace_ref={
            "name": "test empty project",
            "project_profile_id": profile_id,
            "selected_profile_id": f"server:{profile_id}",
            "project_context_snapshot": {
                "status": "validated",
                "name": "test empty project",
                "project_key": "test-empty-project",
                "resources": [],
            },
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        73,
        workspace_root=str(tmp_path / "ideas" / "thread-one"),
        user_id="user-1",
    )

    draft_dir = tmp_path / "ideas" / "thread-one" / ".illo-project-context" / "local" / profile_id / "project-root"
    source_root = tmp_path / "project-roots" / profile_id
    snapshot = run.target_ref["project_context_snapshot"]
    assert result.ok
    assert result.workspaces == [{"name": "/", "path": str(draft_dir)}]
    assert snapshot["project_id"] == profile_id
    assert snapshot["project_key"] == profile_id
    assert snapshot["resources"][0]["materialization"]["source_path"] == str(source_root)
    assert snapshot["project_workspace_manifest"]["project_id"] == profile_id
    assert snapshot["project_workspace_manifest"]["project_key"] == profile_id
    assert snapshot["project_workspace_manifest"]["mounts"][0]["mount_path"] == "/"


async def test_materialize_empty_saved_project_reports_non_empty_after_root_publish(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    profile_id = "profile-stable"
    source_root = tmp_path / "project-roots" / profile_id
    source_root.mkdir(parents=True)
    (source_root / "analysis").mkdir()
    (source_root / "analysis" / "summary.md").write_text("published", encoding="utf-8")

    run = SimpleNamespace(
        id=74,
        user_id="user-1",
        org_id="org-1",
        metadata_={
            "project_context": {
                "project_profile_id": profile_id,
                "selected_profile_id": f"server:{profile_id}",
                "name": "Empty profile with published root",
                "resources": [],
            },
        },
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "name": "Empty profile with published root",
                "project_profile_id": profile_id,
                "resources": [],
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        74,
        workspace_root=str(tmp_path / "ideas" / "thread-after-publish"),
        user_id="user-1",
    )

    draft_dir = tmp_path / "ideas" / "thread-after-publish" / ".illo-project-context" / "local" / profile_id / "project-root"
    materialization = run.workspace_ref["project_context_materialization"]
    root_resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert result.ok
    assert result.empty_project is False
    assert materialization["empty_project"] is False
    assert materialization["seed_resource_count"] == 0
    assert materialization["project_root_file_count"] == 1
    assert root_resource["materialization"]["root_empty"] is False
    assert root_resource["materialization"]["root_file_count"] == 1
    assert (draft_dir / "analysis" / "summary.md").read_text(encoding="utf-8") == "published"


async def test_materialize_saved_project_adopts_existing_slug_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    profile_id = "profile-stable"
    slug_root = tmp_path / "project-roots" / "test-empty-project"
    slug_root.mkdir(parents=True)
    (slug_root / "analysis").mkdir()
    (slug_root / "unified_payments.csv").write_text("full csv", encoding="utf-8")
    (slug_root / "analysis" / "summary.md").write_text("published", encoding="utf-8")

    run = SimpleNamespace(
        id=75,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "name": "test empty project",
                "slug": "test-empty-project",
                "project_id": profile_id,
                "project_key": profile_id,
                "resources": [],
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    result = await materialize_project_context_workspaces(
        75,
        workspace_root=str(tmp_path / "ideas" / "thread-adopt"),
        user_id="user-1",
    )

    canonical_root = tmp_path / "project-roots" / profile_id
    draft_dir = tmp_path / "ideas" / "thread-adopt" / ".illo-project-context" / "local" / profile_id / "project-root"
    root_resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert result.ok
    assert result.empty_project is False
    assert (canonical_root / "unified_payments.csv").read_text(encoding="utf-8") == "full csv"
    assert (canonical_root / "analysis" / "summary.md").read_text(encoding="utf-8") == "published"
    assert (draft_dir / "unified_payments.csv").read_text(encoding="utf-8") == "full csv"
    assert root_resource["materialization"]["adopted_from_root"] == str(slug_root)


async def test_materialize_thread_draft_marks_conflict_when_root_and_draft_changed(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    project_dir = tmp_path / "project-root"
    project_dir.mkdir()
    (project_dir / "brief.md").write_text("root v1")

    run = SimpleNamespace(
        id=56,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "resources": [
                    {
                        "id": "resource-folder",
                        "kind": "folder",
                        "mount_path": "/reports",
                        "path": str(project_dir),
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

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())

    first = await materialize_project_context_workspaces(56, workspace_root=str(tmp_path / "thread-root"), user_id="user-1")
    draft_dir = tmp_path / "thread-root" / ".illo-project-context" / "local" / "run-56" / "project-root"
    source_root = Path(run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["source_path"])

    assert first.ok
    (draft_dir / "brief.md").write_text("draft edit")
    (source_root / "brief.md").write_text("root v2")

    second = await materialize_project_context_workspaces(56, workspace_root=str(tmp_path / "thread-root"), user_id="user-1")

    assert second.ok
    assert (draft_dir / "brief.md").read_text() == "draft edit"
    status = run.target_ref["project_context_snapshot"]["resources"][0]["materialization"]["draft_status"]
    assert status["conflicts"] == ["brief.md"]
    assert status["out_of_date"] == ["brief.md"]


def test_project_context_root_is_scoped_by_thread(monkeypatch, tmp_path):
    from brain.systems.runs.cortex.runner import _project_context_root

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert _project_context_root(101, thread_id="idea/with spaces") == str(tmp_path / "ideas" / "idea-with-spaces")
    assert _project_context_root(101, thread_id=None) == str(tmp_path / "run-101")


def test_project_context_root_uses_workspace_root_in_deploy(monkeypatch, tmp_path):
    from brain.systems.runs.cortex.runner import _project_context_root

    monkeypatch.delenv("ILLO_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert _project_context_root(102, thread_id="idea-2") == str(tmp_path / "ideas" / "idea-2")


def test_runner_fails_fast_when_project_context_has_no_workspace(monkeypatch):
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
    monkeypatch.setattr(runner, "_async_record_project_activity", AsyncMock())
    monkeypatch.setattr(
        runner,
        "materialize_project_context_workspaces",
        AsyncMock(return_value=SimpleNamespace(
            ok=False,
            workspaces=[],
            errors=["Could not materialize GitHub repository example-org/missing-repo: repository not found."],
        )),
    )

    async def fake_mark_failed(run_id, error, *, final_answer=None):
        captured.update({"run_id": run_id, "error": error, "final_answer": final_answer})
        return {"idea_id": "idea-3", "new_status": "failed"}

    monkeypatch.setattr(runner, "_mark_run_failed_after_runner_error_async", fake_mark_failed)

    context_ready, status_payload = runner._materialize_project_context(49)

    assert context_ready is False
    assert status_payload == {"idea_id": "idea-3", "new_status": "failed"}
    assert captured["run_id"] == 49
    assert "Project Context unavailable" in captured["error"]
    assert "did not provide a usable workspace" in captured["final_answer"]
