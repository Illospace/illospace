"""GitHub source tool definitions."""

from __future__ import annotations


GITHUB_TOOLS = [
    {
        "name": "read_github_source",
        "description": (
            "Read bounded GitHub repository source data such as repo metadata, issues, and pull requests. "
            "Use this as a generic source reader before writing findings into Domains, Cycles, Slack updates, "
            "or inbound projections. This tool is read-only and accepts owner/name, GitHub URL, or git remote "
            "repo values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_repo", "list_issues", "list_pull_requests"],
                    "default": "list_issues",
                    "description": "Which GitHub source data to read.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository as owner/name, GitHub URL, or git remote URL.",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "default": "open",
                    "description": "Issue or pull request state.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional labels filter for issues.",
                },
                "assignee": {"type": "string", "description": "Optional GitHub issue assignee login."},
                "creator": {"type": "string", "description": "Optional GitHub issue creator login."},
                "mentioned": {"type": "string", "description": "Optional GitHub issue mentioned-user login."},
                "since": {"type": "string", "description": "Optional ISO timestamp lower bound for updated issues."},
                "include_pull_requests": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether list_issues should include GitHub's pull-request-as-issue rows.",
                },
                "head": {"type": "string", "description": "Optional pull request head filter."},
                "base": {"type": "string", "description": "Optional pull request base branch filter."},
                "limit": {"type": "integer", "default": 30, "description": "Maximum items to return, 1-100."},
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key containing a GitHub token for private repos.",
                },
            },
            "required": ["repo"],
        },
    }
]


__all__ = ["GITHUB_TOOLS"]
