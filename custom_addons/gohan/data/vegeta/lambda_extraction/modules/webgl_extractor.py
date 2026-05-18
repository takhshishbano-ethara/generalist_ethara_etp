"""
Phase 3B: WebGL / Three.js Scene Extractor.

For sites classified as '3D & WebGL / Game', extracts Three.js renderer config,
scene features, detected shaders, 3D assets, and canvas properties.
Falls back gracefully for non-3D sites.
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_webgl_extractor.js"


async def extract_webgl(page: Page) -> dict:
    """
    Inject the WebGL extractor and return structured 3D scene data.
    Safe to call on non-3D sites — returns {detected: False}.
    """
    js = _JS_PATH.read_text(encoding="utf-8")

    try:
        raw = await page.evaluate(js)
    except Exception as exc:
        logger.warning("WebGL extraction failed: %s", exc)
        return {"detected": False}

    if not isinstance(raw, dict) or not raw.get("detected"):
        return {"detected": False}

    result = {
        "detected": True,
        "canvases": raw.get("canvases", []),
        "webgl_info": raw.get("webgl_info"),
        "three_js": _process_three_js(raw.get("three_js")),
        "shaders": raw.get("shaders", []),
        "detected_3d_assets": raw.get("detected3DAssets", {}),
        "r3f_likely": raw.get("r3f_likely", False),
    }

    logger.info(
        "WebGL extraction: %d canvases, Three.js r%s, %d code hints, %d shaders",
        len(result["canvases"]),
        (result["three_js"] or {}).get("revision", "N/A"),
        len((result["three_js"] or {}).get("code_hints", [])),
        len(result["shaders"]),
    )

    return result


def _process_three_js(raw: dict | None) -> dict | None:
    if not raw:
        return None

    return {
        "revision": raw.get("revision", "unknown"),
        "renderer": raw.get("renderer"),
        "controls": raw.get("controls"),
        "physics": raw.get("physics"),
        "post_processing": raw.get("postProcessing", []),
        "loaders": raw.get("loaders", []),
        "code_hints": raw.get("codeHints", []),
    }
