"""Find the person from the authenticated run, including a team channel request."""

from illo.db import one


async def run_person_id(ctx):
    if ctx.run["profile"] == "team_channel":
        person = await one(
            ctx.conn,
            """SELECT p.id FROM people p JOIN events e
            ON p.key=e.payload->>'task_actor' JOIN runs r ON r.event_id=e.id
            WHERE r.id=%s AND p.active""",
            (ctx.run["id"],),
        )
        return person["id"] if person else None
    return ctx.run.get("person_id")
