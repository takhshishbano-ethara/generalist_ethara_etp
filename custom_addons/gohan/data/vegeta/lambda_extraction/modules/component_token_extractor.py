"""
Phase 3C: Component Design Token Extraction.

Extracts baseline styles and state variants (hover, focus, active, disabled)
for interactive UI components: buttons, inputs, links, badges.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_component_tokens.js"

_HEX_RE = re.compile(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)")


async def extract_component_tokens(page: Page) -> dict:
    """
    Extract component-level design tokens from the current page.

    Returns dict with keys: buttons, inputs, links, badges
    Each value is a list of variant dicts with {selector, sampleText, base, states}.
    """
    js_code = _JS_PATH.read_text(encoding="utf-8")
    raw: dict = await page.evaluate(js_code)

    result = {}
    for component_type in ("buttons", "inputs", "links", "badges"):
        variants = raw.get(component_type, [])
        for variant in variants:
            _normalize_colors(variant.get("base", {}))
            for state_props in variant.get("states", {}).values():
                _normalize_colors(state_props)
        result[component_type] = variants

    return result


def _normalize_colors(props: dict) -> None:
    """Convert rgb()/rgba() color values to hex in-place."""
    color_props = (
        "backgroundColor", "color", "borderColor",
        "outlineColor", "boxShadow",
    )
    for prop in color_props:
        val = props.get(prop)
        if val and val.startswith("rgb"):
            hex_val = _rgb_to_hex(val)
            if hex_val:
                props[prop] = hex_val


def _rgb_to_hex(rgb_str: str) -> str | None:
    m = _HEX_RE.match(rgb_str)
    if not m:
        return None
    r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"#{r:02x}{g:02x}{b:02x}"
