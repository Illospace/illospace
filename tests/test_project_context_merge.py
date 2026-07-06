from __future__ import annotations

from brain.systems.cortex.project_context.merge import (
    merge_project_context_resources,
    project_resource_identity,
)


def test_project_resource_identity_canonicalizes_github_repo_shapes():
    assert (
        project_resource_identity({"repo": "uwear-ai/uwear-backend"})
        == project_resource_identity({"uri": "https://github.com/uwear-ai/uwear-backend.git"})
        == project_resource_identity({"remote": "git@github.com:uwear-ai/uwear-backend.git"})
        == "github_repo:uwear-ai/uwear-backend"
    )


def test_merge_project_context_resources_dedupes_equivalent_github_resources():
    merged = merge_project_context_resources(
        {"resources": [{"repo": "uwear-ai/uwear-backend", "label": "Backend"}]},
        {"resources": [{"uri": "https://github.com/uwear-ai/uwear-backend", "label": "Backend repo"}]},
    )

    assert merged is not None
    assert merged["resources"] == [{"repo": "uwear-ai/uwear-backend", "label": "Backend"}]
