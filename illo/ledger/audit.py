"""Keep a call record without keeping secret argument values."""

import hashlib
import json

from psycopg.types.json import Jsonb

from illo.db import Connection


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
    """Hash the full arguments. Store field names with all values redacted."""
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    redacted = {key: "[redacted]" for key in args}
    await conn.execute(
        """
        INSERT INTO audit (run_id,token_id,principal,tool,args_digest,args,decision,reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """,
        (run_id, token_id, principal, tool, digest, Jsonb(redacted), decision, reason),
    )
