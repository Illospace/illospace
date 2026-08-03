"""Environment-backed configuration for the meetbot service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MeetbotConfig:
    """Runtime settings with the defaults defined by the meetbot spec."""

    api_token: str | None = None
    bridge_token: str | None = None
    callback_url: str = "http://api:8000"
    display_name: str = "Illo (notetaker)"
    max_session_seconds: int = 7_200
    caption_language: str = "fr-FR"
    ui_locale: str = "en-US"
    lobby_timeout_seconds: int = 600
    uploads_root: Path = Path("/app/brain/uploads")
    private_root: Path = Path("/data/private/meetbot")
    storage_state_path: Path = Path("/data/private/meetbot/google-storage-state.json")
    caption_warning_seconds: int = 90

    @classmethod
    def from_env(cls) -> MeetbotConfig:
        """Build configuration from the public meetbot environment contract."""

        return cls(
            api_token=_optional_env("ILLO_MEETBOT_TOKEN"),
            bridge_token=_optional_env("ILLO_MEETBOT_BRIDGE_TOKEN"),
            callback_url=(
                os.getenv("ILLO_MEETBOT_CALLBACK_URL", "").strip() or "http://api:8000"
            ).rstrip("/"),
            display_name=os.getenv("ILLO_MEETBOT_DISPLAY_NAME", "Illo (notetaker)").strip()
            or "Illo (notetaker)",
            max_session_seconds=_positive_int_env("ILLO_MEETBOT_MAX_SESSION_SECONDS", 7_200),
            caption_language=(
                os.getenv("ILLO_MEETBOT_CAPTION_LANGUAGE", "fr-FR").strip() or "fr-FR"
            ),
            ui_locale=os.getenv("ILLO_MEETBOT_UI_LOCALE", "en-US").strip() or "en-US",
            lobby_timeout_seconds=_positive_int_env(
                "ILLO_MEETBOT_LOBBY_TIMEOUT_SECONDS",
                600,
            ),
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
