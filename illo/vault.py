"""Encrypt secrets before they reach Postgres."""

from cryptography.fernet import Fernet, InvalidToken

from illo.db import Connection, all_rows, one


class Vault:
    """Use a Fernet key from ILLO_VAULT_KEY. Never print the key or a value."""

    def __init__(self, key: str):
        try:
            self.cipher = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("ILLO_VAULT_KEY must be a Fernet key") from exc

    def encrypt(self, value: str) -> bytes:
        """Authenticate and encrypt one value."""
        return self.cipher.encrypt(value.encode())

    def decrypt(self, value: bytes) -> str:
        """Reject changed ciphertext or a wrong key."""
        try:
            return self.cipher.decrypt(bytes(value)).decode()
        except InvalidToken as exc:
            raise ValueError("The secret cannot be decrypted with this vault key") from exc

    async def set(self, conn: Connection, name: str, value: str) -> None:
        """Insert or replace a named secret."""
        await conn.execute(
            """
            INSERT INTO secrets (name,secret) VALUES (%s,%s)
            ON CONFLICT (name) DO UPDATE SET secret = EXCLUDED.secret
        """,
            (name, self.encrypt(value)),
        )

    async def get(self, conn: Connection, name: str) -> str | None:
        """Read a secret for trusted server code only."""
        row = await one(conn, "SELECT secret FROM secrets WHERE name = %s", (name,))
        return self.decrypt(row["secret"]) if row else None


async def get_names(conn: Connection) -> list[str]:
    """List names without returning secret values."""
    return [r["name"] for r in await all_rows(conn, "SELECT name FROM secrets ORDER BY name")]


async def delete(conn: Connection, name: str) -> bool:
    """Delete one named secret."""
    return bool(await one(conn, "DELETE FROM secrets WHERE name = %s RETURNING name", (name,)))
