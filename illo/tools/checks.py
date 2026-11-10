"""The three authority checks. Profiles are explicit data, never prompt text."""

from dataclasses import dataclass
from typing import Literal

READ_TOOLS = frozenset(
    {
        "people_list",
        "people_get",
        "lanes_search",
        "lanes_get",
        "lanes_mine",
        "asks_list",
        "asks_get",
        "rules_list",
        "search_all",
        "search_get",
        "notes_search",
        "github_search_issues",
        "github_get_issue",
        "github_get_pull",
        "web_read_public_page",
    }
)
TICKET_TOOLS = frozenset(
    {
        "github_create_issue",
        "github_assign_issue",
        "github_comment_issue",
        "claims_take",
        "work_start",
    }
)
PERSONAL_TOOLS = frozenset(
    {"mail_search", "mail_get", "calendar_list", "drive_search", "drive_get"}
)
INTERNAL_TOOLS = frozenset(
    {
        "asks_create",
        "asks_answer_with_rule",
        "rules_remove_mine",
        "slack_send_dm",
        "slack_post_channel",
        "slack_read_thread",
        "notes_propose",
        "connectors_list",
        "connectors_call",
        "email_draft",
    }
)
GRANT_PROFILES = {
    "person_dm": READ_TOOLS | TICKET_TOOLS | PERSONAL_TOOLS | INTERNAL_TOOLS,
    "team_channel": READ_TOOLS | TICKET_TOOLS | {"asks_create", "slack_read_thread"},
    "tainted": READ_TOOLS - {"lanes_mine"} | TICKET_TOOLS,
    "routine": READ_TOOLS | TICKET_TOOLS | {"asks_create", "slack_send_dm"},
    "agent_request": READ_TOOLS
    | TICKET_TOOLS
    | PERSONAL_TOOLS
    | {"asks_create", "notes_propose", "connectors_list", "connectors_call"},
}


@dataclass(frozen=True)
class Decision:
    outcome: Literal["allow", "draft", "deny"]
    reason: str


def grant_names(run: dict) -> frozenset[str]:
    """Taint always narrows the grant, even if an earlier profile was broader."""
    names = frozenset(GRANT_PROFILES.get("tainted" if run["tainted"] else run["profile"], ()))
    if run["principal"] == "outsider":
        return names & {"web_read_public_page"}
    return names


def scope(run: dict, tool) -> Decision:
    """Check 1: a registered tool must be named by the run grant."""
    if tool.name not in grant_names(run):
        return Decision("deny", "The run grant does not include this tool")
    if tool.personal and (
        run["tainted"]
        or not run["principal"].startswith("person:")
        or run["profile"] not in {"person_dm", "agent_request"}
    ):
        return Decision("deny", "A personal tool needs an untainted run of its owner")
    return Decision("allow", "The tool is in the run grant")


def destination(run: dict, tool, args: dict) -> Decision:
    """Check 2: personal data cannot leave its owner's destination without a draft."""
    if tool.risk == "read":
        return Decision("allow", "The tool does not write")
    target = tool.destination(args)
    if not target or not (
        target == "ledger" or target.startswith(("person:", "channel:", "github:", "outside:"))
    ):
        return Decision("deny", "The tool has no valid destination")
    if run["personal_read"] and target not in {run["principal"], "ledger"}:
        return Decision("draft", "Personal data needs approval for this destination")
    return Decision("allow", "The destination is allowed")


def risk(run: dict, tool) -> Decision:
    """Check 3: outside messages need approval. Destructive tools do not exist."""
    if tool.risk == "send_outside":
        return Decision("draft", "A message to an outsider needs approval")
    if tool.risk not in {"read", "write_internal"}:
        return Decision("deny", "This risk is not allowed")
    return Decision("allow", "The risk is allowed")


def check(run: dict, tool, args: dict) -> Decision:
    """Return the strictest of the three checks."""
    first = scope(run, tool)
    if first.outcome == "deny":
        return first
    second = destination(run, tool, args)
    third = risk(run, tool)
    return next(
        (d for d in (second, third) if d.outcome == "deny"),
        next((d for d in (second, third) if d.outcome == "draft"), third),
    )
