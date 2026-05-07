"""
Claude Code Auth — use Claude Code subscription for API calls.

Two authentication methods (in priority order):
1. Setup token (sk-ant-oat01-...) — long-lived, no refresh needed, simplest
2. OAuth credentials — short-lived access token + refresh token, auto-refreshes

Both are stored in ~/.claude/.credentials.json (or macOS Keychain).
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger("brain.systems.auth")

# Anthropic OAuth constants (from pi-ai/anthropic.js)
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_SETUP_TOKEN_PREFIX = "sk-ant-oat01-"

# Credential file path
_CRED_PATH = Path.home() / ".claude" / ".credentials.json"

# Cache
_cached_token: dict | None = None
_token_lock = threading.Lock()


# ── Credential Storage ───────────────────────────────────────

def _read_cred_file() -> dict:
    """Read the full credentials file. Returns {} if missing."""
    if not _CRED_PATH.is_file():
        return {}
    try:
        return json.loads(_CRED_PATH.read_text())
    except Exception:
        return {}


def _write_cred_file(data: dict) -> None:
    """Write credentials file (creates parent dir if needed)."""
    _CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CRED_PATH.write_text(json.dumps(data, indent=2))


def _read_keychain_credentials() -> dict | None:
    """Read Claude Code OAuth credentials from macOS Keychain."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout.strip())
    except Exception as e:
        logger.debug("Keychain read failed: %s", e)
        return None


def _read_all_credentials() -> dict:
    """Read credentials from keychain (macOS) or file, merged."""
    keychain = _read_keychain_credentials() or {}
    file_data = _read_cred_file()
    # Keychain takes precedence for OAuth, file for setup token
    merged = {**file_data}
    if keychain.get("claudeAiOauth"):
        merged["claudeAiOauth"] = keychain["claudeAiOauth"]
    return merged


# ── Setup Token (sk-ant-oat01-...) ───────────────────────────

def get_setup_token() -> str | None:
    """Get stored setup token if available."""
    data = _read_all_credentials()
    token = data.get("setupToken", "")
    if token and token.startswith(_SETUP_TOKEN_PREFIX):
        return token
    return None


def save_setup_token(token: str) -> None:
    """Store a setup token."""
    if not token.startswith(_SETUP_TOKEN_PREFIX):
        raise ValueError(f"Invalid setup token — must start with {_SETUP_TOKEN_PREFIX}")
    data = _read_cred_file()
    data["setupToken"] = token
    _write_cred_file(data)
    invalidate_cache()
    logger.info("Stored Claude Code setup token")


# ── OAuth Credentials ────────────────────────────────────────

def _parse_oauth(data: dict) -> dict | None:
    """Extract OAuth creds from credential data."""
    oauth = data.get("claudeAiOauth")
    if not oauth or not isinstance(oauth, dict):
        return None
    access = oauth.get("accessToken", "")
    refresh = oauth.get("refreshToken", "")
    expires = oauth.get("expiresAt", 0)
    if not access or not isinstance(expires, (int, float)):
        return None
    return {"access": access, "refresh": refresh, "expires": expires}


def _refresh_token(refresh_token: str) -> dict:
    """Refresh an expired OAuth token via Anthropic's token endpoint."""
    resp = httpx.post(
        _TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "client_id": _CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    expires_ms = int(time.time() * 1000) + data["expires_in"] * 1000 - 5 * 60 * 1000
    return {
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires": expires_ms,
    }


def _save_oauth_credentials(creds: dict) -> None:
    """Write refreshed OAuth credentials back to storage."""
    # Try macOS keychain first
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                oauth = data.get("claudeAiOauth", {})
                oauth["accessToken"] = creds["access"]
                oauth["refreshToken"] = creds["refresh"]
                oauth["expiresAt"] = creds["expires"]
                data["claudeAiOauth"] = oauth
                subprocess.run(
                    ["security", "add-generic-password", "-U",
                     "-s", "Claude Code-credentials",
                     "-a", "Claude Code",
                     "-w", json.dumps(data)],
                    capture_output=True, timeout=5, check=True,
                )
                logger.info("Wrote refreshed OAuth token to keychain")
                return
        except Exception as e:
            logger.debug("Keychain write failed: %s", e)

    # File fallback
    data = _read_cred_file()
    oauth = data.get("claudeAiOauth", {})
    oauth["accessToken"] = creds["access"]
    oauth["refreshToken"] = creds["refresh"]
    oauth["expiresAt"] = creds["expires"]
    data["claudeAiOauth"] = oauth
    _write_cred_file(data)
    logger.info("Wrote refreshed OAuth token to credentials file")


# ── Main API ─────────────────────────────────────────────────

def get_access_token() -> tuple[str | None, str]:
    """Get a valid API token for the Anthropic SDK.

    Returns: (token, method) where method is "setup_token", "oauth", or ("", "none")
    Thread-safe with caching.
    """
    global _cached_token

    with _token_lock:
        # Check cache
        if _cached_token:
            if _cached_token.get("type") == "setup_token":
                return _cached_token["access"], "setup_token"
            if _cached_token["expires"] > time.time() * 1000:
                return _cached_token["access"], "oauth"

        all_creds = _read_all_credentials()

        # 1. Setup token (highest priority — simplest, no refresh)
        setup = all_creds.get("setupToken", "")
        if setup and setup.startswith(_SETUP_TOKEN_PREFIX):
            _cached_token = {"access": setup, "type": "setup_token"}
            logger.info("Using Claude Code setup token")
            return setup, "setup_token"

        # 2. OAuth credentials
        oauth = _parse_oauth(all_creds)
        if not oauth:
            return None, "none"

        now_ms = time.time() * 1000

        if oauth["expires"] > now_ms:
            _cached_token = oauth
            logger.info("Using Claude Code OAuth token (expires in %.0f min)",
                        (oauth["expires"] - now_ms) / 60000)
            return oauth["access"], "oauth"

        # Need refresh
        if not oauth.get("refresh"):
            logger.warning("OAuth token expired, no refresh token")
            return None, "none"

        try:
            new_creds = _refresh_token(oauth["refresh"])
            _save_oauth_credentials(new_creds)
            _cached_token = new_creds
            logger.info("Refreshed OAuth token (valid for %.0f min)",
                        (new_creds["expires"] - now_ms) / 60000)
            return new_creds["access"], "oauth"
        except Exception as e:
            logger.warning("OAuth refresh failed: %s", e)
            return None, "none"


# Backwards compat
def get_oauth_access_token() -> str | None:
    """Get token (any method). Returns None if unavailable."""
    token, _ = get_access_token()
    return token


def get_auth_status() -> dict:
    """Return detailed auth status for the dashboard."""
    result = {
        "authenticated": False,
        "method": "none",
        "setup_token": {"found": False},
        "oauth": {"found": False},
    }

    all_creds = _read_all_credentials()

    # Setup token
    setup = all_creds.get("setupToken", "")
    if setup and setup.startswith(_SETUP_TOKEN_PREFIX):
        result["setup_token"] = {
            "found": True,
            "prefix": setup[:18] + "…",
        }
        result["authenticated"] = True
        result["method"] = "setup_token"

    # OAuth
    oauth = _parse_oauth(all_creds)
    if oauth:
        now_ms = time.time() * 1000
        expired = oauth["expires"] <= now_ms
        remaining_min = max(0, (oauth["expires"] - now_ms) / 60000)
        result["oauth"] = {
            "found": True,
            "expired": expired,
            "expires_in_min": round(remaining_min, 1),
            "has_refresh": bool(oauth.get("refresh")),
        }
        if not expired and not result["authenticated"]:
            result["authenticated"] = True
            result["method"] = "oauth"

    return result


def invalidate_cache() -> None:
    """Force re-read of credentials on next call."""
    global _cached_token
    with _token_lock:
        _cached_token = None
