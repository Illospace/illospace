"""Keep a call record without keeping secret argument values."""

import hmac
import json
import os
import secrets

from psycopg.types.json import Jsonb

from illo.db import Connection
from illo.vault import Vault


async def argument_digest(conn: Connection, args: dict) -> str:
    """Without a vault key, omit the digest rather than permit value guesses."""
    vault_key = os.environ.get("ILLO_VAULT_KEY")
    if not vault_key:
        return "unavailable"
    vault = Vault(vault_key)
    key = await vault.get(conn, "audit_digest_key")
    if key is None:
        # Only initialization needs a lock. Concurrent writers must use one key.
        await conn.execute("SELECT pg_advisory_xact_lock(697637125)")
        key = await vault.get(conn, "audit_digest_key")
        if key is None:
            key = secrets.token_hex(32)
            await vault.set(conn, "audit_digest_key", key)
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hmac.digest(key.encode(), encoded, "sha256").hex()


async def write(
    conn: Connection,
    *,
    principal: str,
    tool: str,
    args: dict,
    decision: str,
    reason: str,
    run_id: int | None = None,
    token_id: int | None = None,
) -> None:
    """Authenticate the full arguments. Store field names with all values redacted."""
    digest = await argument_digest(conn, args)
    redacted = {key: "[redacted]" for key in args}
    await conn.execute(
        """
        INSERT INTO audit (run_id,token_id,principal,tool,args_digest,args,decision,reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """,
        (run_id, token_id, principal, tool, digest, Jsonb(redacted), decision, reason),
    )
