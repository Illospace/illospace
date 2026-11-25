"""Static CI guardrail: lock down every chat_postMessage call site in illo/."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Exact allowlist of modules permitted to call chat_postMessage.
# Adding a new proactive Slack sender without going through Chief of Staff
# (illo/board/escalate.py) must fail CI here.
ALLOWED_SLACK_SENDERS = {
    # Single unified Chief-of-Staff escalation outbox
    "illo/board/escalate.py",
    # Conversational Slack replies & low-level Slack primitive
    "illo/channels/slack/adapter.py",
    "illo/channels/slack/intake.py",
    "illo/channels/slack/limits.py",
    "illo/connectors/chat.py",
    "illo/slackapi.py",
    # Explicit user auth / sign-in confirmation flows
    "illo/channels/slack/agent_oauth.py",
    "illo/connectors/chatgpt.py",
    "illo/tasks/pages/signin.py",
    # Scheduled routines & owner health watchdog
    "illo/routines/scheduler.py",
    "illo/routines/watchdog.py",
    # Explicit user tools, approval drafts, button acks, memory/notes, and person-to-person relay
    "illo/tools/slack.py",
    "illo/asks/drafts.py",
    "illo/asks/actions.py",
    "illo/asks/relay.py",
    "illo/memory/buttons.py",
    "illo/notes/__init__.py",
    # Gated legacy fallback senders (suppressed whenever board coordinator is active)
    "illo/asks/service.py",
    "illo/channels/slack/tasks.py",
    "illo/deep/findings.py",
    "illo/routines/reminders.py",
}


def _chat_post_message_files() -> set[str]:
    found = set()
    for path in (ROOT / "illo").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chat_postMessage"
            ):
                found.add(path.relative_to(ROOT).as_posix())
                break
    return found


def test_chat_post_message_call_sites_match_allowlist():
    actual = _chat_post_message_files()
    assert actual == ALLOWED_SLACK_SENDERS, (
        f"Unexpected chat_postMessage senders: added={sorted(actual - ALLOWED_SLACK_SENDERS)}, "
        f"removed={sorted(ALLOWED_SLACK_SENDERS - actual)}"
    )


def test_agent_doors_never_post_directly_to_slack():
    actual = _chat_post_message_files()
    assert not any(path.startswith("illo/channels/mcp/") for path in actual)
    assert "illo/board/doors.py" not in actual
    assert "illo/connectors/github_tasks.py" not in actual
