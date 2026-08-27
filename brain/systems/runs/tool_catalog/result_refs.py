"""Tool-boundary explicit refs for result payloads with connector-specific shapes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from brain.contracts.github import _github_artifact_ref_id, _github_comment_ref_id


_GITHUB_ISSUE_RESULT_TOOLS = frozenset(
    {
        "create_github_issue",
        "update_github_issue",
    }
)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _github_issue_ref(repo: str, issue: Any) -> dict[str, str] | None:
    if not isinstance(issue, Mapping):
        return None
    number = _positive_int(issue.get("number"))
    artifact_type = str(issue.get("type") or "issue").strip() or "issue"
    if number is None or artifact_type not in {"issue", "pull_request"}:
        return None
    kind = "github_pull_request" if artifact_type == "pull_request" else "github_issue"
    return {"kind": kind, "id": _github_artifact_ref_id(repo, number)}


def _github_pull_request_ref(repo: str, payload: Mapping[str, Any]) -> dict[str, str] | None:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, Mapping):
        number = _positive_int(pull_request.get("number"))
        artifact_type = str(pull_request.get("type") or "pull_request").strip() or "pull_request"
        if artifact_type not in {"issue", "pull_request"}:
            return None
        kind = "github_issue" if artifact_type == "issue" else "github_pull_request"
    else:
        number = _positive_int(payload.get("number"))
        kind = "github_pull_request"
    if number is None:
        return None
    return {
        "kind": kind,
        "id": _github_artifact_ref_id(repo, number),
    }


def _github_comment_ref(repo: str, payload: Mapping[str, Any]) -> dict[str, str] | None:
    comment = payload.get("comment")
    issue_number = _positive_int(payload.get("issue_number"))
    comment_id = _positive_int(comment.get("id")) if isinstance(comment, Mapping) else None
    if issue_number is None or comment_id is None:
        return None
    return {
        "kind": "github_issue_comment",
        "id": _github_comment_ref_id(repo, issue_number, comment_id),
    }


def emit_explicit_tool_result_refs(tool_name: str, payload: Any) -> Any:
    """Add refs at a known tool result boundary and return ``payload``."""

    if not isinstance(payload, dict) or "mutated_target_refs" in payload:
        return payload
    repo = str(payload.get("repo") or "").strip()
    if repo.count("/") != 1:
        return payload

    ref: dict[str, str] | None = None
    if tool_name in _GITHUB_ISSUE_RESULT_TOOLS:
        ref = _github_issue_ref(repo, payload.get("issue"))
    elif tool_name == "create_github_pull_request":
        ref = _github_pull_request_ref(repo, payload)
    elif tool_name == "add_github_issue_comment":
        ref = _github_comment_ref(repo, payload)
    if ref is None:
        return payload

    # Explicit refs take the same pre-order position as the former walker
    # extraction, preserving cap and dedup order for legacy tool results.
    original = dict(payload)
    payload.clear()
    payload["mutated_target_refs"] = [ref]
    payload.update(original)
    return payload


__all__ = ["emit_explicit_tool_result_refs"]
