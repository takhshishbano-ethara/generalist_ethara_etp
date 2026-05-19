"""
Phase 3A: Brand / Logo / Site Name Extraction.

Injects inject_brand_extractor.js to detect logos via multi-signal scoring,
extract favicons, and determine the site name.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_brand_extractor.js"


async def extract_brand(page: Page) -> dict:
    """
    Extract logo, favicon, and site name from the current page.

    Returns dict with keys:
        site_name, logo, logo_candidates, favicons, theme_color
    """
    js_code = _JS_PATH.read_text(encoding="utf-8")
    raw: dict = await page.evaluate(js_code)

    candidates = raw.get("logo_candidates", [])
    for c in candidates:
        if c.get("src") and c["src"] not in ("inline-svg", ""):
            try:
                c["src"] = _resolve_url(page.url, c["src"])
            except Exception:
                pass

    favicons = raw.get("favicons", [])
    for f in favicons:
        if f.get("href"):
            try:
                f["href"] = _resolve_url(page.url, f["href"])
            except Exception:
                pass

    best_logo = _pick_best_logo(candidates)

    return {
        "site_name": raw.get("site_name", ""),
        "logo": best_logo,
        "logo_candidates": candidates,
        "favicons": favicons,
        "theme_color": raw.get("theme_color"),
    }


def _pick_best_logo(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    best = candidates[0]
    if best.get("score", 0) < 2:
        return None
    return {
        "src": best["src"],
        "type": best.get("type", "img"),
        "alt": best.get("alt"),
        "width": best.get("width"),
        "height": best.get("height"),
    }


def _resolve_url(base_url: str, url: str) -> str:
    if url.startswith(("http://", "https://", "data:", "//")):
        return url
    parsed = urlparse(base_url)
    if url.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{url}"
    base_path = parsed.path.rsplit("/", 1)[0] if "/" in parsed.path else ""
    return f"{parsed.scheme}://{parsed.netloc}{base_path}/{url}"
