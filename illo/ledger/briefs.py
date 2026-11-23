"""Store each person's brief preferences and the first-brief notice."""

from illo.db import one


async def settings(conn, person):
    await conn.execute(
        """INSERT INTO brief_preferences (person_id,announced)
        VALUES (%s,EXISTS(SELECT 1 FROM routine_state WHERE key=%s AND last_status='ok'))
        ON CONFLICT (person_id) DO NOTHING""",
        (person["id"], f"digest:{person['key']}"),
    )
    return await one(conn, "SELECT * FROM brief_preferences WHERE person_id=%s", (person["id"],))


def schedule(config, preferences):
    fields = config.digest.schedule.split()
    if preferences["local_time"]:
        hour, minute = preferences["local_time"].split(":")
        fields[:2] = [str(int(minute)), str(int(hour))]
    return " ".join(fields)
