"""Vault encryption helpers shared by systems code and migrations."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

from brain.kernel import config


def _read_key_from_env_file(env_path: Path) -> str:
    """Try to read VAULT_MASTER_KEY from a .env file."""
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("VAULT_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _get_fernet() -> Fernet:
    """Return a Fernet instance using VAULT_MASTER_KEY."""
    key = os.environ.get("VAULT_MASTER_KEY", "")
    if not key:
        brain_env = Path(config.BRAIN_DIR) / "brain" / ".env"
        key = _read_key_from_env_file(brain_env)
    if not key:
        core_env = Path(config.BRAIN_DIR) / "core" / ".env"
        key = _read_key_from_env_file(core_env)
        if key:
            new_env = Path(config.BRAIN_DIR) / "brain" / ".env"
            try:
                with open(new_env, "a") as f:
                    f.write(f"VAULT_MASTER_KEY={key}\n")
            except OSError:
                pass
    if not key:
        root_env = Path(config.BRAIN_DIR) / ".env"
        key = _read_key_from_env_file(root_env)
    if key:
        os.environ["VAULT_MASTER_KEY"] = key
    if not key:
        raise RuntimeError(
            "VAULT_MASTER_KEY is required. Refusing to auto-generate a vault "
            "key because that can silently strand existing secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(value: str) -> bytes:
    return _get_fernet().encrypt(value.encode())


def _decrypt(token: bytes) -> str:
    return _get_fernet().decrypt(token).decode()
