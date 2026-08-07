from __future__ import annotations

import pytest

from brain.contracts.github import parse_github_repo_slug
from brain.systems.knowledge.connectors.github import _resource_repositories


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/tree/main", "owner/repo"),
        ("http://github.com/owner/repo/issues/1", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("github://owner/repo/tree/main", "owner/repo"),
        ("github.com/owner/repo", "owner/repo"),
        ("owner/repo", "owner/repo"),
    ],
)
def test_parse_github_repo_slug_accepts_github_references(value: str, expected: str):
    assert parse_github_repo_slug(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "/static/uploads/whatever.pdf",
        "static/uploads/whatever.pdf",
        "https://gitlab.com/owner/repo",
        "https://example.com/a/b",
        "file:///a/b",
    ],
)
def test_parse_github_repo_slug_rejects_non_github_references(value: str):
    assert parse_github_repo_slug(value) is None


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
