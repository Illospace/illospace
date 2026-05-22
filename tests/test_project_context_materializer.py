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
