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
    },
    {
        "name": "create_github_issue",
        "description": (
            "Open a REAL GitHub issue in a repository via the GitHub API. This performs a public "
            "write to the target repo — it is NOT an internal tracker record and has real-world "
            "effects. Use only when the target repo and the incident are both clear and a "
            "write-capable token can reach the repo. If no write-capable token can reach a "
            "(private) repo, this returns an error carrying no_write_token so the triage flow can "
            "ask for clarification or fall back to an internal tracker record + handoff. Accepts "
            "owner/name, GitHub URL, or git remote repo values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Target repository as owner/name, GitHub URL, or git remote URL.",
                },
                "title": {
                    "type": "string",
                    "description": "Issue title. Required.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Optional Markdown issue body. Prefix AI-authored triage issues with a "
                        "clear AI-generated disclaimer."
                    ),
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional label names to apply. Labels must already exist in the repo.",
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional GitHub login handles to assign.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token for private repos.",
                },
            },
            "required": ["repo", "title"],
        },
    },
]


__all__ = ["GITHUB_TOOLS"]
