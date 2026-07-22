"""GitHub source tool definitions."""

from __future__ import annotations


GITHUB_TOOLS = [
    {
        "name": "read_github_source",
        "description": (
            "Read bounded GitHub repository data: metadata, issues, pull requests, exact issue/PR counts, "
            "CI checks, or source files and trees at an explicit git ref. Source grep is a bounded literal "
            "search that returns path:line citations and pagination when more files remain. Use this as a "
            "generic source reader before writing findings into Domains, Cycles, Slack updates, or inbound "
            "projections. This tool is read-only and accepts owner/name, GitHub URL, or git remote repo values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get_repo",
                        "list_issues",
                        "list_pull_requests",
                        "get_pull_request",
                        "pull_request_checks",
                        "get_counts",
                        "get_file",
                        "list_tree",
                        "grep",
                    ],
                    "default": "list_issues",
                    "description": (
                        "Which GitHub source data to read. get_pull_request includes mergeability, CI "
                        "check runs, and combined status; pull_request_checks reads CI for a head SHA; "
                        "get_counts returns exact issue and PR counts for the requested state. get_file, "
                        "list_tree, and grep require ref; grep is literal rather than regular-expression search."
                    ),
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
                "pull_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Pull request number required by get_pull_request.",
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Alias for pull_number when using get_pull_request.",
                },
                "sha": {
                    "type": "string",
                    "description": "Head commit SHA required by pull_request_checks.",
                },
                "ref": {
                    "type": "string",
                    "maxLength": 512,
                    "description": (
                        "Explicit commit SHA, branch, or tag for get_file, list_tree, and grep. The response "
                        "returns resolved_ref; use that commit SHA for pinned follow-up reads."
                    ),
                },
                "path": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": (
                        "Repository-relative file path required by get_file, or optional directory prefix "
                        "for list_tree and grep."
                    ),
                },
                "query": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "Non-empty literal text required by grep.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether grep should match case exactly.",
                },
                "line_start": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "First one-based line returned by get_file.",
                },
                "line_end": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional inclusive last line returned by get_file; each read is capped.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 30,
                    "description": "Maximum items to return, 1-100; grep applies a stricter cap of 50 matches.",
                },
                "cursor": {
                    "type": "string",
                    "description": (
                        "Opaque next_page token returned by an issue, pull-request, tree, or grep listing."
                    ),
                },
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
            "owner/name, GitHub URL, or git remote repo values. Customer-reported bug filing is a "
            "two-part contract: create the GitHub issue in the owning repo as the durable artifact, "
            "then create a linked mirror in an existing workspace tracker. Never create a Domain while "
            "filing. Preserve the customer quote, concrete impact, and source origin_ref in the issue "
            "body. If this tool fails, the final reply must name the GitHub issue and the exact blocker; "
            "a tracker record is retention/handoff, never a silent substitute. For a chantier parent mirror, use "
            "the title '[Chantier] <title>' and a body that states the goal as 'Done means …', the "
            "chantier slug, and key references. Do not add a hand-maintained child checklist: "
            "native GitHub sub-issues are the progress surface."
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
                        "clear AI-generated disclaimer. For a customer-reported bug, include the "
                        "customer quote, concrete impact (including credit loss), and Slack origin_ref. "
                        "A chantier parent body carries its "
                        "'Done means …' goal, chantier slug, and key refs, without duplicating "
                        "native sub-issues as a Markdown checklist."
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
                    "description": (
                        "Optional GitHub login handles to assign. Honor an explicit assignment request "
                        "using the person's verified GitHub identity."
                    ),
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token for private repos.",
                },
            },
            "required": ["repo", "title"],
        },
    },
    {
        "name": "update_github_issue",
        "description": (
            "Update an EXISTING real GitHub issue via the GitHub API. This can transfer ownership, "
            "change labels, open or close the issue, and edit its title or body. Each requested "
            "field is applied independently, followed by an exact issue read-back. The result "
            "reports applied and failed fields separately, so a partial update is never presented "
            "as total success. This uses the same project-bound GitHub App write identity as issue "
            "creation; the resulting GitHub issues webhook keeps configured mirrored ticket records "
            "in sync. labels_set replaces every label and is mutually exclusive with labels_add and "
            "labels_remove."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Target repository as owner/name, GitHub URL, or git remote URL.",
                },
                "issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of the existing issue to update.",
                },
                "assignees_add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitHub login handles to add as assignees.",
                },
                "assignees_remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitHub login handles to remove as assignees.",
                },
                "labels_add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Existing repository label names to add.",
                },
                "labels_remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names to remove.",
                },
                "labels_set": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Replace all issue labels with this exact list; [] clears every label. "
                        "Do not combine with labels_add or labels_remove."
                    ),
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed"],
                    "description": "New issue state.",
                },
                "title": {
                    "type": "string",
                    "description": "Replacement issue title. Must be non-empty when provided.",
                },
                "body": {
                    "type": "string",
                    "description": "Replacement Markdown body; an empty string clears the body.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token.",
                },
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "add_github_issue_comment",
        "description": (
            "Append a Markdown comment to an EXISTING real GitHub issue via the GitHub API. This is a "
            "separate append-only write: it does not edit the issue title or body and cannot be used to "
            "close or otherwise update the issue. Use update_github_issue for issue fields, then call this "
            "tool separately when an audit note, resolution, or other timeline comment is required. The "
            "tool uses the same project-bound GitHub App write identity and auth fallback policy as other "
            "GitHub issue writes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Target repository as owner/name, GitHub URL, or git remote URL.",
                },
                "issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of the existing issue to comment on.",
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Non-empty Markdown comment body.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token.",
                },
            },
            "required": ["repo", "issue_number", "body"],
        },
    },
    {
        "name": "add_github_sub_issue",
        "description": (
            "Link a REAL GitHub issue as a native sub-issue of a chantier parent issue. This is an "
            "external GitHub write. Parent and child may be in different repositories owned by the "
            "same organization. The tool resolves the child's issue number to GitHub's numeric issue "
            "id, requests a GitHub App token scoped to both repositories, and is idempotent when the "
            "child is already linked. The result verifies the relationship against the parent's "
            "authoritative native sub-issue list. Use that parent-side rollup as the progress surface; "
            "do not maintain a duplicate checklist in the parent body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_repo": {
                    "type": "string",
                    "description": "Parent repository as owner/name, GitHub URL, or git remote URL.",
                },
                "parent_issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Chantier parent issue number.",
                },
                "child_repo": {
                    "type": "string",
                    "description": "Child repository as owner/name, GitHub URL, or git remote URL.",
                },
                "child_issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Member ticket issue number; the tool resolves its numeric GitHub id.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token.",
                },
            },
            "required": [
                "parent_repo",
                "parent_issue_number",
                "child_repo",
                "child_issue_number",
            ],
        },
    },
    {
        "name": "remove_github_sub_issue",
        "description": (
            "Remove a REAL GitHub issue from a chantier parent's native sub-issues. This is an "
            "external GitHub write, supports same-organization cross-repository children, resolves "
            "the child's issue number to its numeric GitHub id, and is idempotent when the child is "
            "already unlinked. It does not close or otherwise edit either issue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_repo": {
                    "type": "string",
                    "description": "Parent repository as owner/name, GitHub URL, or git remote URL.",
                },
                "parent_issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Chantier parent issue number.",
                },
                "child_repo": {
                    "type": "string",
                    "description": "Child repository as owner/name, GitHub URL, or git remote URL.",
                },
                "child_issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Member ticket issue number; the tool resolves its numeric GitHub id.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key holding a write-capable GitHub token.",
                },
            },
            "required": [
                "parent_repo",
                "parent_issue_number",
                "child_repo",
                "child_issue_number",
            ],
        },
    },
    {
        "name": "list_github_sub_issues",
        "description": (
            "Read native GitHub sub-issue relationships. Use action='list' with a parent issue to "
            "inspect its bounded, authoritative sub-issue rollup or verify a newly linked child. Use "
            "action='get_parent' with a member ticket to resolve its parent by the child's global "
            "GitHub node id, including a parent in another repository. For cross-repository reads, "
            "set counterpart_repo to the child repo for action='list' or the expected parent repo "
            "for action='get_parent'; Illo then mints one GitHub App installation token scoped to "
            "both repositories. This tool is read-only and accepts owner/name, GitHub URL, or git "
            "remote repo values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get_parent"],
                    "default": "list",
                    "description": "List a parent's children or look up one child issue's parent.",
                },
                "repo": {
                    "type": "string",
                    "description": "Parent repo for list; child repo for get_parent.",
                },
                "issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Parent issue number for list; child issue number for get_parent.",
                },
                "counterpart_repo": {
                    "type": "string",
                    "description": (
                        "Child repo for action='list'; expected parent repo for action='get_parent'. "
                        "Mints one GitHub App installation token scoped to both repositories."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 30,
                    "description": "Maximum sub-issues to return for list, 1-100.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_page token returned by a sub-issue listing.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key containing a GitHub token for private repos.",
                },
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "check_fix_deploy_state",
        "description": (
            "Read GitHub commit ancestry to check whether a fix PR or commit is merged, on staging, "
            "or deployed to main. Returns an indeterminate result when GitHub cannot be reached or "
            "no available read token can see the repository; it never guesses closed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository as owner/name, GitHub URL, or git remote URL.",
                },
                "pr_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Fix pull request number. Provide this or sha, not both.",
                },
                "sha": {
                    "type": "string",
                    "description": "Fix merge commit SHA. Provide this or pr_number, not both.",
                },
                "token_secret_key": {
                    "type": "string",
                    "description": "Optional Vault secret key containing a GitHub read token.",
                },
            },
            "required": ["repo"],
        },
    },
]


__all__ = ["GITHUB_TOOLS"]
