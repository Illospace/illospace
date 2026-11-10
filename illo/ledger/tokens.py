"""Store only token hashes. Return each new token once."""

import hashlib
import secrets

from illo.db import Connection, all_rows, one


def digest(token: str) -> str:
    """Use SHA-256 on high-entropy random tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create(
    conn: Connection, person_id: int, label: str, scopes: list[str] | None = None
) -> str:
    """Create a client token for an active person."""
    token = secrets.token_urlsafe(32)
    row = await one(
        conn,
        """
        INSERT INTO tokens (person_id,label,token_hash,scopes)
        SELECT id,%s,%s,%s FROM people WHERE id = %s AND active RETURNING id
    """,
        (label, digest(token), scopes or [], person_id),
    )
    if not row:
        raise ValueError("The person is missing or inactive")
    return token


async def verify(conn: Connection, token: str) -> dict | None:
    """Reject revoked tokens and inactive owners. Record use without the token."""
    return await one(
        conn,
        """
        UPDATE tokens t SET last_used_at = now() FROM people p
        WHERE t.token_hash = %s AND t.revoked_at IS NULL AND p.id = t.person_id AND p.active
        RETURNING t.id,t.person_id,t.label,t.scopes,p.key AS person_key
    """,
        (digest(token),),
    )


async def list_tokens(conn: Connection, person_id: int | None = None) -> list[dict]:
    """List token metadata. Never return hashes."""
    return await all_rows(
        conn,
        """
        SELECT id,person_id,label,scopes,created_at,last_used_at,revoked_at FROM tokens
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
