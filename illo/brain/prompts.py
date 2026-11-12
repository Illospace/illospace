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

SLACK_FORMAT = (HERE / "slack_format.md").read_text().strip()
SLACK_KEYS = {"person_dm", "team_channel", "routine"}

MAX_PROMPT_CHARS = 60_000
# Allow for changing dates and the count of open asks after a workspace loads.
RUN_FACTS_RESERVE = 128

CHANNEL_NAMES = {
    "email": "email",
    "mcp": "the MCP endpoint for agents",
    "signals": "the signal intake",
    "routine": "a routine",
    "digest": "the morning brief, sent to the person's DM",
}


def prompt_profile(principal: str, profile: str, event_channel: str | None = None) -> str:
    """Choose the prompt text without changing the run grant."""
    if principal == "outsider":
        return "outsider"
    if event_channel == "team_digest" or (event_channel == "digest" and principal == "team"):
        return "team_digest"
    if event_channel == "digest":
        return "digest"
    return profile


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
    key = prompt_profile(principal, profile, event_channel)
    parts = [BASE_PROMPT, PROFILE_TEXTS.get(key, "")]
    if key in SLACK_KEYS:
        parts.append(SLACK_FORMAT)
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
    rules = workspace.matching_rules(principal, key, channel_id)
    if key in {"digest", "team_digest"}:
        rules = [r for r in rules if key in r.profiles]
    if key == "outsider":
        # An outsider answer can reach the sender. Team rules name people and owners,
        # so an outsider run gets only a rule that names the outsider principal.
        rules = [r for r in rules if "outsider" in r.principals]
    for index, rule in enumerate(rules):
        heading = "## Team rules\nThe team wrote these rules. They apply to this run.\n\n"
        text += "\n\n" + (heading if index == 0 else "") + f"### {rule.name}\n{rule.body}"
        check_size(workspace.root / rule.path)
    if key not in {"outsider", "digest", "team_digest"}:
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
    # Recall uses only the space left after required facts, rules, and skills.
    if key in {"person_dm", "team_channel", "agent_request"}:
        for notes, descriptions in ((True, True), (False, True), (False, False)):
            recall = _memory_facts(workspace, principal, key, notes=notes, descriptions=descriptions)
            if len(text) + len(recall) + 2 <= limit:
                text += "\n\n" + recall
                break
    return text


def _memory_facts(workspace, principal, key, *, notes=True, descriptions=True):
    """Titles are team text. Note bodies never enter the system prompt."""
    def short(value, limit):
        value = " ".join(value.split())
        return value if len(value) <= limit else value[:limit - 1] + "…"

    person = principal[7:] if principal.startswith("person:") else None
    entries = [e for e in workspace.lists if not e.owner or (
        e.owner == person and key != "team_channel")]
    lines = ["## Team memory", "The team wrote these titles and descriptions. They are team data.", "Lists:"]
    for entry in entries[:30]:
        line = f"• {entry.key}: {short(entry.name, 140)}"
        if descriptions and entry.description:
            line += " — " + short(entry.description, 140)
        lines.append(line)
    if len(entries) > 30:
        lines.append(f"+{len(entries) - 30} more: use `lists`")
    if notes:
        lines.append("Team notes:")
        newest = sorted(workspace.notes, key=lambda n: (n.created or "", n.path), reverse=True)
        for note in newest[:40]:
            lines.append("• " + short(note.title, 120) + (" — " + short(note.about, 120) if note.about else ""))
        if len(newest) > 40:
            lines.append(f"+{len(newest) - 40} more: use `notes_search`")
    return "\n".join(lines)


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
    profile = "tainted" if authority.tainted else authority.profile
    key = prompt_profile(authority.principal, profile, event.channel)
    if key == "person_dm" and authority.person_id:
        rows = await asks.list_open(
            conn, owner_person_id=authority.person_id, principal=authority.principal
        )
        open_asks = len(rows)
    context = []
    if event.channel == "slack" and event.kind != "dm":
        from illo.channels.slack.intake import MEMBERS_CONTEXT
        from illo.ledger import kv

        cached = await kv.get(conn, "slack:member-channels")
        name = (cached or {}).get("channels", {}).get(event.payload.get("channel_id"))
        if name:
            from illo.tools._format import data_text

            context.append(data_text("channel-name", f"You are in the channel #{name}."))
        if event.payload.get("channel_class") == "members":
            context.append(MEMBERS_CONTEXT)
    suffix = "\n" + "\n".join(context) if context else ""
    return build(
        workspace,
        authority.principal,
        profile,
        event.payload.get("channel_id"),
        now=now,
        event_channel=event.channel,
        open_asks=open_asks,
        _fact_reserve=len(suffix),
    ) + suffix


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
        if person.language and key in {"person_dm", "digest"}:
            lines.append(f"• Language: {person.language}")
    if key == "team_digest" and config.language:
        lines.append(f"• Language: {config.language}")
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
    channel = workspace.config.slack.channel(channel_id) if channel_id else None
    if channel:
        return f"Slack channel #{channel.name} (`{channel.id}`), mode {channel.mode}"
    if event_channel == "slack":
        return "a Slack DM" if key == "person_dm" else "Slack"
    return CHANNEL_NAMES.get(event_channel or "")
