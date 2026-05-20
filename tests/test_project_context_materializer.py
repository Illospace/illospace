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


def test_runner_skips_materialization_for_thread_attachment_files(monkeypatch):
    from brain.systems.runs.cortex import runner

    run = SimpleNamespace(
        id=51,
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

    async def fail_materialize(*_args, **_kwargs):
        raise AssertionError("file-only attachments should not be materialized as workspaces")

    monkeypatch.setattr(runner, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(runner, "materialize_project_context_workspaces", fail_materialize)

    context_ready, status_payload = runner._materialize_project_context(51)

    assert context_ready is True
    assert status_payload is None


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
    assert result.workspaces == [
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
    resource = run.target_metadata["project_context_snapshot"]["resources"][0]
    assert resource["path"] == result.workspaces[0]["path"]
    assert resource["materialization"]["status"] == "ready"
    assert run.metadata_["workspaces"] == result.workspaces
    assert "test-private-token" not in str(run.metadata_)
    assert "test-private-token" not in str(run.target_metadata)


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
    assert result.workspaces == [{"name": "example-org/example-repo", "path": str(checkout)}]
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
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
    assert result.workspaces == [{"name": "example-org/example-repo", "path": str(expected_checkout)}]
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
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


async def test_materialize_empty_project_context_is_not_ready(tmp_path, monkeypatch):
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

    assert not result.ok
    assert result.workspaces == []
    assert result.errors == ["Project Context has no resources to materialize."]
    assert "workspace_root" not in run.workspace_ref


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
            errors=["Project Context has no resources to materialize."],
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
