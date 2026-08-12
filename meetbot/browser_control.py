"""Shared low-level browser controls for Google Meet workflows."""

from __future__ import annotations

from typing import Any


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
