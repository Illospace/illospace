"""Encrypted connector grants. The caller owns the transaction."""

import json
from datetime import UTC, datetime, timedelta

from illo.db import all_rows, one
from illo.vault import Vault


def vault(ctx) -> Vault:
    if not ctx.config.vault_key:
        raise ValueError("Set ILLO_VAULT_KEY before a connector sign-in")
    return Vault(ctx.config.vault_key.get_secret_value())


async def load(
    conn, ctx, key: str, person_id: int | None, *, lock: bool = False
) -> tuple[dict, dict]:
    row = await one(
        conn,
        "SELECT * FROM connector_grants WHERE connector_key=%s "
        "AND person_id IS NOT DISTINCT FROM %s AND revoked_at IS NULL"
        + (" FOR UPDATE" if lock else ""),
        (key, person_id),
    )
    if not row:
        raise ValueError("This connector needs a sign-in")
    return row, json.loads(vault(ctx).decrypt(row["secret"]))


async def save(conn, ctx, key: str, person_id: int | None, token: dict, scopes: list[str]) -> None:
    expires = token.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires)) if expires else None
    await conn.execute(
        """INSERT INTO connector_grants (connector_key,person_id,secret,scopes,expires_at)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (connector_key,person_id) DO UPDATE
        SET secret=EXCLUDED.secret,scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
        revoked_at=NULL,broken_at=NULL,notified_at=NULL""",
        (key, person_id, vault(ctx).encrypt(json.dumps(token)), list(scopes), expires_at),
    )


async def status(conn) -> list[dict]:
    return await all_rows(
        conn,
        """SELECT g.connector_key,p.key AS person,g.expires_at,
        g.revoked_at,g.broken_at FROM connector_grants g LEFT JOIN people p ON p.id=g.person_id
        ORDER BY g.connector_key,p.key""",
    )
