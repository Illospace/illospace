"""Headful Playwright engine for joining and observing Google Meet."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from meetbot.browser_diagnostics import capture_failure_evidence
from meetbot.config import MeetbotConfig
from meetbot.models import EngineResult, MeetbotSessionOutcome, SessionEvents

logger = logging.getLogger(__name__)

_CAPTION_LANGUAGE_MENU_LABELS = {
    "fr-fr": "French",
    "en-us": "English",
}


def caption_language_menu_labels(language_tag: str) -> tuple[str, ...]:
    """Return Meet's known label followed by the BCP-47 subtag fallback."""

    normalized = _normalize_language_tag(language_tag)
    if not normalized:
        return ()
    subtag = normalized.partition("-")[0]
    known_label = _CAPTION_LANGUAGE_MENU_LABELS.get(normalized)
    return (known_label, subtag) if known_label else (subtag,)


def caption_option_matches(option_text: str, language_tag: str) -> bool:
    """Match a Meet menu option by known label, tag, or standalone language subtag."""

    text = " ".join(str(option_text or "").split())
    if not text:
        return False
    normalized_text = text.casefold().replace("_", "-")
    normalized_tag = _normalize_language_tag(language_tag)
    labels = caption_language_menu_labels(language_tag)
    if not normalized_tag or not labels:
        return False
    known_label = _CAPTION_LANGUAGE_MENU_LABELS.get(normalized_tag)
    if known_label and normalized_text in {
        known_label.casefold(),
        f"{known_label.casefold()} (beta)",
    }:
        return True
    if re.search(rf"(?<![a-z]){re.escape(normalized_tag)}(?![a-z])", normalized_text):
        return True
    subtag = labels[-1].casefold()
    return bool(re.search(rf"(?<![a-z]){re.escape(subtag)}(?![a-z])", normalized_text))


def _normalize_language_tag(language_tag: str) -> str:
    return str(language_tag or "").strip().replace("_", "-").casefold()


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


_CAPTION_OBSERVER_SCRIPT = r"""
(() => {
  if (window.__illoMeetbotCaptionObserver) return;

  const nodeIds = new WeakMap();
  const lastValues = new Map();
  let nextNodeId = 1;
  const captionSelector = [
    '[role="region"][aria-label*="aption" i]',
    '[data-caption-id]',
    '[jsname="YSxPC"]',
    '[jsname="tgaKEf"]',
    '.a4cQT'
  ].join(',');
  const rowSelector = [
    '[data-caption-row]',
    '[jsname="dsyhDe"]',
    '.CNusmb',
    '.TBMuR',
    '.nMcdL'
  ].join(',');
  const speakerSelector = [
    '[data-speaker-name]',
    '.KcIKyf',
    '.zs7s8d',
    '.NWpY1d',
    '.xoMHSc',
    'span[jsname="YSxPC"]'
  ].join(',');
  const textSelector = [
    '[data-caption-text]',
    '.bh44bd',
    '.iTTPOb',
    'span[jsname="tgaKEf"]'
  ].join(',');

  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const identity = (node) => {
    if (!nodeIds.has(node)) nodeIds.set(node, nextNodeId++);
    return `caption-${nodeIds.get(node)}`;
  };
  const emit = (row) => {
    if (!(row instanceof Element)) return;
    const speakerNode = row.querySelector(speakerSelector);
    const textNodes = Array.from(row.querySelectorAll(textSelector));
    let speaker = clean(speakerNode && speakerNode.textContent);
    let text = clean(textNodes.map((node) => node.textContent).join(' '));
    if (!text) {
      const lines = String(row.innerText || '').split('\n').map(clean).filter(Boolean);
      if (!speaker && lines.length > 1) speaker = lines.shift() || '';
      text = clean(lines.filter((line) => line !== speaker).join(' '));
    }
    if (!text) return;
    const lineId = identity(row);
    const fingerprint = `${speaker}\n${text}`;
    if (lastValues.get(lineId) === fingerprint) return;
    lastValues.set(lineId, fingerprint);
    window.__illoMeetbotCaption({line_id: lineId, speaker, text});
  };
  const scan = (container) => {
    const rows = Array.from(container.querySelectorAll(rowSelector));
    if (rows.length) {
      rows.forEach(emit);
      return;
    }
    emit(container);
  };
  const scanFrom = (target) => {
    const element = target instanceof Element ? target : target.parentElement;
    if (!element) return;
    const row = element.closest(rowSelector);
    if (row) {
      emit(row);
      return;
    }
    const closestCaption = element.closest(captionSelector);
    if (closestCaption) scan(closestCaption);
    element.querySelectorAll(captionSelector).forEach(scan);
  };

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      scanFrom(mutation.target);
      for (const node of mutation.addedNodes) scanFrom(node);
    }
  });
  observer.observe(document.body, {subtree: true, childList: true, characterData: true});
  document.querySelectorAll(captionSelector).forEach(scan);
  window.__illoMeetbotCaptionObserver = observer;
})();
"""


@dataclass(slots=True)
class _BrowserRuntime:
    page: Any
    leave_requested: asyncio.Event


class PlaywrightMeetEngine:
    """Drive one visible Chromium page and report only observed Meet states."""

    def __init__(self, config: MeetbotConfig) -> None:
        self._config = config
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
                # Deliberately NO microphone/camera permission grant: a page
                # that never receives the permission structurally cannot send
                # audio or video, which is the real silence guarantee. The
                # mute-toggle pass below is best-effort cosmetics on top.
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
                await self._attach_caption_observer(page, events)
                await self._enable_captions(page, events, session_id)
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
        opened = await _click_first_visible(
            page,
            (
                'button[aria-label*="Chat with everyone" i]',
                'button[aria-label*="chat" i]',
            ),
        )
        if not opened:
            raise RuntimeError("Google Meet chat is not available.")
        editor = await _first_visible(
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
            clicked = await _click_first_visible(
                page,
                (
                    'button:has-text("Continue without microphone and camera")',
                    'button:has-text("Continue without microphone")',
                ),
            )
            if clicked:
                return
            if await _first_visible(
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
            field = await _first_visible(
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
        return await _click_first_visible(
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

    async def _attach_caption_observer(self, page: Any, events: SessionEvents) -> None:
        async def on_caption(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            speaker = str(payload.get("speaker") or "Unknown speaker")
            text = str(payload.get("text") or "").strip()
            line_id = str(payload.get("line_id") or "").strip() or None
            if text:
                await events.caption(speaker, text, line_id)

        await page.expose_function("__illoMeetbotCaption", on_caption)
        await page.evaluate(_CAPTION_OBSERVER_SCRIPT)

    async def _enable_captions(
        self,
        page: Any,
        events: SessionEvents,
        session_id: str,
    ) -> None:
        await page.keyboard.press("c")
        await asyncio.sleep(1.0)
        enabled = await _first_visible(
            page,
            (
                'button[aria-label*="Turn off captions" i]',
                '[role="button"][data-tooltip*="Turn off captions" i]',
            ),
        )
        if enabled is None:
            clicked = await _click_first_visible(
                page,
                (
                    'button[aria-label*="Turn on captions" i]',
                    'button[aria-label*="Show captions" i]',
                    '[role="button"][data-tooltip*="Turn on captions" i]',
                ),
            )
            if not clicked:
                logger.warning("Meetbot could not verify or click the Google Meet captions control")

        strategy = await self._set_caption_language(page, session_id)
        if strategy:
            logger.info(
                "Meetbot confirmed caption language %s with selector strategy %s",
                self._config.caption_language,
                strategy,
            )
            return
        warning = (
            f"Could not confirm the caption language is {self._config.caption_language}; "
            "the transcript may be translated or empty."
        )
        logger.warning("%s", warning)
        logger.info(
            "Meetbot caption-language diagnostic evidence for session %s is under %s",
            session_id,
            self._config.debug_dir,
        )
        await events.warning(warning)

    async def _set_caption_language(self, page: Any, session_id: str) -> str | None:
        strategies = (
            ("visible-language-control", self._set_visible_caption_language),
            ("caption-settings-control", self._set_via_caption_settings_control),
            ("more-options-settings-captions", self._set_via_meet_settings),
        )
        for name, strategy in strategies:
            try:
                if await strategy(page):
                    return name
            except Exception:
                logger.debug(
                    "Meet caption-language selector strategy %s failed",
                    name,
                    exc_info=True,
                )
            # Meet closes the menu on Escape, so live selector evidence must be
            # recorded while the failed strategy's last overlay is still open.
            await capture_failure_evidence(
                page,
                session_id,
                name,
                debug_dir=self._config.debug_dir,
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                logger.debug("Meetbot could not close a caption settings overlay", exc_info=True)
        return None

    async def _set_visible_caption_language(self, page: Any) -> bool:
        control = await _caption_language_control(page)
        if control is None:
            return False
        return await _choose_caption_language(page, control, self._config.caption_language)

    async def _set_via_caption_settings_control(self, page: Any) -> bool:
        opened = await _click_first_visible(
            page,
            (
                'button[aria-label*="Caption settings" i]',
                'button[aria-label*="Caption language" i]',
                '[role="button"][data-tooltip*="Caption settings" i]',
                '[role="button"][aria-haspopup][aria-label*="caption" i]',
            ),
        )
        if not opened:
            return False
        await asyncio.sleep(0.25)
        return await self._set_visible_caption_language(page)

    async def _set_via_meet_settings(self, page: Any) -> bool:
        more_opened = await _click_first_visible(
            page,
            (
                'button[aria-label="More options" i]',
                '[role="button"][data-tooltip="More options" i]',
            ),
        )
        if not more_opened:
            return False
        await asyncio.sleep(0.25)
        settings_opened = await _click_first_visible(
            page,
            (
                '[role="menuitem"]:has-text("Settings")',
                '[role="button"]:has-text("Settings")',
                'button:has-text("Settings")',
            ),
        )
        if not settings_opened:
            return False
        await asyncio.sleep(0.25)
        captions_opened = await _click_first_visible(
            page,
            (
                '[role="tab"]:has-text("Captions")',
                '[role="menuitem"]:has-text("Captions")',
                '[role="button"][aria-label="Captions" i]',
                'button:has-text("Captions")',
            ),
        )
        if not captions_opened:
            return False
        await asyncio.sleep(0.25)
        return await self._set_visible_caption_language(page)

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
    await _click_first_visible(
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
    """Best-effort toggle-off; never a join precondition.

    The context grants no microphone/camera permission, so the page cannot
    transmit either way. A bot with no device sees NO mute control at all —
    absence is the normal permissionless state, not an error (first live
    join failed here when this raised).
    """

    if await _first_visible(page, on_selectors) is not None:
        return
    if not await _click_first_visible(page, off_selectors):
        logger.info("Google Meet shows no %s control; nothing to mute.", device)
        return
    if await _first_visible(page, on_selectors) is None:
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


async def _caption_language_control(page: Any) -> Any | None:
    control = await _first_visible(
        page,
        (
            'select[aria-label*="Language of the meeting" i]',
            'select[aria-label*="caption language" i]',
            '[role="combobox"][aria-label*="Language of the meeting" i]',
            '[role="combobox"][aria-label*="caption language" i]',
            'button[aria-label*="Language of the meeting" i]',
            'button[aria-label*="caption language" i]',
            '[aria-haspopup="listbox"][aria-label*="Language of the meeting" i]',
            '[aria-haspopup="listbox"][aria-label*="caption language" i]',
        ),
    )
    if control is not None:
        return control

    labels = page.get_by_text(
        re.compile(r"^(Language of the meeting|Caption language)$", re.IGNORECASE)
    )
    for index in range(await labels.count()):
        label = labels.nth(index)
        if not await label.is_visible():
            continue
        parent = label.locator("xpath=..")
        control = await _first_visible(
            parent,
            (
                "select",
                '[role="combobox"]',
                'button[aria-haspopup="listbox"]',
            ),
        )
        if control is not None:
            return control
    return None


async def _choose_caption_language(page: Any, control: Any, language_tag: str) -> bool:
    tag_name = str(await control.evaluate("node => node.tagName || ''")).casefold()
    if tag_name == "select":
        return await _select_native_caption_language(control, language_tag)

    await control.click()
    await asyncio.sleep(0.2)
    options = page.locator(
        '[role="option"], [role="menuitemradio"], [role="menuitem"]'
    )
    for index in range(await options.count()):
        option = options.nth(index)
        if not await option.is_visible():
            continue
        if not caption_option_matches(await option.inner_text(), language_tag):
            continue
        await option.click()
        await asyncio.sleep(0.2)
        if await _locator_matches_caption_language(control, language_tag):
            return True
        selected = page.locator(
            '[role="option"][aria-selected="true"], '
            '[role="menuitemradio"][aria-checked="true"]'
        )
        for selected_index in range(await selected.count()):
            if await _locator_matches_caption_language(
                selected.nth(selected_index),
                language_tag,
            ):
                return True
        return False
    return False


async def _select_native_caption_language(control: Any, language_tag: str) -> bool:
    normalized_tag = _normalize_language_tag(language_tag)
    options = control.locator("option")
    for index in range(await options.count()):
        option = options.nth(index)
        value = str(await option.get_attribute("value") or "")
        text = str(await option.inner_text() or "")
        if _normalize_language_tag(value) != normalized_tag and not caption_option_matches(
            text,
            language_tag,
        ):
            continue
        if value:
            await control.select_option(value=value)
        else:
            await control.select_option(label=text)
        selected = control.locator("option:checked")
        return bool(
            await selected.count()
            and await _locator_matches_caption_language(selected.first, language_tag)
        )
    return False


async def _locator_matches_caption_language(locator: Any, language_tag: str) -> bool:
    values = [
        await locator.get_attribute("value"),
        await locator.get_attribute("data-value"),
        await locator.get_attribute("aria-label"),
        await locator.text_content(),
    ]
    normalized_tag = _normalize_language_tag(language_tag)
    for raw_value in values:
        value = str(raw_value or "")
        if _normalize_language_tag(value) == normalized_tag:
            return True
        if caption_option_matches(value, language_tag):
            return True
    return False


async def _click_first_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    locator = await _first_visible(page, selectors)
    if locator is None:
        return False
    await locator.click()
    return True


async def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    return None
