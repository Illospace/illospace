from __future__ import annotations

import pytest

from brain.contracts.github import parse_github_repo_slug


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


def test_parser_is_not_reachable_through_its_old_cortex_home():
    """The move is only real if the old public path stops resolving.

    Cortex imports the parser under a private alias precisely so that
    ``from brain.systems.cortex.project_context.github import parse_github_repo_slug``
    fails. Without this test an unaliased import could silently reintroduce the
    cross-layer path that #725 removed.
    """
    import brain.systems.cortex.project_context.github as cortex_github

    assert not hasattr(cortex_github, "parse_github_repo_slug")
