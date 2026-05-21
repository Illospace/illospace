from brain.systems.cortex.project_context.workspace_manifest import (
    ProjectWorkspaceManifest,
    ThreadDraftIdentity,
    build_project_workspace_manifest_contract,
    normalize_project_workspace_manifest,
    resolve_project_mount_path,
)


def test_workspace_manifest_disambiguates_duplicate_mount_paths():
    manifest = ProjectWorkspaceManifest.from_project_context({
        "id": "profile-a",
        "resources": [
            {
                "id": "first-report-pack",
                "kind": "folder",
                "mount_path": "/reports",
                "path": "/tmp/materialized/reports-a",
                "materialization": {"workspace_path": "/tmp/materialized/reports-a"},
            },
            {
                "id": "second-report-pack",
                "kind": "folder",
                "mount_path": "reports/",
                "path": "/tmp/materialized/reports-b",
                "materialization": {"workspace_path": "/tmp/materialized/reports-b"},
            },
        ],
    })

    assert [mount.mount_path for mount in manifest.mounts] == ["/reports", "/reports-2"]
    assert [mount.original_mount_path for mount in manifest.mounts] == ["/reports", "/reports"]
    assert manifest.allowed_workspaces == [
        {"name": "/reports", "path": "/tmp/materialized/reports-a"},
        {"name": "/reports-2", "path": "/tmp/materialized/reports-b"},
    ]
    assert manifest.resolve_agent_path("/reports/summary.md") == "/tmp/materialized/reports-a/summary.md"
    assert manifest.resolve_agent_path("/reports-2/summary.md") == "/tmp/materialized/reports-b/summary.md"
    assert manifest.to_dict()["workspaces"] == [
        {"name": "/reports", "path": "/tmp/materialized/reports-a"},
        {"name": "/reports-2", "path": "/tmp/materialized/reports-b"},
    ]


def test_durable_project_workspace_manifest_contract_disambiguates_mounts_without_materialization():
    contract = build_project_workspace_manifest_contract({
        "id": "profile-a",
        "resources": [
            {
                "id": "first-report-pack",
                "kind": "folder",
                "mount_path": "/reports",
                "path": "/Users/example/reports-a",
            },
            {
                "id": "second-report-pack",
                "kind": "folder",
                "mount_path": "reports/",
                "path": "/Users/example/reports-b",
            },
        ],
    })

    assert contract["project_key"] == "profile-a"
    assert [mount["resource_id"] for mount in contract["mounts"]] == ["first-report-pack", "second-report-pack"]
    assert [mount["mount_path"] for mount in contract["mounts"]] == ["/reports", "/reports-2"]
    assert [mount["original_mount_path"] for mount in contract["mounts"]] == ["/reports", "/reports"]


def test_thread_draft_identity_is_scoped_by_project():
    resource = {
        "id": "resource-folder",
        "kind": "folder",
        "mount_path": "/reports",
        "path": "/Users/example/projects/reports",
    }

    first = ThreadDraftIdentity.from_project_resource(
        resource,
        thread_workspace_root="/tmp/thread-root",
        project_context={"id": "project-alpha"},
    )
    second = ThreadDraftIdentity.from_project_resource(
        resource,
        thread_workspace_root="/tmp/thread-root",
        project_context={"id": "project-beta"},
    )
    unscoped = ThreadDraftIdentity.from_project_resource(
        resource,
        thread_workspace_root="/tmp/thread-root",
    )

    assert first.project_key == "project-alpha"
    assert second.project_key == "project-beta"
    assert first.draft_workspace_path == "/tmp/thread-root/.illo-project-context/local/project-alpha/resource-folder"
    assert second.draft_workspace_path == "/tmp/thread-root/.illo-project-context/local/project-beta/resource-folder"
    assert unscoped.draft_workspace_path == "/tmp/thread-root/.illo-project-context/local/resource-folder"


def test_mount_path_is_the_agent_facing_truth():
    manifest = normalize_project_workspace_manifest({
        "id": "profile-a",
        "resources": [
            {
                "id": "resource-folder",
                "kind": "folder",
                "mount_path": "/reports",
                "name": "Uploaded reports",
                "path": "/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder",
                "materialization": {
                    "workspace_path": "/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder",
                    "source_path": "/Users/example/projects/reports",
                    "draft": True,
                },
            }
        ],
    })

    mount = manifest.mounts[0]

    assert mount.mount_path == "/reports"
    assert mount.name == "/reports"
    assert mount.workspace_path == "/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder"
    assert manifest.allowed_workspaces == [
        {"name": "/reports", "path": "/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder"}
    ]
    assert manifest.resolve_agent_path("/reports/brief.md") == (
        "/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder/brief.md"
    )
    assert manifest.resolve_agent_path("/tmp/thread-root/.illo-project-context/local/profile-a/resource-folder/brief.md") is None


def test_resolve_project_mount_path_uses_longest_agent_mount_prefix():
    context = {
        "resources": [
            {
                "id": "repo",
                "kind": "repo",
                "mount_path": "/repo",
                "path": "/tmp/materialized/repo",
            },
            {
                "id": "repo-docs",
                "kind": "folder",
                "mount_path": "/repo/docs",
                "path": "/tmp/materialized/docs",
            },
        ],
    }

    assert resolve_project_mount_path(context, "/repo/README.md") == "/tmp/materialized/repo/README.md"
    assert resolve_project_mount_path(context, "/repo/docs/guide.md") == "/tmp/materialized/docs/guide.md"
