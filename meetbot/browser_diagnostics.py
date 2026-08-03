"""Best-effort browser diagnostics for live UI failures."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONTROL_INVENTORY_SCRIPT = r"""
() => {
  const limit = 250;
  const selectors = [
    'button',
    '[role="button"]',
    '[role="menuitem"]',
    '[role="menuitemradio"]',
    '[role="tab"]',
    '[role="option"]',
    'select',
    'option'
  ].join(',');
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 500);
  const isVisible = (node) => {
    if (!(node instanceof Element)) return false;
    if (node.tagName === 'OPTION') {
      const select = node.closest('select');
      return Boolean(select && isVisible(select));
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  return Array.from(document.querySelectorAll(selectors))
    .filter(isVisible)
    .slice(0, limit)
    .map((node) => ({
      tag: node.tagName.toLowerCase(),
      role: node.getAttribute('role'),
      'aria-label': clean(node.getAttribute('aria-label')) || null,
      'data-tooltip': clean(node.getAttribute('data-tooltip')) || null,
      text: clean(node.textContent) || null
    }));
}
"""


async def capture_failure_evidence(
    page: Any,
    session_id: str,
    label: str,
    *,
    debug_dir: Path,
) -> None:
    """Best-effort screenshot and control inventory for a live UI failure."""

    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("Meetbot could not create its diagnostic directory", exc_info=True)
        return

    artifact_stem = f"{session_id}-{label}"
    screenshot_path = debug_dir / f"{artifact_stem}.png"
    inventory_path = debug_dir / f"{artifact_stem}.json"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Meetbot saved a failure screenshot to %s", screenshot_path)
    except Exception:
        logger.debug("Meetbot could not capture a failure screenshot", exc_info=True)

    try:
        inventory = await page.evaluate(_CONTROL_INVENTORY_SCRIPT)
        if not isinstance(inventory, list):
            inventory = []
        inventory_path.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Meetbot saved a failure control inventory to %s", inventory_path)
    except Exception:
        logger.debug("Meetbot could not capture a failure control inventory", exc_info=True)
