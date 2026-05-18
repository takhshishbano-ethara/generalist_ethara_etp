"""
Phase 3B: Dark Mode Palette Extraction.

Emulates prefers-color-scheme: dark and toggles common dark-mode class/attribute
patterns to detect and extract dark-mode color palettes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_COLOR_EXTRACT_JS = """
(() => {
  const result = { colors: {}, cssVars: {} };

  // Sample key elements for background and text colors
  const selectors = [
    'body', 'html', 'main', 'header', 'nav', 'footer',
    'h1', 'h2', 'h3', 'p', 'a', 'button',
    'section', '[class*="hero"]', '[class*="card"]',
  ];

  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (!el) continue;
      const style = getComputedStyle(el);
      const bg = style.backgroundColor;
      const color = style.color;
      const key = sel.replace(/[^a-zA-Z0-9]/g, '_');
      result.colors[key] = { backgroundColor: bg, color: color };
    } catch (e) {}
  }

  // Extract CSS custom properties from :root / html
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        const sel = rule.selectorText || '';
        if (sel === ':root' || sel === 'html' || sel === 'body') {
          const text = rule.cssText;
          const vars = [...text.matchAll(/(--[\\w-]+)\\s*:\\s*([^;]+)/g)];
          for (const m of vars) {
            result.cssVars[m[1].trim()] = m[2].trim();
          }
        }
      }
    } catch (e) {}
  }

  return result;
})()
"""

_TOGGLE_DARK_JS = """
(method) => {
  const html = document.documentElement;
  const body = document.body;

  if (method === 'data-theme') {
    html.setAttribute('data-theme', 'dark');
    body?.setAttribute('data-theme', 'dark');
  } else if (method === 'class') {
    html.classList.add('dark');
    body?.classList.add('dark');
  } else if (method === 'data-mode') {
    html.setAttribute('data-mode', 'dark');
    body?.setAttribute('data-mode', 'dark');
  } else if (method === 'color-scheme') {
    html.style.colorScheme = 'dark';
  }
}
"""

_RESET_DARK_JS = """
(method) => {
  const html = document.documentElement;
  const body = document.body;

  if (method === 'data-theme') {
    html.removeAttribute('data-theme');
    body?.removeAttribute('data-theme');
  } else if (method === 'class') {
    html.classList.remove('dark');
    body?.classList.remove('dark');
  } else if (method === 'data-mode') {
    html.removeAttribute('data-mode');
    body?.removeAttribute('data-mode');
  } else if (method === 'color-scheme') {
    html.style.colorScheme = '';
  }
}
"""


async def extract_dark_mode(page: Page) -> dict:
    """
    Detect and extract dark mode colors.

    Returns dict with keys:
        has_dark_mode, detection_method, light_colors, dark_colors,
        css_variable_overrides
    """
    light = await page.evaluate(_COLOR_EXTRACT_JS)
    light_colors = light.get("colors", {})
    light_vars = light.get("cssVars", {})

    best_result = None
    best_method = None
    best_change_count = 0

    methods = [
        ("prefers-color-scheme", _try_media_emulation),
        ("data-theme", _try_toggle("data-theme")),
        ("class", _try_toggle("class")),
        ("data-mode", _try_toggle("data-mode")),
    ]

    for method_name, try_fn in methods:
        dark, change_count = await try_fn(page, light_colors)
        if change_count > best_change_count:
            best_change_count = change_count
            best_result = dark
            best_method = method_name

    if best_change_count < 3:
        return {"has_dark_mode": False}

    dark_colors = best_result.get("colors", {})
    dark_vars = best_result.get("cssVars", {})

    var_overrides = {}
    for var_name, dark_val in dark_vars.items():
        light_val = light_vars.get(var_name)
        if light_val and light_val != dark_val:
            var_overrides[var_name] = {"light": light_val, "dark": dark_val}

    return {
        "has_dark_mode": True,
        "detection_method": best_method,
        "light_colors": light_colors,
        "dark_colors": dark_colors,
        "css_variable_overrides": var_overrides,
    }


async def _try_media_emulation(page: Page, light_colors: dict) -> tuple[dict, int]:
    try:
        await page.emulate_media(color_scheme="dark")
        await page.wait_for_timeout(500)
        dark = await page.evaluate(_COLOR_EXTRACT_JS)
        change_count = _count_changes(light_colors, dark.get("colors", {}))
        await page.emulate_media(color_scheme=None)
        await page.wait_for_timeout(300)
        return dark, change_count
    except Exception:
        try:
            await page.emulate_media(color_scheme="light")
        except Exception:
            pass
        return {}, 0


def _try_toggle(method: str):
    async def _inner(page: Page, light_colors: dict) -> tuple[dict, int]:
        try:
            await page.evaluate(_TOGGLE_DARK_JS, method)
            await page.wait_for_timeout(500)
            dark = await page.evaluate(_COLOR_EXTRACT_JS)
            change_count = _count_changes(light_colors, dark.get("colors", {}))
            await page.evaluate(_RESET_DARK_JS, method)
            await page.wait_for_timeout(300)
            return dark, change_count
        except Exception:
            try:
                await page.evaluate(_RESET_DARK_JS, method)
            except Exception:
                pass
            return {}, 0
    return _inner


def _count_changes(light: dict, dark: dict) -> int:
    count = 0
    for key in light:
        if key not in dark:
            continue
        l_bg = light[key].get("backgroundColor", "")
        d_bg = dark[key].get("backgroundColor", "")
        l_c = light[key].get("color", "")
        d_c = dark[key].get("color", "")
        if l_bg != d_bg:
            count += 1
        if l_c != d_c:
            count += 1
    return count
