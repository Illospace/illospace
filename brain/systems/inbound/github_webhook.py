"""GitHub webhook → inbound envelope translation (pure).

GitHub cannot post the bridge-token/`WebhookEnvelopeCreate` shape the generic
``/webhooks`` endpoint expects, so a dedicated router verifies GitHub's
``X-Hub-Signature-256`` and maps the event into the SAME inbound envelope that
``submit_inbound_envelope`` already consumes. This module is the pure, testable
core of that router: signature verification and event→envelope mapping. The
router itself (connection lookup + the ``submit_inbound_envelope`` call) is thin
wiring on top of these functions.

Once a source policy + domain projection are configured for the GitHub
connection, these envelopes upsert domain records within seconds; until then they
fall through to Illo triage like any unmatched signal. Either way the domain goes
fresh on the event instead of on a twice-daily poll.
"""

from __future__ import annotations

import hashlib
import hmac

# Envelope kind for GitHub deliveries. A configured source policy matches on this
# (and/or origin) to route the event to a domain projection.
GITHUB_ENVELOPE_KIND = "github_event"

# Events we translate. Anything else returns None (ignored) so the router can 204.
_SUPPORTED_EVENTS = ("issues", "pull_request", "issue_comment")


def verify_signature(secret, body, signature_header) -> bool:
    """Constant-time verify of GitHub's ``X-Hub-Signature-256`` over the raw body.

    ``secret`` and ``body`` may be ``str`` or ``bytes``. Returns ``False`` on any
    missing/malformed input rather than raising.
    """
    if not secret or not signature_header:
        return False
    header = str(signature_header).strip()
    if not header.startswith("sha256="):
        return False
    secret_b = secret.encode() if isinstance(secret, str) else secret
    body_b = body.encode() if isinstance(body, str) else body
    expected = "sha256=" + hmac.new(secret_b, body_b, hashlib.sha256).hexdigest()
    # Compare as bytes: the header is attacker-controlled and Starlette decodes
    # header values as latin-1, so a non-ASCII header would make a str-vs-str
    # compare_digest raise TypeError (→ unhandled 500). latin-1 round-trips any
    # decoded header byte; a non-ASCII header simply won't match the ASCII digest.
    return hmac.compare_digest(expected.encode("ascii"), header.encode("latin-1"))


def _subject(event: str, payload: dict) -> tuple[str, dict]:
    """Return ``(noun, subject_dict)`` for the event's primary object."""
    if event == "issues":
        return "issue", payload.get("issue") or {}
    if event == "pull_request":
        return "pull request", payload.get("pull_request") or {}
    if event == "issue_comment":
        # A comment carries its parent issue/PR as `issue`.
        return "comment", payload.get("issue") or {}
    return "event", {}


def github_event_to_envelope(event: str, payload: dict, *, delivery_id=None) -> "dict | None":
    """Map a GitHub webhook (event name + JSON payload) to an inbound envelope.

    Returns ``None`` for unsupported events. The full payload is carried through
    so a configured projection can extract its own fields; ``hints`` surfaces the
    commonly-needed bits (including ``source_updated_at`` for freshness).
    """
    if event not in _SUPPORTED_EVENTS:
        return None
    if not isinstance(payload, dict):  # a signature-valid but non-object body
        payload = {}
    action = payload.get("action") or "event"
    repo = (payload.get("repository") or {}).get("full_name")
    noun, subject = _subject(event, payload)

    # The record anchor is always the issue/PR (node_id + url) so a comment
    # updates its parent's record rather than forking a new one.
    number = subject.get("number")
    title = subject.get("title")
    url = subject.get("html_url")
    state = subject.get("state")
    node_id = subject.get("node_id")
    source_updated_at = subject.get("updated_at")
    author = (subject.get("user") or {}).get("login")
    comment_url = None

    if event == "issue_comment":
        comment = payload.get("comment") or {}
        # Keep node_id/url anchored to the issue; the comment supplies freshness,
        # the acting author, and its own url as a separate hint.
        comment_url = comment.get("html_url")
        author = (comment.get("user") or {}).get("login") or author
        source_updated_at = comment.get("updated_at") or source_updated_at

    merge_hints = {}
    summary_action = action
    if event == "pull_request":
        merged = subject.get("merged") is True
        if merged:
            pr_outcome = "merged"
        elif state == "closed":
            pr_outcome = "closed_unmerged"
        else:
            pr_outcome = "open"
        merge_hints = {
            "merged": merged,
            "pr_outcome": pr_outcome,
            "base_ref": (subject.get("base") or {}).get("ref"),
            "head_ref": (subject.get("head") or {}).get("ref"),
            "merge_commit_sha": subject.get("merge_commit_sha"),
            "merged_at": subject.get("merged_at"),
        }
        if pr_outcome == "merged":
            summary_action = "merged"

    where = f" #{number}" if number else ""
    summary = (
        f"GitHub {noun}{where} {summary_action}: {title}".strip()
        if title
        else f"GitHub {noun}{where} {summary_action}".strip()
    )

    return {
        "origin": f"github:{repo}" if repo else "github",
        "kind": GITHUB_ENVELOPE_KIND,
        "summary": summary[:2000],
        "payload": payload,
        "hints": {
            "provider": "github",
            "event": event,
            "action": action,
            "repo": repo,
            "number": number,
            "url": url,
            "comment_url": comment_url,
            "state": state,
            "node_id": node_id,
            "author": author,
            # Freshness: GitHub's own last-modified for the subject object.
            "source_updated_at": source_updated_at,
            **merge_hints,
        },
        "idempotency_key": (f"github:{delivery_id}"[:160] if delivery_id else None),
    }
