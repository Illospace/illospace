"""Resolve the principal from verified channel identities."""

from dataclasses import dataclass

from illo.ledger import people, tokens


@dataclass(frozen=True)
class Authority:
    principal: str
    profile: str
    person_id: int | None = None
    tainted: bool = False


async def resolve(conn, event, workspace) -> Authority | None:
    """Only this gateway function creates authority for an inbound event."""
    if event.channel == "slack":
        person = (
            None if event.payload.get("outside_workspace")
            else await people.by_slack_id(conn, event.sender)
        )
        if event.kind in {"dm", "message.im"}:
            if not person or event.payload.get("bot") or event.payload.get("webhook"):
                return None
            return Authority(f"person:{person['key']}", "person_dm", person["id"])
        if not event.payload.get("channel_id"):
            return None
        channel = workspace.config.slack.channel(event.payload["channel_id"])
        if channel.mode == "off":
            return None
        if channel.mode == "listen" and not event.payload.get("mentioned"):
            return None
        tainted = any(event.payload.get(key) for key in ("bot", "bot_id", "webhook", "form"))
        if event.payload.get("channel_class") == "members" and (
                tainted or not person or not event.payload.get("mentioned")):
            return None
        if not channel.listed and (tainted or not person):
            return None
        if not tainted and not person:
            return None
        return Authority("team", "tainted" if tainted else "team_channel", tainted=tainted)
    if event.channel == "email":
        person = await people.by_email(conn, event.sender)
        tainted = not person or bool(
            event.payload.get("forwarded") or event.payload.get("contains_outside_text")
        )
        return Authority(
            f"person:{person['key']}" if person else "outsider",
            "tainted" if tainted else "person_dm",
            person["id"] if person else None,
            tainted,
        )
    if event.channel == "team_digest":
        return Authority("team", "routine") if workspace.config.digest.team else None
    if event.channel == "digest":
        return await person_authority(conn, event.payload.get("principal", ""))
    if event.channel == "routine":
        routine = next(
            (r for r in workspace.routines if r.name == event.payload.get("routine") and r.enabled),
            None,
        )
        if not routine or routine.principal != event.payload.get("principal"):
            return None
        if routine.principal == "team":
            return Authority("team", "routine")
        return await person_authority(conn, routine.principal)
    if event.channel == "mcp":
        # The agent channel passes the raw bearer only in sender, never in stored payload.
        token = await tokens.verify(conn, event.sender)
        if token:
            return Authority(f"person:{token['person_key']}", "agent_request", token["person_id"])
        return None
    if event.channel == "signals":
        return Authority("team", "tainted", tainted=True)
    return None


async def person_authority(conn, principal):
    """Person routines and briefs keep the person's authority."""
    if not principal.startswith("person:"):
        return None
    person = await people.get(conn, principal[7:])
    return Authority(principal, "person_dm", person["id"]) if person else None
