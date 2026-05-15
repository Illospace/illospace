from brain.systems.cortex.project_context.permissions import (
    attach_project_provenance,
    derive_project_permission_scope,
    normalize_project_path,
    validate_path_permission,
)


def _snapshot(*, mode: str = "read_write"):
    return {
        "id": "ctx-1",
        "name": "Repo",
        "status": "ready",
        "resources": [
            {
                "id": "repo",
                "path": "/work/illospace",
                "mode": mode,
                "permissions": {
                    "allowed_paths": ["brain", "tests"],
                    "forbidden_paths": ["brain/secrets", "/work/illospace/tests/fixtures/private"],
                },
            }
        ],
    }


def test_project_paths_reject_traversal_in_every_common_shape():
    escaping_paths = [
        "..",
        "../outside.py",
        "../../etc/passwd",
        r"..\outside.py",
        "/../outside.py",
    ]

    for path in escaping_paths:
        assert normalize_project_path(path) is None


def test_project_permission_scope_blocks_sibling_prefix_and_forbidden_children():
    snapshot = _snapshot()

    assert validate_path_permission("/work/illospace/brain/app.py", snapshot)[0] is True
    assert validate_path_permission("/work/illospace/tests/test_app.py", snapshot)[0] is True

    allowed, reason, _scope = validate_path_permission("/work/illospace-other/brain/app.py", snapshot)
    assert allowed is False
    assert reason == "path is outside allowed Project Context resources"

    allowed, reason, _scope = validate_path_permission("/work/illospace/brain/secrets/key.txt", snapshot)
    assert allowed is False
    assert reason == "path is inside forbidden project context path `/work/illospace/brain/secrets`"

    allowed, reason, _scope = validate_path_permission(
        "/work/illospace/tests/fixtures/private/token.txt",
        snapshot,
    )
    assert allowed is False
    assert reason == "path is inside forbidden project context path `/work/illospace/tests/fixtures/private`"


def test_project_permission_scope_enforces_read_only_snapshots_for_writes():
    snapshot = _snapshot(mode="read")

    read_allowed, _read_reason, scope = validate_path_permission(
        "/work/illospace/brain/app.py",
        snapshot,
        operation="read",
    )
    write_allowed, write_reason, _scope = validate_path_permission(
        "/work/illospace/brain/app.py",
        snapshot,
        operation="write",
    )

    assert read_allowed is True
    assert scope["permission_mode"] == "read"
    assert write_allowed is False
    assert write_reason == "project context is read-only"


def test_project_provenance_carries_permission_decision_with_artifacts():
    artifact = {"path": "/work/illospace/brain/secrets/key.txt", "operation": "read"}

    enriched = attach_project_provenance(artifact, _snapshot())

    permission = enriched["provenance"]["project_context"]["path_permission"]
    assert permission["allowed"] is False
    assert permission["reason"] == "path is inside forbidden project context path `/work/illospace/brain/secrets`"
    assert permission["scope"]["resource_ids"] == ["repo"]


def test_project_permission_scope_deduplicates_normalized_roots():
    scope = derive_project_permission_scope(
        {
            "resources": [
                {"id": "a", "path": "/repo", "permissions": {"allowed_paths": ["src", "./src", "src/../tests"]}},
                {"id": "b", "path": "/repo/tests"},
            ]
        }
    )

    assert scope["allowed_paths"] == ["/repo/src", "/repo/tests"]
