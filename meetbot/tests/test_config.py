from __future__ import annotations

from pathlib import Path

import pytest

from meetbot.config import MeetbotConfig


def test_config_uses_the_spec_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ILLO_MEETBOT_TOKEN",
        "ILLO_MEETBOT_BRIDGE_TOKEN",
        "ILLO_MEETBOT_CALLBACK_URL",
        "ILLO_MEETBOT_DISPLAY_NAME",
        "ILLO_MEETBOT_MAX_SESSION_SECONDS",
        "ILLO_MEETBOT_CAPTION_LANGUAGE",
        "ILLO_MEETBOT_UI_LOCALE",
        "ILLO_MEETBOT_LOBBY_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    config = MeetbotConfig.from_env()

    assert config.api_token is None
    assert config.bridge_token is None
    assert config.callback_url == "http://api:8000"
    assert config.display_name == "Illo (notetaker)"
    assert config.max_session_seconds == 7_200
    assert config.caption_language == "fr-FR"
    assert config.ui_locale == "en-US"
    assert config.lobby_timeout_seconds == 600
    assert config.debug_dir == Path("/data/private/meetbot/debug")


def test_debug_directory_resolves_from_private_root(tmp_path: Path) -> None:
    config = MeetbotConfig(private_root=tmp_path / "private")

    assert config.debug_dir == tmp_path / "private" / "debug"


def test_config_reads_language_locale_and_lobby_timeout_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ILLO_MEETBOT_CAPTION_LANGUAGE", "en-US")
    monkeypatch.setenv("ILLO_MEETBOT_UI_LOCALE", "en-GB")
    monkeypatch.setenv("ILLO_MEETBOT_LOBBY_TIMEOUT_SECONDS", "45")

    config = MeetbotConfig.from_env()

    assert config.caption_language == "en-US"
    assert config.ui_locale == "en-GB"
    assert config.lobby_timeout_seconds == 45


def test_config_rejects_non_positive_session_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ILLO_MEETBOT_MAX_SESSION_SECONDS", "0")

    with pytest.raises(ValueError, match="must be a positive integer"):
        MeetbotConfig.from_env()


def test_config_rejects_non_positive_lobby_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ILLO_MEETBOT_LOBBY_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="must be a positive integer"):
        MeetbotConfig.from_env()
