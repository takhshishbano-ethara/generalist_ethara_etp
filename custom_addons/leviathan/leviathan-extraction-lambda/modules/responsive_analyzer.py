"""
Phase 5: Responsive Behavior Analysis.
Tests 4 SOP-mandated breakpoints and captures per-section layout changes.
"""

import os
import asyncio
import json

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BREAKPOINTS, PRIMARY_VIEWPORT


SECTION_SELECTORS = [
    ("navigation", "nav, header, [role='navigation'], [class*='nav']:not(span):not(a), [class*='Nav']:not(span):not(a), [class*='header']:not(h1):not(h2):not(h3):not(h4):not(h5):not(h6)"),
    ("hero", "[class*='hero'], [class*='Hero'], [class*='banner'], [class*='Banner'], section:first-of-type, main > div:first-child"),
    ("content_grid", "[class*='grid'], [class*='Grid'], [class*='cards'], [class*='Cards'], [class*='gallery'], [class*='Gallery']"),
    ("main_content", "main, [class*='content'], [class*='Content'], [role='main']"),
    ("footer", "footer, [class*='footer'], [class*='Footer'], [role='contentinfo']"),
]

# CSS properties captured per element for responsive diffing
_DIFF_PROPERTIES = [
    "display", "visibility", "opacity", "width", "height",
    "flexDirection", "gridTemplateColumns", "fontSize",
    "padding", "margin", "transform", "position",
]

# Selectors used for per-element responsive diffing (common structural elements)
_ELEMENT_DIFF_SELECTORS = [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "img", "button", "a",
    "section", "nav", "header", "footer", "main",
    "div[class]",
]

_MAX_DIFF_ELEMENTS = 100


async def _safe_eval(page, expr, default=None):
    """Evaluate JS on the page, returning default if context was destroyed."""
    try:
        return await page.evaluate(expr)
    except Exception:
        return default


async def _safe_resize(page, url, width, height):
    """Resize viewport, re-navigating if the SPA triggers a route change."""
    try:
        await page.set_viewport_size({"width": width, "height": height})
        await asyncio.sleep(1.5)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
        return True
    except Exception:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.set_viewport_size({"width": width, "height": height})
            await asyncio.sleep(2)
            await _safe_eval(page, "window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
            return True
        except Exception:
            return False


async def analyze_responsive(page, output_dir: str = None) -> dict:
    """
    Analyze responsive behavior across 4 SOP breakpoints.

    Args:
        page: Playwright Page object (already navigated)
        output_dir: Optional directory to save responsive screenshots

    Returns:
        dict with breakpoints, per_section_behavior, typography_changes, behavior_changes
    """
    result = {
        "breakpoints": {},
        "per_section_behavior": {},
        "typography_changes": {},
        "behavior_changes": [],
    }

    url = page.url

    screenshots_dir = None
    if output_dir:
        screenshots_dir = os.path.join(output_dir, "screenshots", "responsive")
        os.makedirs(screenshots_dir, exist_ok=True)

    # First capture at desktop (baseline)
    await _safe_resize(page, url, PRIMARY_VIEWPORT["width"], PRIMARY_VIEWPORT["height"])

    baseline = await _capture_layout_state(page, url)

    # Analyze each breakpoint
    for bp_key, bp_config in BREAKPOINTS.items():
        width = bp_config["width"]
        height = bp_config["height"]
        label = bp_config["label"]

        ok = await _safe_resize(page, url, width, height)
        if not ok:
            continue

        state = await _capture_layout_state(page, url)

        result["breakpoints"][bp_key] = {
            "width": width,
            "height": height,
            "label": label,
        }

        # Per-section comparison with baseline (skip internal meta keys)
        for section_name in baseline["sections"]:
            if section_name.startswith("_"):
                continue
            if section_name not in result["per_section_behavior"]:
                result["per_section_behavior"][section_name] = {}

            base_section = baseline["sections"].get(section_name, {})
            curr_section = state["sections"].get(section_name, {})

            changes = _diff_section(base_section, curr_section, label)
            result["per_section_behavior"][section_name][label] = changes

        # Typography changes
        base_typo = baseline.get("typography", {})
        curr_typo = state.get("typography", {})
        typo_changes = {}
        for tag in ["h1", "h2", "h3", "p"]:
            if tag in base_typo and tag in curr_typo:
                if base_typo[tag]["fontSize"] != curr_typo[tag]["fontSize"]:
                    typo_changes[tag] = {
                        "desktop": base_typo[tag]["fontSize"],
                        "current": curr_typo[tag]["fontSize"],
                    }
        if typo_changes:
            result["typography_changes"][label] = typo_changes

        # Screenshot at this breakpoint
        if screenshots_dir:
            path = os.path.join(screenshots_dir, f"{bp_key}_{width}px.png")
            try:
                await page.screenshot(path=path, full_page=False)
            except Exception:
                pass

    # Detect behavioral changes
    result["behavior_changes"] = _detect_behavior_changes(result)

    # Per-element responsive DOM diffing (richer per-element style changes)
    try:
        result["responsive_element_diffs"] = await _capture_responsive_element_diffs(page, url)
    except Exception:
        result["responsive_element_diffs"] = {"error": "Element diff capture failed"}

    # Reset viewport
    try:
        await page.set_viewport_size(PRIMARY_VIEWPORT)
    except Exception:
        pass

    return result


async def _capture_layout_state(page, url: str = None) -> dict:
    """Capture current layout state for all detectable sections."""
    try:
        state = await page.evaluate("""
    () => {
        const result = { sections: {}, typography: {} };

        const sectionSelectors = """ + json.dumps({name: sel for name, sel in SECTION_SELECTORS}) + """;

        for (const [name, selector] of Object.entries(sectionSelectors)) {
            try {
                const el = document.querySelector(selector);
                if (!el) continue;

                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();

                result.sections[name] = {
                    visible: rect.width > 0 && rect.height > 0,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    display: style.display,
                    flexDirection: style.flexDirection,
                    gridTemplateColumns: style.gridTemplateColumns,
                    position: style.position,
                    overflow: style.overflow + ' ' + style.overflowX,
                    childCount: el.children.length,
                    // Count visible direct children
                    visibleChildCount: Array.from(el.children).filter(c => {
                        const r = c.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }).length,
                    // Check for column-like layout
                    columnsDetected: (() => {
                        const children = Array.from(el.children).filter(c => {
                            const r = c.getBoundingClientRect();
                            return r.width > 50 && r.height > 20;
                        });
                        if (children.length < 2) return 1;
                        const firstTop = children[0].getBoundingClientRect().top;
                        const sameRow = children.filter(c =>
                            Math.abs(c.getBoundingClientRect().top - firstTop) < 10
                        ).length;
                        return sameRow;
                    })(),
                    fontSize: style.fontSize,
                    padding: style.padding,
                };
            } catch (e) {}
        }

        // Typography at current viewport
        for (const tag of ['h1', 'h2', 'h3', 'p']) {
            const el = document.querySelector(tag);
            if (el) {
                const style = getComputedStyle(el);
                result.typography[tag] = {
                    fontSize: style.fontSize,
                    lineHeight: style.lineHeight,
                    fontWeight: style.fontWeight,
                };
            }
        }

        // Check for hamburger menu / mobile nav
        result.sections._nav_hamburger = !!document.querySelector(
            '[class*="hamburger"], [class*="Hamburger"], [class*="menu-toggle"], ' +
            '[class*="MenuToggle"], [aria-label*="menu"], [class*="burger"], ' +
            'button[class*="nav"], [class*="mobile-nav"], [class*="mobileNav"]'
        );

        // Check for video elements
        result.sections._has_video = !!document.querySelector('video');
        result.sections._video_autoplay = !!document.querySelector('video[autoplay]');

        return result;
    }
    """)
    except Exception:
        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                state = await page.evaluate("""
                () => {
                    const result = { sections: {}, typography: {} };
                    for (const tag of ['h1', 'h2', 'h3', 'p']) {
                        const el = document.querySelector(tag);
                        if (el) {
                            const style = getComputedStyle(el);
                            result.typography[tag] = {
                                fontSize: style.fontSize,
                                lineHeight: style.lineHeight,
                                fontWeight: style.fontWeight,
                            };
                        }
                    }
                    return result;
                }
                """)
            except Exception:
                state = {"sections": {}, "typography": {}}
        else:
            state = {"sections": {}, "typography": {}}

    return state


def _diff_section(base, current, breakpoint_label):
    """Compare a section between desktop and current breakpoint."""
    if not isinstance(base, dict) or not isinstance(current, dict):
        return {"visible": False, "columns": 1, "width": 0, "height": 0, "changes": ["section data unavailable"]}

    changes = []

    # Visibility change
    if base.get("visible") and not current.get("visible"):
        changes.append("hidden at this breakpoint")

    # Column collapse
    base_cols = base.get("columnsDetected", 1)
    curr_cols = current.get("columnsDetected", 1)
    if base_cols != curr_cols:
        changes.append(f"columns: {base_cols} → {curr_cols}")

    # Width change
    base_w = base.get("width", 0)
    curr_w = current.get("width", 0)
    if base_w > 0 and curr_w > 0:
        ratio = curr_w / base_w
        if ratio < 0.7:
            changes.append(f"width: {base_w}px → {curr_w}px")

    # Display change
    if base.get("display") != current.get("display"):
        changes.append(f"display: {base.get('display')} → {current.get('display')}")

    # Flex direction change
    if base.get("flexDirection") != current.get("flexDirection"):
        if current.get("flexDirection") == "column":
            changes.append("layout stacked (flex-direction: column)")

    # Grid change
    if base.get("gridTemplateColumns") != current.get("gridTemplateColumns"):
        changes.append(f"grid: {current.get('gridTemplateColumns', 'changed')}")

    # Font size change — filter sub-pixel noise (< 0.5px difference)
    base_fs = base.get("fontSize", "")
    curr_fs = current.get("fontSize", "")
    if base_fs != curr_fs:
        try:
            base_val = float(str(base_fs).replace("px", ""))
            curr_val = float(str(curr_fs).replace("px", ""))
            if abs(base_val - curr_val) >= 0.5:
                changes.append(f"fontSize: {base_fs} → {curr_fs}")
        except (ValueError, TypeError):
            if base_fs and curr_fs:
                changes.append(f"fontSize: {base_fs} → {curr_fs}")

    return {
        "visible": current.get("visible", False),
        "columns": curr_cols,
        "width": current.get("width"),
        "height": current.get("height"),
        "changes": changes,
    }


async def _capture_element_styles_at_breakpoint(page) -> list[dict]:
    """
    Capture key CSS properties for elements matching common selectors.
    Returns up to _MAX_DIFF_ELEMENTS entries with selector, tag, classes, and styles.
    """
    props_json = json.dumps(_DIFF_PROPERTIES)
    selectors_json = json.dumps(_ELEMENT_DIFF_SELECTORS)
    max_elements = _MAX_DIFF_ELEMENTS

    try:
        elements = await page.evaluate(f"""
            () => {{
                const props = {props_json};
                const selectors = {selectors_json};
                const maxElements = {max_elements};
                const results = [];
                const seen = new Set();

                for (const sel of selectors) {{
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {{
                        if (results.length >= maxElements) break;
                        if (seen.has(el)) continue;
                        seen.add(el);

                        const rect = el.getBoundingClientRect();
                        if (rect.width < 1 && rect.height < 1) continue;

                        const cs = getComputedStyle(el);
                        const styles = {{}};
                        for (const prop of props) {{
                            styles[prop] = cs[prop] || '';
                        }}

                        // Build a stable identifier for this element
                        const tag = el.tagName.toLowerCase();
                        let identifier = tag;
                        if (el.id) {{
                            identifier = tag + '#' + el.id;
                        }} else if (el.classList.length > 0) {{
                            identifier = tag + '.' + Array.from(el.classList).slice(0, 3).join('.');
                        }}

                        // Disambiguate with index among siblings
                        const parent = el.parentElement;
                        if (parent && !el.id) {{
                            const siblings = parent.querySelectorAll(':scope > ' + tag);
                            if (siblings.length > 1) {{
                                const idx = Array.from(siblings).indexOf(el);
                                identifier += ':nth(' + idx + ')';
                            }}
                        }}

                        results.push({{
                            selector: identifier,
                            tag: tag,
                            classes: Array.from(el.classList).slice(0, 5),
                            styles: styles,
                        }});
                    }}
                    if (results.length >= maxElements) break;
                }}

                return results;
            }}
        """)
        return elements or []
    except Exception:
        return []


def _diff_element_styles(desktop_elements: list[dict], bp_elements: list[dict]) -> dict:
    """
    Diff per-element styles between desktop baseline and a breakpoint.
    Returns dict keyed by element selector with changed properties.
    """
    # Index desktop elements by selector for fast lookup
    desktop_map = {}
    for el in desktop_elements:
        sel = el.get("selector", "")
        if sel:
            desktop_map[sel] = el

    # Index breakpoint elements
    bp_map = {}
    for el in bp_elements:
        sel = el.get("selector", "")
        if sel:
            bp_map[sel] = el

    diffs = {}

    # Find modified and hidden elements
    for sel, desktop_el in desktop_map.items():
        if sel not in bp_map:
            diffs[sel] = {
                "change": "hidden",
                "tag": desktop_el.get("tag"),
                "classes": desktop_el.get("classes", []),
            }
            continue

        bp_el = bp_map[sel]
        desktop_styles = desktop_el.get("styles", {})
        bp_styles = bp_el.get("styles", {})

        style_changes = {}
        for prop in _DIFF_PROPERTIES:
            dv = desktop_styles.get(prop, "")
            bv = bp_styles.get(prop, "")
            if dv != bv:
                style_changes[prop] = {"desktop": dv, "breakpoint": bv}

        if style_changes:
            diffs[sel] = {
                "change": "modified",
                "tag": bp_el.get("tag"),
                "classes": bp_el.get("classes", []),
                "style_changes": style_changes,
            }

    # Find newly appeared elements
    for sel, bp_el in bp_map.items():
        if sel not in desktop_map:
            diffs[sel] = {
                "change": "appeared",
                "tag": bp_el.get("tag"),
                "classes": bp_el.get("classes", []),
                "styles": bp_el.get("styles", {}),
            }

    return diffs


async def _capture_responsive_element_diffs(page, url: str) -> dict:
    """
    Capture per-element style diffs across all 4 breakpoints compared to
    the 1440px desktop baseline. Returns a dict keyed by breakpoint label.
    """
    element_diffs = {}

    # Capture desktop baseline
    await _safe_resize(page, url, PRIMARY_VIEWPORT["width"], PRIMARY_VIEWPORT["height"])
    desktop_elements = await _capture_element_styles_at_breakpoint(page)

    if not desktop_elements:
        return {"error": "No elements captured at desktop baseline"}

    # Capture at each breakpoint and diff
    for bp_key, bp_config in BREAKPOINTS.items():
        width = bp_config["width"]
        height = bp_config["height"]
        label = bp_config["label"]

        ok = await _safe_resize(page, url, width, height)
        if not ok:
            element_diffs[label] = {"error": f"Could not resize to {width}px"}
            continue

        bp_elements = await _capture_element_styles_at_breakpoint(page)
        diffs = _diff_element_styles(desktop_elements, bp_elements)

        # Summarize
        modified = sum(1 for v in diffs.values() if v.get("change") == "modified")
        hidden = sum(1 for v in diffs.values() if v.get("change") == "hidden")
        appeared = sum(1 for v in diffs.values() if v.get("change") == "appeared")

        element_diffs[label] = {
            "summary": {
                "total_elements_desktop": len(desktop_elements),
                "total_elements_breakpoint": len(bp_elements),
                "modified": modified,
                "hidden": hidden,
                "appeared": appeared,
            },
            "elements": diffs,
        }

    # Reset viewport
    try:
        await page.set_viewport_size(PRIMARY_VIEWPORT)
    except Exception:
        pass

    return element_diffs


def _detect_behavior_changes(result):
    """Detect high-level behavioral changes across breakpoints."""
    changes = []

    for section, breakpoints in result["per_section_behavior"].items():
        for bp_label, data in breakpoints.items():
            for change in data.get("changes", []):
                changes.append({
                    "section": section,
                    "breakpoint": bp_label,
                    "change": change,
                })

    return changes
