"""
Custom Cursor Behavior Extractor - detects and captures custom cursor implementations.

Extracts:
  - Custom cursor elements (dot, ball, follower patterns)
  - CSS properties (mix-blend-mode, pointer-events, will-change, position)
  - GSAP-driven cursor animations (quickTo, quickSetter)
  - Magnetic button effects
  - Leader/follower cursor patterns
  - Cursor trail/particle effects
  - Data-cursor attributes on hover targets

Main entry point:
    async def extract_cursor(page) -> dict
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths to injectable JS
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_CURSOR_JS = _SCRIPTS_DIR / "inject_cursor_tracker.js"


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------
async def extract_cursor(page: Page) -> dict:
    """Inject cursor tracker and return custom cursor behavior data.

    Args:
        page: Playwright Page (after navigation and page load).

    Returns:
        Dict describing custom cursor behavior, elements, blend modes,
        GSAP usage, magnetic effects, and hover states. Contains
        hasCustomCursor=False if no custom cursor detected.
    """
    js = _CURSOR_JS.read_text(encoding="utf-8")

    try:
        result: dict = await page.evaluate(js)
    except Exception as exc:
        logger.warning("Cursor extraction failed: %s", exc)
        return {"hasCustomCursor": False, "error": str(exc)}

    if not isinstance(result, dict):
        return {"hasCustomCursor": False, "error": "Non-dict result"}

    if result.get("hasCustomCursor"):
        logger.info(
            "Custom cursor detected: %d elements, blend=%s, gsap=%s, magnetic=%s, trail=%s",
            len(result.get("cursorElements", [])),
            result.get("blendMode"),
            result.get("gsapDriven"),
            result.get("magneticEffects"),
            result.get("trail"),
        )
    else:
        logger.info("No custom cursor detected")

    return result
