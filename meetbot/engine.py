"""Headful Playwright engine for joining and observing Google Meet."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from meetbot.browser_control import click_first_match, first_visible
from meetbot.browser_diagnostics import capture_failure_evidence
from meetbot.caption_control import (
    CaptionController,
    caption_language_menu_labels,
    caption_option_matches,
)
from meetbot.config import MeetbotConfig
from meetbot.models import EngineResult, MeetbotSessionOutcome, SessionEvents

logger = logging.getLogger(__name__)


def _lobby_timeout_error(timeout_seconds: int) -> str:
    if timeout_seconds and timeout_seconds % 60 == 0:
        count = timeout_seconds // 60
        duration = f"{count} minute{'s' if count != 1 else ''}"
    else:
        duration = f"{timeout_seconds} second{'s' if timeout_seconds != 1 else ''}"
    return (
        f"Nobody admitted the bot within {duration}. Invite its Google account "
        "to the calendar event, or admit it manually when it knocks."
    )


@dataclass(slots=True)
class _BrowserRuntime:
    page: Any
    leave_requested: asyncio.Event


class PlaywrightMeetEngine:
    """Drive one visible Chromium page and report only observed Meet states."""

    def __init__(self, config: MeetbotConfig) -> None:
        self._config = config
        self._caption_control = CaptionController(config)
        self._runtimes: dict[str, _BrowserRuntime] = {}

    async def run(
        self,
        *,
        session_id: str,
        meeting_url: str,
        display_name: str,
        events: SessionEvents,
    ) -> EngineResult:
        # Keep Playwright out of module import paths used by browser-free tests.
        from playwright.async_api import async_playwright

        using_storage_state = self._config.storage_state_path.is_file()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    f"--lang={self._config.ui_locale}",
                ],
            )
            # Meet selectors below use English control text. The UI locale is a DOM
            # contract, not a caption-language preference; spoken French stays separate.
            context_options: dict[str, object] = {
                "viewport": {"width": 1280, "height": 720},
                # Media silence is layered. Chromium's fake-UI flag bypasses
                # prompts, so no single layer is the guarantee: Playwright applies
                # an empty permission override, fake-device substitutes synthetic
                # sources, and the container exposes no real capture hardware. The
                # Meet mute pass below adds UI defense in depth. The join can
                # continue without a grant through _dismiss_media_prompt.
                "permissions": [],
                "locale": self._config.ui_locale,
                "extra_http_headers": {
                    "Accept-Language": f"{self._config.ui_locale},en;q=0.9",
                },
            }
            if using_storage_state:
                context_options["storage_state"] = str(self._config.storage_state_path)
                logger.info("Meetbot session %s is using Google storage state", session_id)
            else:
                logger.info("Meetbot session %s is joining as an anonymous guest", session_id)

            context = await browser.new_context(**context_options)
            page = await context.new_page()
            runtime = _BrowserRuntime(page=page, leave_requested=asyncio.Event())
            self._runtimes[session_id] = runtime
            try:
                await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60_000)
                await events.status("lobby")
                await self._dismiss_media_prompt(page)
                await self._mute_before_join(page)
                if not using_storage_state:
                    blocked_error = await self._fill_guest_name(page, display_name)
                    if blocked_error:
                        return EngineResult(
                            reason=MeetbotSessionOutcome.REFUSED,
                            terminal_status="failed",
                            error=blocked_error,
                        )
                if not await self._click_join(page):
                    blocked_error = await _join_blocked_error(page)
                    if blocked_error:
                        return EngineResult(
                            reason=MeetbotSessionOutcome.REFUSED,
                            terminal_status="failed",
                            error=blocked_error,
                        )
                    raise RuntimeError("Google Meet did not show an Ask to join or Join now button.")

                admission_result = await self._wait_for_admission(page, runtime.leave_requested)
                if admission_result is not None:
                    return admission_result
                await events.status("admitted")
                await self._caption_control.start(page, events, session_id)
                return await self._monitor_call(page, runtime.leave_requested, events)
            except Exception:
                await capture_failure_evidence(
                    page,
                    session_id,
                    "join-failure",
                    debug_dir=self._config.debug_dir,
                )
                raise
            finally:
                self._runtimes.pop(session_id, None)
                await context.close()
                await browser.close()

    async def request_leave(self, session_id: str) -> None:
        runtime = self._runtimes.get(session_id)
        if runtime:
            runtime.leave_requested.set()

    async def send_chat(self, session_id: str, text: str) -> None:
        runtime = self._runtimes.get(session_id)
        if not runtime:
            raise RuntimeError("The meeting browser is not active.")
        page = runtime.page
        opened = await click_first_match(
            page,
            (
                'button[aria-label*="Chat with everyone" i]',
                'button[aria-label*="chat" i]',
            ),
        )
        if not opened:
            raise RuntimeError("Google Meet chat is not available.")
        editor = await first_visible(
            page,
            (
                'textarea[aria-label*="Send a message" i]',
                'textarea[placeholder*="Send" i]',
                '[contenteditable="true"][aria-label*="message" i]',
            ),
        )
        if editor is None:
            raise RuntimeError("Google Meet chat input was not found.")
        await editor.fill(text)
        await editor.press("Enter")

    async def _dismiss_media_prompt(self, page: Any) -> None:
        """Accept Meet's no-devices prejoin dialog when it appears.

        With no mic/cam permission granted, Meet often interposes a
        "Do you want people to see and hear you?" dialog whose decline
        path is the button we want.
        """

        for _ in range(10):
            clicked = await click_first_match(
                page,
                (
                    'button:has-text("Continue without microphone and camera")',
                    'button:has-text("Continue without microphone")',
                ),
            )
            if clicked:
                return
            if await first_visible(
                page,
                (
                    'input[aria-label*="Your name" i]',
                    'button:has-text("Ask to join")',
                    'button:has-text("Join now")',
                ),
            ) is not None:
                return
            await asyncio.sleep(0.5)

    async def _mute_before_join(self, page: Any) -> None:
        await _ensure_media_muted(
            page,
            device="microphone",
            off_selectors=(
                'button[aria-label*="Turn off microphone" i]',
                '[role="button"][data-tooltip*="Turn off microphone" i]',
            ),
            on_selectors=(
                'button[aria-label*="Turn on microphone" i]',
                '[role="button"][data-tooltip*="Turn on microphone" i]',
            ),
        )
        await _ensure_media_muted(
            page,
            device="camera",
            off_selectors=(
                'button[aria-label*="Turn off camera" i]',
                '[role="button"][data-tooltip*="Turn off camera" i]',
            ),
            on_selectors=(
                'button[aria-label*="Turn on camera" i]',
                '[role="button"][data-tooltip*="Turn on camera" i]',
            ),
        )

    async def _fill_guest_name(self, page: Any, display_name: str) -> str | None:
        # The prejoin UI renders progressively; poll briefly before concluding
        # the field is absent, and distinguish "blocked" from "not rendered".
        for _ in range(20):
            field = await first_visible(
                page,
                (
                    'input[aria-label*="Your name" i]',
                    'input[placeholder*="Your name" i]',
                    'input[aria-label="Name"]',
                ),
            )
            if field is not None:
                await field.fill(display_name)
                return None
            if blocked_error := await _join_blocked_error(page):
                return blocked_error
            await asyncio.sleep(0.5)
        raise RuntimeError("Google Meet did not show the anonymous guest name field.")

    async def _click_join(self, page: Any) -> bool:
        role_button = page.get_by_role(
            "button",
            name=re.compile(r"^(Ask to join|Join now)$", re.IGNORECASE),
        )
        for index in range(await role_button.count()):
            candidate = role_button.nth(index)
            if await candidate.is_visible():
                await candidate.click()
                return True
        return await click_first_match(
            page,
            ('button:has-text("Ask to join")', 'button:has-text("Join now")'),
        )

    async def _wait_for_admission(
        self,
        page: Any,
        leave_requested: asyncio.Event,
    ) -> EngineResult | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self._config.lobby_timeout_seconds)
        while True:
            if leave_requested.is_set():
                return EngineResult(reason="leave_requested")
            if await _is_in_call(page):
                return None
            if await _call_ended(page):
                return EngineResult(reason="call_ended")
            rejection = page.get_by_text(
                re.compile(
                    r"(You can.?t join|Your request to join was denied|No one responded)",
                    re.IGNORECASE,
                )
            )
            if await rejection.count() and await rejection.first.is_visible():
                refusal_text = ""
                try:
                    refusal_text = " ".join(
                        str(await rejection.first.inner_text() or "").split()
                    )
                except Exception:
                    logger.debug("Google Meet refusal text could not be read", exc_info=True)
                return EngineResult(
                    reason=MeetbotSessionOutcome.REFUSED,
                    terminal_status="failed",
                    error=(
                        refusal_text
                        or "Google Meet did not admit the bot to the call."
                    ),
                )
            if loop.time() >= deadline:
                return EngineResult(
                    reason=MeetbotSessionOutcome.NOT_ADMITTED,
                    terminal_status="failed",
                    error=_lobby_timeout_error(self._config.lobby_timeout_seconds),
                )
            await asyncio.sleep(0.5)

    async def _monitor_call(
        self,
        page: Any,
        leave_requested: asyncio.Event,
        events: SessionEvents,
    ) -> EngineResult:
        alone_since: float | None = None
        controls_missing_since: float | None = None
        last_participant_check = 0.0
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            if leave_requested.is_set():
                await _click_leave(page)
                return EngineResult(reason="leave_requested")
            if await _call_ended(page):
                return EngineResult(reason="call_ended")

            if await _is_in_call(page):
                controls_missing_since = None
            elif controls_missing_since is None:
                controls_missing_since = now
            elif now - controls_missing_since >= 10.0:
                return EngineResult(reason="call_ui_gone")

            if now - last_participant_check >= 10.0:
                count, names = await _participant_snapshot(page)
                if names:
                    await events.participants(names)
                if count == 1:
                    alone_since = alone_since or now
                elif count is not None:
                    alone_since = None
                if alone_since is not None and now - alone_since >= 300.0:
                    await _click_leave(page)
                    return EngineResult(reason="alone_for_five_minutes")
                last_participant_check = now
            await asyncio.sleep(1.0)


async def _click_leave(page: Any) -> None:
    await click_first_match(
        page,
        (
            'button[aria-label*="Leave call" i]',
            '[role="button"][data-tooltip*="Leave call" i]',
        ),
    )


JOIN_BLOCKED_ERROR = (
    "Google Meet refused the join outright: this meeting does not allow "
    "anonymous guests (\"You can't join this video call\"). Sign the bot in "
    "by installing its Google storage state, or invite the bot's account to "
    "the calendar event."
)


async def _join_blocked_error(page: Any) -> str | None:
    """Detect Meet's hard block page, observed live on 2026-08-03.

    Workspace-hosted meetings can require signed-in participants; anonymous
    visitors then get "You can't join this video call" with no name field
    and no way to knock. Naming the real cause beats reporting whichever
    downstream selector happened to miss.
    """

    blocked = page.get_by_text(
        re.compile(r"You can.?t join this (video )?call", re.IGNORECASE)
    )
    if await blocked.count() and await blocked.first.is_visible():
        return JOIN_BLOCKED_ERROR
    return None


async def _ensure_media_muted(
    page: Any,
    *,
    device: str,
    off_selectors: tuple[str, ...],
    on_selectors: tuple[str, ...],
) -> None:
    """Best-effort Meet-level toggle-off; never a join precondition.

    The context permission override, synthetic sources, and container isolation
    operate below this UI control. When media access is unavailable, Meet can
    omit the control altogether; absence is not an error (the first live join
    failed here when this raised).
    """

    if await first_visible(page, on_selectors) is not None:
        return
    if not await click_first_match(page, off_selectors):
        logger.info("Google Meet shows no %s control; nothing to mute.", device)
        return
    if await first_visible(page, on_selectors) is None:
        logger.warning("Google Meet did not confirm the %s toggled off.", device)


async def _is_in_call(page: Any) -> bool:
    if page.is_closed():
        return False
    selector = (
        'button[aria-label*="Leave call" i],'
        '[role="button"][data-tooltip*="Leave call" i]'
    )
    return await page.locator(selector).count() > 0


async def _call_ended(page: Any) -> bool:
    if page.is_closed():
        return True
    ended = page.get_by_text(
        re.compile(
            r"(You.?ve left the meeting|The call has ended|You were removed from the meeting)",
            re.IGNORECASE,
        )
    )
    return bool(await ended.count() and await ended.first.is_visible())


async def _participant_snapshot(page: Any) -> tuple[int | None, list[str]]:
    result = await page.evaluate(
        r"""
        () => {
          let count = null;
          for (const button of document.querySelectorAll('button[aria-label]')) {
            const label = button.getAttribute('aria-label') || '';
            if (!/(participants|people|show everyone)/i.test(label)) continue;
            const match = label.match(/(?:\(|\s)(\d+)(?:\)|\s|$)/);
            if (match) { count = Number(match[1]); break; }
          }
          const names = new Set();
          const selectors = '[data-participant-name],[data-self-name],[data-participant-id][aria-label]';
          for (const node of document.querySelectorAll(selectors)) {
            const name = (node.getAttribute('data-participant-name') ||
              node.getAttribute('data-self-name') || node.getAttribute('aria-label') || '').trim();
            if (name && name.length <= 120) names.add(name);
          }
          return {count, names: Array.from(names)};
        }
        """
    )
    if not isinstance(result, dict):
        return None, []
    raw_count = result.get("count")
    count = int(raw_count) if isinstance(raw_count, (int, float)) else None
    raw_names = result.get("names")
    names = [str(name) for name in raw_names] if isinstance(raw_names, list) else []
    return count, names
