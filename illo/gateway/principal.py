"""Resolve the principal from verified channel identities."""

from dataclasses import dataclass

from illo.db import one
from illo.ledger import people, tokens


@dataclass(frozen=True)
class Authority:
    principal: str
    profile: str
    person_id: int | None = None
    tainted: bool = False
    personal_read: bool = False


async def resolve(conn, event, workspace) -> Authority | None:
    """Only this gateway function creates authority for an inbound event."""
    if event.channel == "board":
        from illo.board.acl import thread_access

        thread_id = event.payload.get("thread_id")
        if type(thread_id) is not int or not 0 < thread_id < 2**63:
            return None
        thread = await thread_access(conn, thread_id)
        if not thread or thread["state"] != "open":
            return None
        if thread["effective_owner"] is None:
            return Authority("team", "board_coordinator")
        owner = await one(conn, "SELECT id,key,active FROM people WHERE id=%s", (thread["effective_owner"],))
        if owner and owner["active"]:
            return Authority(f"person:{owner['key']}", "board_coordinator", owner["id"], personal_read=True)
        return None
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


async def smoke_authority(conn, kind, person_key=None) -> Authority | None:
    """Give the payer check the authority of the route. The smoke tool has no team access."""
    person = await people.get(conn, person_key) if person_key else None
    if person_key and person is None:
        return None
    if kind in {"person_dm", "agent_request"} or (
        person and kind in {"digest", "routine", "deep", "board_coordinator"}
    ):
        profile = kind if kind in {"agent_request", "board_coordinator"} else "person_dm"
        if person:
            return Authority(f"person:{person_key}", profile, person["id"])
        # Own-grant policy rejects this authority. Team-grant policy can use it.
        return Authority("team", profile)
    profile = {"digest": "routine", "deep": "team_channel"}.get(kind, kind)
    return Authority("team", profile, tainted=kind == "tainted")
