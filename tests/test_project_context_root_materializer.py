from pathlib import Path
from types import SimpleNamespace


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


async def test_materialize_saved_project_does_not_adopt_display_name_root(tmp_path, monkeypatch):
    from brain.systems.cortex.project_context import materializer
    from brain.systems.cortex.project_context.materializer import materialize_project_context_workspaces

    profile_id = "profile-stable"
    name_root = tmp_path / "project-roots" / "test-empty-project"
    name_root.mkdir(parents=True)
    (name_root / "unified_payments.csv").write_text("wrong root", encoding="utf-8")

    run = SimpleNamespace(
        id=76,
        user_id="user-1",
        org_id="org-1",
        metadata_={},
        target_ref={
            "kind": "cortex_idea",
            "project_context_snapshot": {
                "status": "validated",
                "name": "test empty project",
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
        76,
        workspace_root=str(tmp_path / "ideas" / "thread-no-name-adopt"),
        user_id="user-1",
    )

    canonical_root = tmp_path / "project-roots" / profile_id
    root_resource = run.target_ref["project_context_snapshot"]["resources"][0]
    assert result.ok
    assert result.empty_project is True
    assert not (canonical_root / "unified_payments.csv").exists()
    assert "adopted_from_root" not in root_resource["materialization"]


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
