from __future__ import annotations

from brain.systems.knowledge.connectors.github import _resource_repositories


def test_resource_repositories_ignores_uploaded_file_uri():
    context = {"resources": [{"uri": "/static/uploads/x.pdf"}]}

    assert _resource_repositories(context) == []


def test_resource_repositories_keeps_github_urls_in_generic_resource_fields():
    context = {
        "resources": [
            {"uri": "https://github.com/owner/first"},
            {"url": "https://github.com/owner/second/tree/main"},
        ]
    }

    assert _resource_repositories(context) == ["owner/first", "owner/second"]
