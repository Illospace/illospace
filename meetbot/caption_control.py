"""Browser-side caption controls for Google Meet."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from meetbot.browser_control import click_first_match, first_visible
from meetbot.browser_diagnostics import capture_failure_evidence
from meetbot.config import MeetbotConfig
from meetbot.models import SessionEvents

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


class CaptionController:
    """Observe captions and drive Google Meet caption controls."""

    def __init__(self, config: MeetbotConfig) -> None:
        self._config = config

    async def start(self, page: Any, events: SessionEvents, session_id: str) -> None:
        await self._attach_caption_observer(page, events)
        await self._enable_captions(page, events, session_id)

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
        enabled = await first_visible(
            page,
            (
                'button[aria-label*="Turn off captions" i]',
                '[role="button"][data-tooltip*="Turn off captions" i]',
            ),
        )
        if enabled is None:
            clicked = await click_first_match(
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
        opened = await click_first_match(
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
        more_opened = await click_first_match(
            page,
            (
                'button[aria-label="More options" i]',
                '[role="button"][data-tooltip="More options" i]',
            ),
        )
        if not more_opened:
            return False
        await asyncio.sleep(0.25)
        settings_opened = await click_first_match(
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
        captions_opened = await click_first_match(
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


async def _caption_language_control(page: Any) -> Any | None:
    control = await first_visible(
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
        control = await first_visible(
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
