"""Store only token hashes. Return each new token once."""

import hashlib
import secrets

from psycopg import AsyncConnection, Error

from illo.db import Connection, all_rows, one


def digest(token: str) -> str:
    """Use SHA-256 on high-entropy random tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create(
    conn: Connection, person_id: int, label: str, scopes: list[str] | None = None,
    *, agent_key: str | None = None, workspace=None,
) -> str:
    """Create a client token for an active person."""
    scopes = ["collector"] if scopes is None else scopes
    if scopes not in (["collector"], ["agent"]):
        raise ValueError("Choose one token scope: collector or agent")
    if agent_key:
        person = await one(conn, "SELECT key FROM people WHERE id=%s AND active", (person_id,))
        agent = next((a for a in workspace.agents if a.key == agent_key), None) if workspace else None
        if scopes != ["agent"] or not person or not agent or agent.owner != person["key"]:
            raise ValueError("The agent must exist, use the agent scope, and belong to the token's person")
    token = secrets.token_urlsafe(32)
    row = await one(
        conn,
        """
        INSERT INTO tokens (person_id,label,token_hash,scopes,agent_key)
        SELECT id,%s,%s,%s,%s FROM people WHERE id = %s AND active RETURNING id
    """,
        (label, digest(token), scopes, agent_key, person_id),
    )
    if not row:
        raise ValueError("The person is missing or inactive")
    return token


async def verify(conn: Connection, token: str) -> dict | None:
    """Reject revoked tokens and inactive owners. Record use without the token."""
    row = await one(
        conn,
        """
        SELECT t.id,t.person_id,t.label,t.scopes,t.agent_key,p.key AS person_key,
          (t.last_used_at IS NULL OR t.last_used_at <= clock_timestamp()-interval '1 minute') AS touch
        FROM tokens t JOIN people p ON p.id=t.person_id
        WHERE t.token_hash=%s AND t.revoked_at IS NULL AND p.active
    """,
        (digest(token),),
    )
    if row and row.pop("touch"):
        # Commit use on another connection. The request never owns this row lock.
        try:
            async with await AsyncConnection.connect(
                conn.info.dsn, password=conn.info.password, autocommit=True,
                options=f"{conn.info.options} -c lock_timeout=100ms",
            ) as usage:
                await usage.execute("""
                    UPDATE tokens SET last_used_at=clock_timestamp() WHERE id IN (
                      SELECT t.id FROM tokens t JOIN people p ON p.id=t.person_id
                      WHERE t.id=%s AND t.revoked_at IS NULL AND p.active
                        AND (t.last_used_at IS NULL OR t.last_used_at <= clock_timestamp()-interval '1 minute')
                      FOR NO KEY UPDATE OF t SKIP LOCKED)
                """, (row["id"],))
        except Error:
            # A busy table or a refused connection must not refuse the request. The next use records the time.
            pass
    return row


async def list_tokens(conn: Connection, person_id: int | None = None) -> list[dict]:
    """List token metadata. Never return hashes."""
    return await all_rows(
        conn,
        """
        SELECT id,person_id,label,scopes,agent_key,created_at,last_used_at,revoked_at FROM tokens
        WHERE (%s::bigint IS NULL OR person_id = %s) ORDER BY id
    """,
        (person_id, person_id),
    )


async def revoke(conn: Connection, token_id: int) -> bool:
    """Revoke a client token once."""
    return bool(
        await one(
            conn,
            """
        UPDATE tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id
    """,
            (token_id,),
        )
    )
