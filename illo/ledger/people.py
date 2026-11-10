"""Read the active people copied from the workspace."""

from illo.db import Connection, all_rows, one


async def list_people(conn: Connection) -> list[dict]:
    """Return active people in key order."""
    return await all_rows(conn, "SELECT * FROM people WHERE active ORDER BY key")


async def get(conn: Connection, key: str) -> dict | None:
    """Find an active person by workspace key."""
    return await one(conn, "SELECT * FROM people WHERE key = %s AND active", (key,))


async def by_slack_id(conn: Connection, slack_id: str) -> dict | None:
    """Map a channel identity to an active person."""
    return await one(conn, "SELECT * FROM people WHERE slack_id = %s AND active", (slack_id,))


async def by_github_login(conn: Connection, login: str) -> dict | None:
    """GitHub logins are case insensitive."""
    return await one(
        conn, "SELECT * FROM people WHERE lower(github_login) = lower(%s) AND active", (login,)
    )


async def by_email(conn: Connection, email: str) -> dict | None:
    """Match a normalized mailbox address."""
    return await one(
        conn, "SELECT * FROM people WHERE %s = ANY(emails) AND active", (email.strip().lower(),)
    )
