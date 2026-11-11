"""Build task authority from an authenticated run or client token."""

from illo.db import one
from illo.ledger.tasks import TaskError, Viewer


async def token_viewer(conn, workspace, token):
    person = token["person_key"]
    key = token.get("agent_key")
    if key and not any(a.key == key and a.owner == person for a in workspace.agents):
        raise TaskError("The agent is missing or has a different owner. Create a new token.")
    held = () if key else tuple(["person:" + person, *["agent:" + a.key for a in workspace.agents if a.owner == person]])
    return Viewer(person, "agent:" + (key or f"unnamed:{token['id']}"), holder_refs=held)


async def run_viewer(ctx):
    run = ctx.run
    if run["tainted"] or run["principal"] == "outsider":
        raise TaskError("This run cannot read tasks.")
    if run["profile"] == "agent_request":
        token = await one(ctx.conn, """SELECT t.*,p.key AS person_key FROM tokens t
            JOIN people p ON p.id=t.person_id JOIN submissions s ON s.token_id=t.id
            JOIN events e ON e.external_id=s.event_id AND e.channel='mcp'
            JOIN runs r ON r.event_id=e.id WHERE r.id=%s AND t.revoked_at IS NULL
            AND p.active""", (run["id"],))
        if not token:
            raise TaskError("The agent token was not found.")
        return await token_viewer(ctx.conn, ctx.app.workspace, token)
    person = await one(ctx.conn, "SELECT key FROM people WHERE id=%s AND active", (run.get("person_id"),))
    if run["profile"] == "team_channel":
        person = await one(ctx.conn, """SELECT p.key FROM people p JOIN events e
            ON p.key=e.payload->>'task_actor' JOIN runs r ON r.event_id=e.id
            WHERE r.id=%s AND p.active""", (run["id"],))
    if run["profile"] in {"person_dm", "team_channel"}:
        if not person:
            raise TaskError("The run needs an identified person.")
        # These profiles use the person's own request. Their Done confirmation counts as a person's confirmation.
        return Viewer(person["key"], "person:" + person["key"], human=True, team_only=run["profile"] == "team_channel")
    return Viewer(person["key"] if person else None,
                  "person:" + person["key"] if person else "code", team=not person)
