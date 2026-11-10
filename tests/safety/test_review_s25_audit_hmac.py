import asyncio
import hashlib
import hmac
import json
from pathlib import Path

from cryptography.fernet import Fernet

from illo.db import all_rows, one
from illo.ledger import audit
from illo.vault import Vault


async def write(conn, args):
    await audit.write(
        conn, principal="person:ada", tool="test", args=args, decision="allow", reason="Test"
    )


async def test_rule_01_audit_digest_uses_a_vault_key(conn, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ILLO_VAULT_KEY", key)
    args = {"code": "123456"}
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    await write(conn, args)
    row = await one(conn, "SELECT * FROM audit")
    assert row["args_digest"] != hashlib.sha256(encoded).hexdigest()
    secret = await Vault(key).get(conn, "audit_digest_key")
    assert secret
    assert row["args_digest"] == hmac.digest(secret.encode(), encoded, "sha256").hex()
    assert row["args"] == {"code": "[redacted]"}
    stored = await one(conn, "SELECT secret FROM secrets WHERE name='audit_digest_key'")
    assert secret.encode() not in bytes(stored["secret"])
    await write(conn, args)
    rows = await all_rows(conn, "SELECT args_digest FROM audit ORDER BY id")
    assert rows[0] == rows[1]
    await Vault(key).set(conn, "audit_digest_key", "a-different-test-key")
    await write(conn, args)
    last = await one(conn, "SELECT args_digest FROM audit ORDER BY id DESC LIMIT 1")
    assert last != rows[0]


async def test_rule_01_no_vault_key_omits_digest(conn, monkeypatch):
    monkeypatch.delenv("ILLO_VAULT_KEY", raising=False)
    await write(conn, {"code": "123456"})
    assert (await one(conn, "SELECT args_digest FROM audit"))["args_digest"] == "unavailable"


async def test_rule_01_migration_clears_old_guessable_digests(conn):
    await write(conn, {"code": "123456"})
    await conn.execute("UPDATE audit SET args_digest=%s", (hashlib.sha256(b"123456").hexdigest(),))
    migration = Path(__file__).parents[2] / "illo/migrations/0203_mcp_audit_digests.sql"
    await conn.execute(migration.read_text())
    assert (await one(conn, "SELECT args_digest FROM audit"))["args_digest"] == "unavailable"


async def test_rule_01_concurrent_audits_initialize_one_key(pool, monkeypatch):
    monkeypatch.setenv("ILLO_VAULT_KEY", Fernet.generate_key().decode())

    async def call():
        async with pool.connection() as conn:
            await write(conn, {"code": "123456"})

    await asyncio.gather(*(call() for _ in range(4)))
    async with pool.connection() as conn:
        row = await one(conn, "SELECT count(DISTINCT args_digest) AS n FROM audit")
        assert row["n"] == 1
        row = await one(conn, "SELECT count(*) AS n FROM secrets WHERE name='audit_digest_key'")
        assert row["n"] == 1
