"""ChatGPT grants and sign-in state. No plaintext credential enters the ledger."""

import json
from datetime import UTC, datetime

from illo.connectors.store import vault
from illo.db import all_rows, one


async def load(conn, person_id, *, lock=False):
    return await one(
        conn,
        "SELECT * FROM connector_grants WHERE connector_key='chatgpt' AND person_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (person_id,),
    )


async def save(conn, ctx, person_id, token):
    await conn.execute(
        """INSERT INTO connector_grants (connector_key,person_id,secret,scopes,expires_at)
        VALUES ('chatgpt',%s,%s,%s,%s) ON CONFLICT (connector_key,person_id) DO UPDATE
        SET secret=EXCLUDED.secret,scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
        revoked_at=NULL,broken_at=NULL,notified_at=NULL""",
        (
            person_id,
            vault(ctx).encrypt(json.dumps(token)),
            ["openid", "profile", "email", "offline_access"],
            datetime.fromtimestamp(token["expires_at"], UTC),
        ),
    )


async def disconnect(conn, person_id):
    await conn.execute("SELECT id FROM people WHERE id=%s FOR UPDATE", (person_id,))
    await conn.execute(
        "UPDATE connector_oauth_states SET cancelled_at=clock_timestamp(), "
        "consumed_at=coalesce(consumed_at,clock_timestamp()) "
        "WHERE connector_key='chatgpt' AND person_id=%s",
        (person_id,),
    )
    await conn.execute(
        "DELETE FROM connector_grants WHERE connector_key='chatgpt' AND person_id=%s",
        (person_id,),
    )


async def status(conn, person_id=None):
    return await all_rows(
        conn,
        """SELECT p.key AS person,g.expires_at,g.broken_at,g.revoked_at FROM people p
        LEFT JOIN connector_grants g ON g.person_id=p.id AND g.connector_key='chatgpt'
        WHERE p.active AND (%s::bigint IS NULL OR p.id=%s) ORDER BY p.key""",
        (person_id, person_id),
    )


async def state(conn, state_hash, *, lock=False):
    return await one(
        conn,
        """SELECT * FROM connector_oauth_states WHERE connector_key='chatgpt'
        AND state_hash=%s AND expires_at>clock_timestamp() AND consumed_at IS NULL
        AND cancelled_at IS NULL
        AND EXISTS (SELECT 1 FROM people WHERE id=person_id AND active)"""
        + (" FOR UPDATE" if lock else ""),
        (state_hash,),
    )
