from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from meetbot.browser_diagnostics import capture_failure_evidence
from meetbot.caption_control import CaptionController
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
    tmp_path: Path,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("meetbot.caption_control.asyncio.sleep", no_sleep)
    events = _WarningEvents()
    caption_control = CaptionController(
        MeetbotConfig(caption_language="fr-FR", private_root=tmp_path)
    )

    await caption_control._enable_captions(_NeverAdmittedPage(), events, "session-1")

    assert events.messages == [
        "Could not confirm the caption language is fr-FR; "
        "the transcript may be translated or empty."
    ]


@pytest.mark.asyncio
async def test_caption_failure_capture_precedes_each_strategy_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captures: list[tuple[str, bool]] = []

    class _RecordingPage(_NeverAdmittedPage):
        def __init__(self) -> None:
            self.overlay_open = False

        class _RecordingKeyboard:
            def __init__(self, page: _RecordingPage) -> None:
                self.page = page

            async def press(self, key: str) -> None:
                assert key == "Escape"
                self.page.overlay_open = False

        @property
        def keyboard(self) -> _RecordingKeyboard:
            return self._RecordingKeyboard(self)

    async def failed_strategy(page: Any) -> bool:
        assert not page.overlay_open
        page.overlay_open = True
        return False

    async def capture(
        page: Any,
        session_id: str,
        label: str,
        *,
        debug_dir: Path,
    ) -> None:
        assert session_id == "session-1"
        assert debug_dir == tmp_path / "debug"
        captures.append((label, page.overlay_open))

    caption_control = CaptionController(MeetbotConfig(private_root=tmp_path))
    monkeypatch.setattr(caption_control, "_set_visible_caption_language", failed_strategy)
    monkeypatch.setattr(caption_control, "_set_via_caption_settings_control", failed_strategy)
    monkeypatch.setattr(caption_control, "_set_via_meet_settings", failed_strategy)
    monkeypatch.setattr("meetbot.caption_control.capture_failure_evidence", capture)

    result = await caption_control._set_caption_language(_RecordingPage(), "session-1")

    assert result is None
    assert all(overlay_open for _, overlay_open in captures)
    labels = {label for label, _ in captures}
    assert "visible-language-control" in labels
    assert "caption-settings-control" in labels
    assert "more-options-settings-captions" in labels


@pytest.mark.asyncio
async def test_failure_capture_names_are_distinct_by_session_and_label(
    tmp_path: Path,
) -> None:
    class _CapturePage:
        def __init__(self) -> None:
            self.screenshot_paths: list[Path] = []

        async def screenshot(self, *, path: str, full_page: bool) -> None:
            assert full_page
            self.screenshot_paths.append(Path(path))

        async def evaluate(self, script: str) -> list[dict[str, str | None]]:
            assert "menuitemradio" in script
            return [
                {
                    "tag": "button",
                    "role": None,
                    "aria-label": "Caption settings",
                    "data-tooltip": None,
                    "text": "",
                }
            ]

    page = _CapturePage()
    debug_dir = tmp_path / "debug"
    captures = (
        ("session-1", "join-failure"),
        ("session-1", "visible-language-control"),
        ("session-1", "caption-settings-control"),
        ("session-2", "caption-settings-control"),
    )

    for session_id, label in captures:
        await capture_failure_evidence(
            page,
            session_id,
            label,
            debug_dir=debug_dir,
        )

    expected_stems = {f"{session_id}-{label}" for session_id, label in captures}
    assert {path.stem for path in page.screenshot_paths} == expected_stems
    assert {path.stem for path in debug_dir.glob("*.json")} == expected_stems
    assert len(page.screenshot_paths) == len(captures)
    assert len(list(debug_dir.glob("*.json"))) == len(captures)
    inventory = json.loads(
        (debug_dir / "session-1-visible-language-control.json").read_text()
    )
    assert inventory == [
        {
            "tag": "button",
            "role": None,
            "aria-label": "Caption settings",
            "data-tooltip": None,
            "text": "",
        }
    ]


@pytest.mark.asyncio
async def test_capture_errors_do_not_interrupt_caption_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _BrokenCapturePage(_NeverAdmittedPage):
        def __init__(self) -> None:
            self.escape_count = 0
            self.screenshot_count = 0
            self.evaluate_count = 0

            class _Keyboard:
                async def press(inner_self, key: str) -> None:
                    if key == "Escape":
                        self.escape_count += 1

            self.keyboard = _Keyboard()

        async def screenshot(self, *, path: str, full_page: bool) -> None:
            self.screenshot_count += 1
            raise RuntimeError("screenshot failed")

        async def evaluate(self, script: str) -> object:
            self.evaluate_count += 1
            raise RuntimeError("evaluate failed")

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("meetbot.caption_control.asyncio.sleep", no_sleep)
    page = _BrokenCapturePage()
    events = _WarningEvents()
    caption_control = CaptionController(
        MeetbotConfig(caption_language="fr-FR", private_root=tmp_path)
    )

    await caption_control._enable_captions(page, events, "session-1")

    assert page.escape_count == 3
    assert page.screenshot_count == 3
    assert page.evaluate_count == 3
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

    from meetbot.engine import JOIN_BLOCKED_ERROR, _join_blocked_error

    assert asyncio.run(_join_blocked_error(_BlockedPage())) == JOIN_BLOCKED_ERROR
