from types import SimpleNamespace


def test_materialize_github_project_context_uses_vault_key_without_persisting_token(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    clone_calls = []

    def fake_get_secret(key_name, **kwargs):
        assert key_name == "GITHUB_EXAMPLE_TOKEN"
        assert kwargs["user_id"] == "user-1"
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

    result = materialize_project_context_workspaces(
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


def test_materialize_github_project_context_fails_closed_when_clone_unavailable(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        materializer,
        "_clone_github_repo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Repository not found")),
    )

    result = materialize_project_context_workspaces(43, workspace_root=str(tmp_path), user_id="user-1")

    assert not result.ok
    assert "Could not materialize GitHub repository example-org/private" in result.errors[0]
    assert run.target_status == "invalid"
    assert run.target_metadata["project_context_snapshot"]["status"] == "invalid"
    assert run.metadata_["project_context_materialization"]["status"] == "failed"



def test_materialize_agent_run_workspace_ref_project_context_updates_workspace_root(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_clone(slug, destination, *, token, branch):
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch, "commit": "abc123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = materialize_project_context_workspaces(44, workspace_root=str(tmp_path), user_id="user-1")

    assert result.ok
    assert result.workspaces
    assert run.workspace_ref["workspace_root"] == result.workspaces[0]["path"]
    assert run.workspace_ref["resolved_workspace_root"] == result.workspaces[0]["path"]
    assert run.workspace_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]
    assert run.target_ref["project_context_snapshot"]["resources"][0]["path"] == result.workspaces[0]["path"]


def test_materialize_reuses_existing_thread_checkout_without_reclone(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
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

    result = materialize_project_context_workspaces(45, workspace_root=str(tmp_path), user_id="user-1")

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


def test_materialize_ignores_stale_managed_run_path_and_uses_thread_root(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(slug, destination, *, token, branch):
        clone_calls.append(destination)
        destination.mkdir(parents=True)
        return {"path": str(destination), "branch": branch or "main", "commit": "new123"}

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "list_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = materialize_project_context_workspaces(46, workspace_root=str(thread_root), user_id="user-1")

    assert result.ok
    assert clone_calls == [expected_checkout]
    assert result.workspaces == [{"name": "example-org/example-repo", "path": str(expected_checkout)}]
    resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert resource["path"] == str(expected_checkout)


def test_materialize_refuses_to_overwrite_non_matching_thread_checkout(tmp_path, monkeypatch):
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
        def get(self, _model, _id):
            return run

    class FakeUow:
        session = FakeSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    clone_calls = []

    def fake_clone(*args, **kwargs):
        clone_calls.append((args, kwargs))
        raise AssertionError("non-matching checkout should not be overwritten")

    monkeypatch.setattr(materializer, "UnitOfWork", lambda: FakeUow())
    monkeypatch.setattr(materializer, "_git_output", lambda *_args: None)
    monkeypatch.setattr(materializer, "_clone_github_repo", fake_clone)

    result = materialize_project_context_workspaces(47, workspace_root=str(tmp_path), user_id="user-1")

    assert not result.ok
    assert clone_calls == []
    assert (checkout / "local-change.txt").read_text() == "do not delete"
    assert "refusing to overwrite live workspace state" in result.errors[0]
    assert run.target_status == "invalid"


def test_project_context_root_is_scoped_by_thread(monkeypatch, tmp_path):
    from brain.systems.runs.cortex.runner import _project_context_root

    monkeypatch.setenv("ILLO_PROJECT_CONTEXT_WORKSPACE_ROOT", str(tmp_path))

    assert _project_context_root(101, thread_id="idea/with spaces") == str(tmp_path / "ideas" / "idea-with-spaces")
    assert _project_context_root(101, thread_id=None) == str(tmp_path / "run-101")
