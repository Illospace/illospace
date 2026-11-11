"""Assemble the system prompt of a run.

The prompt sets the voice and explains the run. It grants no authority:
the tool server checks each call against the grant of the run.
The output is deterministic for the same inputs, so tests can compare snapshots.
"""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from illo.ledger import asks
from illo.workspace import WorkspaceError

HERE = Path(__file__).parent
BASE_PROMPT = (HERE / "base_prompt.md").read_text().strip()
PROFILE_TEXTS = {p.stem: p.read_text().strip() for p in sorted((HERE / "prompts").glob("*.md"))}

MAX_PROMPT_CHARS = 60_000
# Allow for changing dates and the count of open asks after a workspace loads.
RUN_FACTS_RESERVE = 128

CHANNEL_NAMES = {
    "email": "email",
    "mcp": "the MCP endpoint for agents",
    "signals": "the signal intake",
    "routine": "a routine",
}


def prompt_profile(principal: str, profile: str) -> str:
    """An outsider always gets the outsider text, as in the tool grant."""
    return "outsider" if principal == "outsider" else profile


def build(
    workspace,
    principal: str,
    profile: str,
    channel_id: str | None = None,
    *,
    now: datetime | None = None,
    event_channel: str | None = None,
    open_asks: int = 0,
    _fact_reserve: int = 0,
) -> str:
    """Return base text, profile text, run facts, matching rules, and the skill list."""
    key = prompt_profile(principal, profile)
    parts = [BASE_PROMPT, PROFILE_TEXTS.get(key, "")]
    parts.append(_facts(workspace, principal, key, channel_id, now, event_channel, open_asks))
    text = "\n\n".join(parts)
    limit = MAX_PROMPT_CHARS - max(0, _fact_reserve)

    def check_size(source):
        if len(text) > limit:
            raise WorkspaceError(
                f"{source}: the system prompt exceeds {MAX_PROMPT_CHARS:,} characters "
                "with space for run facts"
            )

    check_size(f"{workspace.root / 'illo.yaml'} or {workspace.root / 'people.yaml'}")
    rules = workspace.matching_rules(principal, profile, channel_id)
    if key == "outsider":
        # An outsider answer can reach the sender. Team rules name people and owners,
        # so an outsider run gets only a rule that names the outsider principal.
        rules = [r for r in rules if "outsider" in r.principals]
    for index, rule in enumerate(rules):
        heading = "## Team rules\nThe team wrote these rules. They apply to this run.\n\n"
        text += "\n\n" + (heading if index == 0 else "") + f"### {rule.name}\n{rule.body}"
        check_size(workspace.root / rule.path)
    if key != "outsider":
        for index, skill in enumerate(workspace.skills):
            if index == 0:
                text += (
                    "\n\n## Skills\nA skill is a procedure that the team wrote. When a request matches "
                    "a skill, load its steps with `skills_get` and follow them.\n"
                )
            else:
                text += "\n"
            text += f"• {skill.name}: {skill.description}"
            check_size(f"{workspace.root / skill.path} (skill {skill.name})")
    return text


def validate_workspace_prompts(workspace) -> None:
    """Reject a workspace before any profile can receive an oversized prompt."""
    principals = ["team", "outsider", *(f"person:{p.key}" for p in workspace.people if p.active)]
    channels = [None, *(c.id for c in workspace.config.slack.channels)]
    for profile in PROFILE_TEXTS:
        for principal in principals:
            for channel_id in channels:
                build(
                    workspace,
                    principal,
                    profile,
                    channel_id,
                    event_channel="mcp",
                    open_asks=10**19,
                    _fact_reserve=RUN_FACTS_RESERVE,
                )


async def build_for_run(conn, workspace, authority, event, *, now: datetime | None = None) -> str:
    """Read the facts that need the ledger, then build. The run manager calls this."""
    open_asks = 0
    if authority.profile == "person_dm" and authority.person_id:
        rows = await asks.list_open(
            conn, owner_person_id=authority.person_id, principal=authority.principal
        )
        open_asks = len(rows)
    prompt = build(
        workspace,
        authority.principal,
        authority.profile,
        event.payload.get("channel_id"),
        now=now,
        event_channel=event.channel,
        open_asks=open_asks,
    )
    if event.channel == "digest":
        prompt += (
            "\n\nThis run writes a morning brief. Follow its greeting and format instructions. "
            "Call digest_snapshot. Use only that snapshot. Return the message as final text."
        )
    return prompt


def _facts(workspace, principal, key, channel_id, now, event_channel, open_asks) -> str:
    """State who, when, and where. The model cannot see these facts any other way."""
    config = workspace.config
    person = None
    if principal.startswith("person:"):
        person = next((p for p in workspace.people if p.key == principal[7:]), None)
    lines = [f"• Team: {config.team_name}", f"• Principal: {principal}"]
    if person:
        detail = f"• Person: {person.name} (`{person.key}`), role {person.role}"
        if person.areas:
            detail += ", areas " + ", ".join(person.areas)
        lines.append(detail)
    if key in {"person_dm", "team_channel", "tainted", "routine", "agent_request"}:
        lines.append("• GitHub repositories: " + (", ".join(config.github.repositories) or "none"))
    timezone = person.timezone if person else config.timezone
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    lines.append(f"• Local time: {local:%A} {local.day} {local:%B %Y, %H:%M}, {timezone}")
    where = _where(workspace, channel_id, event_channel, key)
    if where:
        lines.append(f"• Channel: {where}")
    if key == "person_dm" and person and open_asks > 0:
        lines.append(
            f"• Open asks of {person.name}: {open_asks}. Read them with a tool before you list them."
        )
    return "## Run facts\n" + "\n".join(lines)


def _where(workspace, channel_id, event_channel, key) -> str | None:
    """Name the place of the event. A configured channel also shows its mode."""
    channel = next((c for c in workspace.config.slack.channels if c.id == channel_id), None)
    if channel:
        return f"Slack channel #{channel.name} (`{channel.id}`), mode {channel.mode}"
    if event_channel == "slack":
        return "a Slack DM" if key == "person_dm" else "Slack"
    return CHANNEL_NAMES.get(event_channel or "")
