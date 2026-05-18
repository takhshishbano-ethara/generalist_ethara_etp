"""
Phase 4D: Authentication & Form Detection.

Injects JS to detect login forms, OAuth providers, cookie consent,
auth SDKs, and protected route indicators.
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_auth_detector.js"


async def extract_auth(page: Page) -> dict:
    """Detect authentication patterns on the current page."""
    js = _JS_PATH.read_text(encoding="utf-8")

    try:
        raw = await page.evaluate(js)
    except Exception as exc:
        logger.warning("Auth detection failed: %s", exc)
        return {"has_auth": False, "error": str(exc)}

    if not isinstance(raw, dict):
        return {"has_auth": False}

    logger.info(
        "Auth detection: has_auth=%s, forms=%d, oauth=%s, consent=%s",
        raw.get("has_auth"),
        len(raw.get("login_forms", [])),
        raw.get("oauth_providers", []),
        bool(raw.get("cookie_consent")),
    )

    return raw
