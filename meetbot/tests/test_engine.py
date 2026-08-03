from __future__ import annotations

import asyncio

import pytest

from meetbot.config import MeetbotConfig
from meetbot.engine import (
    PlaywrightMeetEngine,
    caption_language_menu_labels,
    caption_option_matches,
)


class _HiddenLocator:
    first: _HiddenLocator

    def __init__(self) -> None:
        self.first = self

    async def count(self) -> int:
        return 0

    async def is_visible(self) -> bool:
        return False


class _NeverAdmittedPage:
    class _Keyboard:
        async def press(self, key: str) -> None:
            return None

    keyboard = _Keyboard()

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> _HiddenLocator:
        return _HiddenLocator()

    def get_by_text(self, pattern: object) -> _HiddenLocator:
        return _HiddenLocator()


class _WarningEvents:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def warning(self, message: str) -> None:
        self.messages.append(message)

    async def status(self, status: object) -> None:
        return None

    async def caption(
        self,
        speaker: str,
        text: str,
        line_id: str | None = None,
    ) -> None:
        return None

    async def participants(self, names: list[str]) -> None:
        return None


def test_caption_language_tags_map_to_meet_labels_with_subtag_fallback() -> None:
    assert caption_language_menu_labels("fr-FR") == ("French", "fr")
    assert caption_language_menu_labels("EN-us") == ("English", "en")
    assert caption_language_menu_labels("de-DE") == ("de",)

    assert caption_option_matches("French", "fr-FR")
    assert caption_option_matches("DE-de — German", "de-DE")
    assert not caption_option_matches("Swedish", "de-DE")


@pytest.mark.asyncio
async def test_lobby_wait_is_bounded_and_returns_not_admitted_failure() -> None:
    engine = PlaywrightMeetEngine(MeetbotConfig(lobby_timeout_seconds=0))

    result = await engine._wait_for_admission(
        _NeverAdmittedPage(),
        asyncio.Event(),
    )

    assert result is not None
    assert result.reason == "not_admitted"
    assert result.terminal_status == "failed"
    assert result.error == (
        "Nobody admitted the bot within 0 seconds. Invite its Google account "
        "to the calendar event, or admit it manually when it knocks."
    )


@pytest.mark.asyncio
async def test_caption_language_failure_emits_transcript_risk_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("meetbot.engine.asyncio.sleep", no_sleep)
    events = _WarningEvents()
    engine = PlaywrightMeetEngine(MeetbotConfig(caption_language="fr-FR"))

    await engine._enable_captions(_NeverAdmittedPage(), events)

    assert events.messages == [
        "Could not confirm the caption language is fr-FR; "
        "the transcript may be translated or empty."
    ]


def test_missing_mute_controls_do_not_block_the_join() -> None:
    """A bot with no mic/cam permission sees no mute controls at all.

    The first live join failed because control absence raised; absence is
    the normal permissionless state and must be tolerated silently.
    """

    from meetbot.engine import _ensure_media_muted

    asyncio.run(
        _ensure_media_muted(
            _NeverAdmittedPage(),
            device="microphone",
            off_selectors=('button[aria-label*="Turn off microphone" i]',),
            on_selectors=('button[aria-label*="Turn on microphone" i]',),
        )
    )


class _BlockedPage(_NeverAdmittedPage):
    class _VisibleLocator:
        def __init__(self) -> None:
            self.first = self

        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

    def get_by_text(self, pattern: object) -> object:
        return self._VisibleLocator()


def test_blocked_meeting_reports_the_real_cause() -> None:
    """Anonymous-join hard block (seen live 2026-08-03) must name itself."""

    from meetbot.engine import JOIN_BLOCKED_ERROR, _raise_if_join_blocked

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_raise_if_join_blocked(_BlockedPage()))
    assert str(excinfo.value) == JOIN_BLOCKED_ERROR
