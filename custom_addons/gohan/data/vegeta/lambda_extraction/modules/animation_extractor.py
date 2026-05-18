"""
Phase 3: Animation Extractor

Extracts ALL animation data from a page using Playwright:
  - CSS animations, GSAP timelines, ScrollTrigger configs, Lenis, Lottie, keyframes
  - Scroll-triggered animation map (scrolling in 5% increments, diffing new animations)
  - Hover/click/focus micro-interaction states for interactive elements
  - Structured output with global motion rules and per-component interaction tables

Main entry point:
    async def extract_animations(page) -> dict
"""

import asyncio
import json
import logging
from pathlib import Path
from copy import deepcopy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths to injectable JS
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_ANIMATION_CAPTURE_JS = _SCRIPTS_DIR / "inject_animation_capture.js"
_INTERACTION_STATES_JS = _SCRIPTS_DIR / "inject_interaction_states.js"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SCROLL_STEP_PERCENT = 5  # scroll in 5% increments
_MAX_INTERACTIVE_ELEMENTS = 50
_HOVER_SETTLE_MS = 800
_EVALUATE_TIMEOUT_MS = 10_000
_SCROLL_SETTLE_MS = 350  # wait after each scroll for animations to fire

# Selectors for interactive elements, ordered by priority
_INTERACTIVE_SELECTORS = [
    'a.cta, a.btn, a.button, [class*="cta"], [class*="btn"]:not(input)',
    'button',
    '[role="button"]',
    '[class*="Btn"], [class*="button"], [class*="Button"]',
    '[class*="link"], [class*="Link"]',
    'nav a',
    '[class*="card"], [class*="Card"]',
    '[class*="trigger"], [class*="trg"]',
    'a[href]',
    'input[type="submit"], input[type="button"]',
    '[tabindex="0"]',
]


# ---------------------------------------------------------------------------
# Helpers — JS file loading
# ---------------------------------------------------------------------------
def _load_js(path: Path) -> str:
    """Read a JS file from disk. Cached on first call via simple closure."""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Initial animation capture
# ---------------------------------------------------------------------------
async def _capture_animations(page) -> dict:
    """Inject the animation capture script and return its result dict."""
    js = _load_js(_ANIMATION_CAPTURE_JS)
    try:
        result = await asyncio.wait_for(
            page.evaluate(js),
            timeout=_EVALUATE_TIMEOUT_MS / 1000,
        )
        return result if isinstance(result, dict) else {}
    except asyncio.TimeoutError:
        logger.warning("Animation capture timed out")
        return {}
    except Exception as exc:
        logger.warning("Animation capture failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Diffing helpers
# ---------------------------------------------------------------------------
def _animation_signature(anim: dict) -> str:
    """Create a hashable signature for an animation entry so we can detect new ones."""
    target = anim.get("target") or {}
    parts = [
        anim.get("type", ""),
        anim.get("animationName", anim.get("transitionProperty", anim.get("id", ""))),
        target.get("tag", ""),
        target.get("id", ""),
        ",".join(target.get("classes", [])),
        str(anim.get("duration", "")),
    ]
    return "|".join(str(p) if p is not None else "" for p in parts)


def _all_animations_from_capture(capture: dict) -> list[dict]:
    """Flatten all animation lists from a capture dict into one list."""
    anims = []
    for key in ("css_animations", "css_transitions", "web_animations", "gsap_tweens"):
        anims.extend(capture.get(key, []))
    return anims


def _diff_animations(old_sigs: set, new_capture: dict) -> list[dict]:
    """Return only animations whose signature is NOT in old_sigs."""
    new_anims = []
    for anim in _all_animations_from_capture(new_capture):
        sig = _animation_signature(anim)
        if sig not in old_sigs:
            new_anims.append(anim)
    return new_anims


def _sigs_from_capture(capture: dict) -> set:
    return {_animation_signature(a) for a in _all_animations_from_capture(capture)}


# ---------------------------------------------------------------------------
# 2. Scroll-triggered animation capture
# ---------------------------------------------------------------------------
async def _detect_scroll_hijack(page) -> dict:
    """Detect if Lenis or similar scroll-hijacking is active."""
    try:
        return await page.evaluate("""
            () => ({
                hasLenis: !!(window.__lenis || window.Lenis ||
                    document.documentElement.classList.contains('lenis') ||
                    document.documentElement.classList.contains('lenis-smooth')),
                hasLocomotive: !!document.querySelector('[data-scroll-container]'),
                hasSmoothScrollbar: !!window.Scrollbar,
                hasGsapScrub: !!(window.ScrollTrigger &&
                    window.ScrollTrigger.getAll &&
                    window.ScrollTrigger.getAll().some(st => st.vars && st.vars.scrub)),
            })
        """)
    except Exception:
        return {}


async def _scroll_with_wheel(page, target_y: int, current_y: int):
    """Scroll using WheelEvent dispatch for Lenis/scroll-hijacked sites."""
    delta = target_y - current_y
    if abs(delta) < 10:
        return
    # Dispatch in chunks to simulate real scrolling
    chunk_size = min(200, abs(delta))
    steps = max(1, abs(delta) // chunk_size)
    direction = 1 if delta > 0 else -1

    for _ in range(steps):
        await page.evaluate(f"""
            () => window.dispatchEvent(new WheelEvent('wheel', {{
                deltaY: {direction * chunk_size}, bubbles: true, cancelable: true
            }}))
        """)
        await page.wait_for_timeout(80)

    # Wait for Lenis to animate to target position
    await page.wait_for_timeout(400)


async def _capture_scroll_triggers_progress(page) -> list:
    """Read ScrollTrigger progress at current scroll position."""
    try:
        return await page.evaluate("""
            () => {
                if (!window.ScrollTrigger || !window.ScrollTrigger.getAll) return [];
                return window.ScrollTrigger.getAll().map(st => ({
                    id: st.vars?.id || null,
                    progress: st.progress,
                    isActive: st.isActive,
                    start: st.start,
                    end: st.end,
                    scrub: st.vars?.scrub || false,
                    pin: !!st.vars?.pin,
                    trigger_classes: st.trigger?.classList ?
                        Array.from(st.trigger.classList).slice(0, 3) : [],
                }));
            }
        """) or []
    except Exception:
        return []


async def _capture_scroll_animations(page) -> dict:
    """
    Scroll from 0% to 100% in _SCROLL_STEP_PERCENT increments.
    At each position, re-inject the capture script, diff with the cumulative
    set of known animations, and record newly-appeared ones keyed by scroll %.

    Uses WheelEvent dispatch on Lenis/scroll-hijacked sites to properly
    trigger scrub-based GSAP animations.
    """
    scroll_map: dict[str, list[dict]] = {}
    seen_sigs: set[str] = set()

    try:
        dims = await page.evaluate(
            "() => ({ scrollHeight: document.documentElement.scrollHeight, "
            "innerHeight: window.innerHeight })"
        )
        scroll_height = dims.get("scrollHeight", 0)
        inner_height = dims.get("innerHeight", 0)
        max_scroll = max(scroll_height - inner_height, 1)
    except Exception as exc:
        logger.warning("Could not read page dimensions: %s", exc)
        return {}

    # Detect scroll hijacking
    scroll_info = await _detect_scroll_hijack(page)
    use_wheel = scroll_info.get("hasLenis") or scroll_info.get("hasLocomotive") or scroll_info.get("hasSmoothScrollbar")
    has_scrub = scroll_info.get("hasGsapScrub")

    if use_wheel:
        logger.info("  [2/4] Lenis/scroll-hijack detected — using WheelEvent dispatch")
    if has_scrub:
        logger.info("  [2/4] GSAP ScrollTrigger scrub detected — capturing progress")

    # Capture initial state at scroll 0
    initial_capture = await _capture_animations(page)
    seen_sigs = _sigs_from_capture(initial_capture)

    steps = list(range(0, 101, _SCROLL_STEP_PERCENT))
    if steps[-1] != 100:
        steps.append(100)

    current_y = 0
    scroll_trigger_snapshots = []

    for pct in steps:
        scroll_y = int((pct / 100) * max_scroll)
        try:
            if use_wheel:
                await _scroll_with_wheel(page, scroll_y, current_y)
                current_y = scroll_y
            else:
                await page.evaluate(f"window.scrollTo({{ top: {scroll_y}, behavior: 'instant' }})")
                await page.wait_for_timeout(_SCROLL_SETTLE_MS)
        except Exception as exc:
            logger.debug("Scroll to %d%% failed: %s", pct, exc)
            continue

        capture = await _capture_animations(page)
        new_anims = _diff_animations(seen_sigs, capture)

        if new_anims:
            key = f"{pct}%"
            scroll_map[key] = new_anims
            seen_sigs |= {_animation_signature(a) for a in new_anims}

        # Capture ScrollTrigger progress at key positions
        if has_scrub and pct % 10 == 0:
            st_progress = await _capture_scroll_triggers_progress(page)
            if st_progress:
                scroll_trigger_snapshots.append({"scroll_pct": pct, "triggers": st_progress})

    # Store ScrollTrigger progress data in special key
    if scroll_trigger_snapshots:
        scroll_map["_scroll_trigger_progress"] = scroll_trigger_snapshots

    # Scroll back to top
    try:
        if use_wheel:
            await page.evaluate("window.__lenis?.scrollTo(0, {immediate: true}); window.scrollTo({top: 0, behavior: 'instant'})")
        else:
            await page.evaluate("window.scrollTo({ top: 0, behavior: 'instant' })")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    return scroll_map


# ---------------------------------------------------------------------------
# 3. Hover / click / focus state capture
# ---------------------------------------------------------------------------
async def _collect_interactive_selectors(page) -> list[str]:
    """
    Query the page for interactive elements using our priority-ordered selector
    list. Return up to _MAX_INTERACTIVE_ELEMENTS unique CSS selectors.
    """
    collected: list[str] = []
    seen_ids: set[str] = set()

    for group_selector in _INTERACTIVE_SELECTORS:
        if len(collected) >= _MAX_INTERACTIVE_ELEMENTS:
            break
        try:
            handles = await page.query_selector_all(group_selector)
        except Exception:
            continue

        for handle in handles:
            if len(collected) >= _MAX_INTERACTIVE_ELEMENTS:
                break
            try:
                # Build a unique selector for this element
                info = await handle.evaluate("""el => {
                    const tag = el.tagName.toLowerCase();
                    if (el.id) return '#' + CSS.escape(el.id);
                    // Build a class-based selector
                    const classes = Array.from(el.classList).slice(0, 3).map(c => '.' + CSS.escape(c)).join('');
                    const base = tag + classes;
                    // Disambiguate by nth-of-type if needed
                    const parent = el.parentElement;
                    if (parent) {
                        const siblings = Array.from(parent.querySelectorAll(':scope > ' + base));
                        const idx = siblings.indexOf(el);
                        if (siblings.length > 1 && idx >= 0) {
                            return base + ':nth-of-type(' + (idx + 1) + ')';
                        }
                    }
                    return base;
                }""")
                if info and info not in seen_ids:
                    seen_ids.add(info)
                    collected.append(info)
            except Exception:
                continue

    return collected


def _style_diff(before: dict, after: dict) -> dict:
    """Return only the properties that changed between before and after styles."""
    changes = {}
    for prop in before:
        val_before = before.get(prop)
        val_after = after.get(prop)
        if val_before != val_after:
            changes[prop] = {"from": val_before, "to": val_after}
    return changes


def _parse_transition_info(default_state: dict) -> dict:
    """Extract duration and easing from transition-related computed styles."""
    raw_duration = default_state.get("transitionDuration", "0s")
    raw_easing = default_state.get("transitionTimingFunction", "ease")
    raw_property = default_state.get("transitionProperty", "all")

    # Parse durations — may be comma-separated
    durations = []
    for part in raw_duration.split(","):
        part = part.strip().lower()
        if part.endswith("ms"):
            try:
                durations.append(float(part[:-2]))
            except ValueError:
                pass
        elif part.endswith("s"):
            try:
                durations.append(float(part[:-1]) * 1000)
            except ValueError:
                pass

    duration_ms = max(durations) if durations else 0

    return {
        "transition_duration_ms": duration_ms,
        "transition_easing": raw_easing.split(",")[0].strip(),
        "transition_property": raw_property,
    }


async def _capture_hover_states(page) -> list[dict]:
    """
    For each interactive element, capture hover state using three-strategy fallthrough:
      1. Real browser hover: scrollIntoView + mouse.move to element center
      2. DOM dispatchEvent: mouseenter/pointerenter + force_hover class
      3. CSS :hover rule extraction from stylesheets (always captured as reference)

    If strategy 1 produces no diff, falls through to strategy 2.
    Strategy 3 (CSS rules) is always collected and merged for elements
    that neither strategy 1 nor 2 captured.

    On Lenis/scroll-hijacked sites, disables Lenis/smooth scroll before
    hovering to prevent timeout conflicts and scroll interference.
    """
    interaction_js = _load_js(_INTERACTION_STATES_JS)
    selectors = await _collect_interactive_selectors(page)
    results: list[dict] = []

    # Disable Lenis and all smooth-scroll for hover capture
    lenis_was_active = False
    try:
        lenis_was_active = await page.evaluate("""
            () => {
                let active = false;
                try { if (window.lenis) { window.lenis.stop(); active = true; } } catch (e) {}
                try { if (window.__lenis) { window.__lenis.stop(); active = true; } } catch (e) {}
                try {
                    if (!active) {
                        active = !!(document.documentElement.classList.contains('lenis') ||
                                    document.documentElement.classList.contains('lenis-smooth'));
                    }
                } catch (e) {}
                if (active) {
                    document.documentElement.classList.add('lenis-stopped');
                }
                document.documentElement.style.scrollBehavior = 'auto';
                document.body.style.scrollBehavior = 'auto';
                return active;
            }
        """)
    except Exception:
        pass

    for selector in selectors:
        entry = {
            "selector": selector,
            "hover": None,
            "focus": None,
            "transition": None,
            "_hover_method": None,
        }

        try:
            # --- Capture default state ---
            default_data = await asyncio.wait_for(
                page.evaluate(f"({interaction_js})('{_escape_js_string(selector)}')"),
                timeout=_EVALUATE_TIMEOUT_MS / 1000,
            )
            if not default_data or not isinstance(default_data, dict):
                continue

            default_styles = default_data.get("default_state", {})
            entry["tag"] = default_data.get("tag")
            entry["text"] = default_data.get("text")
            entry["id"] = default_data.get("id")
            entry["classes"] = default_data.get("classes")

            # Parse transition info from default state
            entry["transition"] = _parse_transition_info(default_styles)

            # --- Hover: Three-strategy fallthrough ---
            hover_diff = None
            hover_method = None

            try:
                el_handle = await page.query_selector(selector)
                if el_handle:
                    # STRATEGY 1: scrollIntoView + mouse.move (real browser hover)
                    try:
                        await el_handle.scroll_into_view_if_needed(timeout=3000)
                        await page.wait_for_timeout(150)

                        # Get element's viewport-relative bounding rect
                        rect = await el_handle.bounding_box()
                        if rect and rect["width"] >= 5 and rect["height"] >= 5:
                            cx = rect["x"] + rect["width"] / 2
                            cy = rect["y"] + rect["height"] / 2
                            # Clamp to viewport bounds
                            cx = max(1, min(cx, 1440 - 1))
                            cy = max(1, min(cy, 900 - 1))
                            await page.mouse.move(cx, cy)
                            await page.wait_for_timeout(_HOVER_SETTLE_MS)

                            # Re-capture styles after hover
                            hover_data = await asyncio.wait_for(
                                page.evaluate(
                                    f"({interaction_js})('{_escape_js_string(selector)}')"
                                ),
                                timeout=_EVALUATE_TIMEOUT_MS / 1000,
                            )
                            if hover_data and isinstance(hover_data, dict):
                                hover_styles = hover_data.get("default_state", {})
                                diff = _style_diff(default_styles, hover_styles)
                                if diff:
                                    hover_diff = diff
                                    hover_method = "mouse_move"
                    except Exception as exc:
                        logger.debug("Strategy 1 (mouse.move) failed for %s: %s", selector, exc)

                    # STRATEGY 2: DOM dispatchEvent + force class (if strategy 1 had no diff)
                    if hover_diff is None:
                        try:
                            escaped = _escape_js_string(selector)
                            await page.evaluate(f"""
                                () => {{
                                    const el = document.querySelector('{escaped}');
                                    if (el) {{
                                        el.classList.add('__force_hover');
                                        el.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true, cancelable: true}}));
                                        el.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true, cancelable: true}}));
                                        el.dispatchEvent(new PointerEvent('pointerenter', {{bubbles: true, cancelable: true, pointerType: 'mouse'}}));
                                        el.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true, cancelable: true, pointerType: 'mouse'}}));
                                    }}
                                }}
                            """)
                            await page.wait_for_timeout(_HOVER_SETTLE_MS)

                            hover_data = await asyncio.wait_for(
                                page.evaluate(
                                    f"({interaction_js})('{_escape_js_string(selector)}')"
                                ),
                                timeout=_EVALUATE_TIMEOUT_MS / 1000,
                            )
                            if hover_data and isinstance(hover_data, dict):
                                hover_styles = hover_data.get("default_state", {})
                                diff = _style_diff(default_styles, hover_styles)
                                if diff:
                                    hover_diff = diff
                                    hover_method = "dom_dispatch"

                            # Clean up: remove force class and dispatch leave events
                            await page.evaluate(f"""
                                () => {{
                                    const el = document.querySelector('{escaped}');
                                    if (el) {{
                                        el.classList.remove('__force_hover');
                                        el.dispatchEvent(new MouseEvent('mouseleave', {{bubbles: true, cancelable: true}}));
                                        el.dispatchEvent(new PointerEvent('pointerleave', {{bubbles: true, cancelable: true, pointerType: 'mouse'}}));
                                    }}
                                }}
                            """)
                        except Exception as exc:
                            logger.debug("Strategy 2 (dispatchEvent) failed for %s: %s", selector, exc)

                    if hover_diff:
                        entry["hover"] = hover_diff
                        entry["_hover_method"] = hover_method

            except Exception as exc:
                logger.debug("Hover capture failed for %s: %s", selector, exc)

            # --- Focus ---
            try:
                await page.focus(selector, timeout=3000)
                await page.wait_for_timeout(200)

                focus_data = await asyncio.wait_for(
                    page.evaluate(
                        f"({interaction_js})('{_escape_js_string(selector)}')"
                    ),
                    timeout=_EVALUATE_TIMEOUT_MS / 1000,
                )
                if focus_data and isinstance(focus_data, dict):
                    focus_styles = focus_data.get("default_state", {})
                    diff = _style_diff(default_styles, focus_styles)
                    if diff:
                        entry["focus"] = diff
            except Exception as exc:
                logger.debug("Focus capture failed for %s: %s", selector, exc)

            # Move mouse away to reset state
            try:
                await page.mouse.move(0, 0)
                await page.wait_for_timeout(100)
            except Exception:
                pass

            results.append(entry)

        except asyncio.TimeoutError:
            logger.debug("Interaction capture timed out for %s", selector)
        except Exception as exc:
            logger.debug("Interaction capture failed for %s: %s", selector, exc)

    # Re-enable Lenis if we disabled it
    if lenis_was_active:
        try:
            await page.evaluate("""
                () => {
                    try { if (window.lenis) window.lenis.start(); } catch (e) {}
                    try { if (window.__lenis) window.__lenis.start(); } catch (e) {}
                    document.documentElement.classList.remove('lenis-stopped');
                }
            """)
        except Exception:
            pass

    return results


async def _extract_css_hover_rules(page) -> list[dict]:
    """Extract :hover rules from stylesheets to supplement live hover capture."""
    try:
        rules = await page.evaluate("""
        () => {
            const hovers = [];
            try {
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules || []) {
                            if (rule.selectorText && rule.selectorText.includes(':hover')) {
                                const sel = rule.selectorText.replace(/:hover/g, '').trim();
                                const props = {};
                                const style = rule.style;
                                for (let i = 0; i < style.length; i++) {
                                    const prop = style[i];
                                    const camel = prop.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                                    props[camel] = { from: null, to: style.getPropertyValue(prop) };
                                }
                                if (Object.keys(props).length > 0) {
                                    hovers.push({ selector: sel, properties: props });
                                }
                            }
                        }
                    } catch(e) {} // cross-origin stylesheet
                }
            } catch(e) {}
            return hovers.slice(0, 100);
        }
        """)
        return rules or []
    except Exception:
        return []


def _escape_js_string(s: str) -> str:
    """Escape a string for safe embedding inside a JS single-quoted string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")


def _ix2_easing_name(easing_obj) -> str:
    """Convert Webflow IX2 easing config to a readable name."""
    if not easing_obj:
        return "ease"
    if isinstance(easing_obj, str):
        return easing_obj
    if isinstance(easing_obj, dict):
        fn = easing_obj.get("fn")
        if isinstance(fn, list) and len(fn) == 4:
            return f"cubic-bezier({fn[0]}, {fn[1]}, {fn[2]}, {fn[3]})"
        name = easing_obj.get("type") or easing_obj.get("name")
        if name:
            return str(name)
    return "ease"


# ---------------------------------------------------------------------------
# 4. Post-processing: structured output
# ---------------------------------------------------------------------------
def _build_animation_list(initial_capture: dict, scroll_map: dict) -> list[dict]:
    """
    Flatten all discovered animations into a uniform list with fields:
    target, trigger, duration_ms, easing, delay_ms, transform_from, transform_to, properties.
    """
    animations: list[dict] = []

    def _normalize(anim: dict, trigger: str) -> dict:
        target = anim.get("target") or {}
        keyframes = anim.get("keyframes", [])

        # Derive transform_from / transform_to from keyframes if available
        transform_from = None
        transform_to = None
        if keyframes:
            first = keyframes[0] if keyframes else {}
            last = keyframes[-1] if len(keyframes) > 1 else {}
            transform_from = first.get("transform") or first.get("offset")
            transform_to = last.get("transform") or last.get("offset")

        duration_raw = anim.get("duration")
        duration_ms = None
        if isinstance(duration_raw, (int, float)):
            duration_ms = float(duration_raw)

        delay_raw = anim.get("delay", 0)
        delay_ms = float(delay_raw) if isinstance(delay_raw, (int, float)) else 0

        # Gather animated properties from GSAP or keyframes
        properties = anim.get("properties") or {}
        if not properties and keyframes:
            # Collect all property names across keyframes (excluding meta keys)
            _meta = {"offset", "easing", "composite", "computedOffset"}
            for kf in keyframes:
                for k, v in kf.items():
                    if k not in _meta:
                        properties[k] = v

        return {
            "target": {
                "tag": target.get("tag"),
                "id": target.get("id"),
                "classes": target.get("classes", []),
                "text": target.get("text"),
            } if target else None,
            "type": anim.get("type") or anim.get("animationName") or "unknown",
            "trigger": trigger,
            "duration_ms": duration_ms,
            "easing": anim.get("easing") or anim.get("ease"),
            "delay_ms": delay_ms,
            "transform_from": transform_from,
            "transform_to": transform_to,
            "properties": properties or None,
            "iterations": anim.get("iterations"),
            "direction": anim.get("direction"),
            "fill": anim.get("fill"),
            "stagger": anim.get("stagger"),
        }

    # Animations from the initial page load
    for anim in _all_animations_from_capture(initial_capture):
        animations.append(_normalize(anim, "load"))

    # Animations from scroll positions
    for pct_key, anims in scroll_map.items():
        trigger = f"scroll:{pct_key}"
        for anim in anims:
            animations.append(_normalize(anim, trigger))

    return animations


def _build_global_motion_rules(animations: list[dict], initial_capture: dict) -> dict:
    """
    Derive global motion rules from the entire animation dataset:
    default duration range, default easing, forbidden patterns.
    """
    durations = [
        a["duration_ms"]
        for a in animations
        if a.get("duration_ms") is not None and a["duration_ms"] > 0
    ]
    easings = [a["easing"] for a in animations if a.get("easing")]

    # Easing frequency
    easing_counts: dict[str, int] = {}
    for e in easings:
        easing_counts[e] = easing_counts.get(e, 0) + 1

    # GSAP defaults
    gsap_defaults = (initial_capture.get("gsap_config") or {}).get("defaults", {})

    most_common_easing = max(easing_counts, key=easing_counts.get) if easing_counts else None

    rules = {
        "duration_range_ms": {
            "min": round(min(durations), 1) if durations else None,
            "max": round(max(durations), 1) if durations else None,
            "median": round(sorted(durations)[len(durations) // 2], 1) if durations else None,
        },
        "default_easing": most_common_easing,
        "easing_distribution": easing_counts,
        "gsap_defaults": gsap_defaults if gsap_defaults else None,
        "total_animation_count": len(animations),
        "forbidden_patterns": _detect_forbidden_patterns(animations),
    }

    return rules


def _detect_forbidden_patterns(animations: list[dict]) -> list[str]:
    """Heuristic detection of motion anti-patterns NOT used on the site."""
    patterns_seen = set()
    for a in animations:
        props = a.get("properties") or {}
        easing = a.get("easing") or ""
        dur = a.get("duration_ms") or 0

        if "left" in props or "top" in props:
            patterns_seen.add("layout_triggering_animations")
        if dur > 2000:
            patterns_seen.add("long_animations_over_2s")
        if "linear" == easing:
            patterns_seen.add("linear_easing")
        if a.get("iterations") == "Infinity" or a.get("iterations") == float("inf"):
            patterns_seen.add("infinite_loops")

    # "Forbidden" = patterns NOT found on this site (the site avoids them)
    all_known = {
        "layout_triggering_animations",
        "long_animations_over_2s",
        "linear_easing",
        "infinite_loops",
        "flash_of_content",
    }
    forbidden = sorted(all_known - patterns_seen)
    return forbidden


def _build_micro_interaction_table(hover_states: list[dict]) -> list[dict]:
    """
    Transform hover_states into a clean component x state table.
    Each row: component identifier, hover changes, focus changes, transition info.
    """
    table = []
    for entry in hover_states:
        row = {
            "component": {
                "selector": entry.get("selector"),
                "tag": entry.get("tag"),
                "id": entry.get("id"),
                "classes": entry.get("classes"),
                "text": entry.get("text"),
            },
            "states": {},
            "transition_duration_ms": (entry.get("transition") or {}).get(
                "transition_duration_ms", 0
            ),
            "transition_easing": (entry.get("transition") or {}).get(
                "transition_easing", "ease"
            ),
        }

        if entry.get("hover"):
            row["states"]["hover"] = entry["hover"]
        if entry.get("focus"):
            row["states"]["focus"] = entry["focus"]

        # Only include if there is at least one state change
        if row["states"]:
            table.append(row)

    return table


# ---------------------------------------------------------------------------
# CDP Animation Domain — passive capture of all animations
# ---------------------------------------------------------------------------
async def _setup_cdp_animation_capture(page):
    """
    Enable the CDP Animation domain to passively capture all animations
    (CSS, WAAPI, GSAP-driven) without polling.
    Returns (cdp_session, captured_events_list).
    """
    captured = []
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Animation.enable")

        def on_animation_started(params):
            anim = params.get("animation", {})
            captured.append({
                "id": anim.get("id"),
                "name": anim.get("name"),
                "type": anim.get("type"),
                "duration": anim.get("source", {}).get("duration"),
                "delay": anim.get("source", {}).get("delay"),
                "backend_node_id": anim.get("source", {}).get("backendNodeId"),
                "keyframes_rule": anim.get("source", {}).get("keyframesRule"),
                "easing": anim.get("source", {}).get("easing"),
            })

        cdp.on("Animation.animationStarted", on_animation_started)
        return cdp, captured
    except Exception as exc:
        logger.debug("CDP Animation setup failed (non-critical): %s", exc)
        return None, captured


async def _teardown_cdp_animation(cdp):
    """Disable animation domain and detach CDP session."""
    if not cdp:
        return
    try:
        await cdp.send("Animation.disable")
        await cdp.detach()
    except Exception:
        pass


def _merge_cdp_animations(cdp_captured: list, existing_animations: list) -> list:
    """Merge CDP-captured animation events into the animation list, avoiding duplicates."""
    existing_sigs = set()
    for a in existing_animations:
        parts = [
            str(a.get("type", "")),
            str(a.get("duration_ms", "")),
            str((a.get("target") or {}).get("tag", "")),
        ]
        existing_sigs.add("|".join(parts))

    merged = []
    for cdp_anim in cdp_captured:
        dur = cdp_anim.get("duration")
        if dur is None or dur <= 0:
            continue
        sig = f"{cdp_anim.get('type', '')}|{dur}|"
        if sig in existing_sigs:
            continue
        merged.append({
            "target": None,
            "type": cdp_anim.get("type") or cdp_anim.get("name") or "cdp-captured",
            "trigger": "cdp-passive",
            "duration_ms": dur,
            "easing": cdp_anim.get("easing"),
            "delay_ms": cdp_anim.get("delay") or 0,
            "transform_from": None,
            "transform_to": None,
            "properties": None,
            "iterations": None,
            "direction": None,
            "fill": None,
            "stagger": None,
            "_cdp_id": cdp_anim.get("id"),
            "_cdp_name": cdp_anim.get("name"),
        })

    return merged


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def extract_animations(page) -> dict:
    """
    Master function: run all animation extraction phases and return a
    JSON-serializable dict.

    Returns dict with keys:
        animations          — list of all animations (load + scroll triggered)
        scroll_animation_map — scroll_percent -> element -> animation_spec
        hover_states        — raw hover/focus capture per interactive element
        micro_interactions  — cleaned component x state table
        global_motion_rules — duration range, default easing, forbidden patterns
        gsap_config         — GSAP version and defaults
        lenis_config        — Lenis smooth-scroll config
        lottie_animations   — Lottie player instances found
        keyframe_definitions— @keyframes rules from stylesheets
    """
    logger.info("Phase 3: Starting animation extraction")

    # ------------------------------------------------------------------
    # Step 0: Enable CDP Animation domain for passive background capture
    # ------------------------------------------------------------------
    cdp_session, cdp_captured = await _setup_cdp_animation_capture(page)

    # ------------------------------------------------------------------
    # Step 1: Initial animation capture (at page load, scroll = 0)
    # ------------------------------------------------------------------
    logger.info("  [1/4] Capturing initial animations...")
    initial_capture = await _capture_animations(page)
    logger.info(
        "  [1/4] Found %d CSS animations, %d CSS transitions, %d GSAP tweens, "
        "%d ScrollTriggers, %d Lottie players, %d keyframe defs",
        len(initial_capture.get("css_animations", [])),
        len(initial_capture.get("css_transitions", [])),
        len(initial_capture.get("gsap_tweens", [])),
        len(initial_capture.get("gsap_scroll_triggers", [])),
        len(initial_capture.get("lottie_animations", [])),
        len(initial_capture.get("keyframe_definitions", [])),
    )

    # ------------------------------------------------------------------
    # Step 2: Scroll-triggered animation capture
    # ------------------------------------------------------------------
    logger.info("  [2/4] Capturing scroll-triggered animations...")
    scroll_map = await _capture_scroll_animations(page)
    total_scroll_anims = sum(len(v) for v in scroll_map.values())
    logger.info(
        "  [2/4] Found %d new animations across %d scroll positions",
        total_scroll_anims,
        len(scroll_map),
    )

    # ------------------------------------------------------------------
    # Step 3: Hover / focus state capture
    # ------------------------------------------------------------------
    logger.info("  [3/4] Capturing hover/focus micro-interactions...")
    hover_states = await _capture_hover_states(page)

    # Supplement with CSS :hover rules from stylesheets
    css_hovers = await _extract_css_hover_rules(page)
    if css_hovers:
        live_selectors = {h.get("selector") for h in hover_states if h.get("hover")}
        for rule in css_hovers:
            sel = rule.get("selector", "")
            if sel not in live_selectors:
                hover_states.append({
                    "selector": sel,
                    "hover": rule.get("properties", {}),
                    "focus": None,
                    "transition": rule.get("transition"),
                    "tag": rule.get("tag", "element"),
                    "text": None,
                    "id": None,
                    "classes": [],
                    "_source": "stylesheet",
                })

    hover_count = sum(1 for h in hover_states if h.get("hover"))
    focus_count = sum(1 for h in hover_states if h.get("focus"))
    logger.info(
        "  [3/4] Captured %d elements: %d with hover changes, %d with focus changes",
        len(hover_states),
        hover_count,
        focus_count,
    )

    # ------------------------------------------------------------------
    # Step 4: Structure the output
    # ------------------------------------------------------------------
    logger.info("  [4/4] Processing and structuring animation data...")

    animations = _build_animation_list(initial_capture, scroll_map)
    global_motion_rules = _build_global_motion_rules(animations, initial_capture)
    micro_interactions = _build_micro_interaction_table(hover_states)

    # ------------------------------------------------------------------
    # Step 4B: Process Webflow IX2 data
    # ------------------------------------------------------------------
    webflow_ix2 = initial_capture.get("webflow_ix2")
    ix2_interactions = []
    if webflow_ix2 and isinstance(webflow_ix2, dict) and webflow_ix2.get("interactions"):
        ix2_interactions = webflow_ix2["interactions"]
        logger.info(
            "  [4B] Webflow IX2: %d interactions, %d action lists",
            webflow_ix2.get("eventCount", 0),
            webflow_ix2.get("actionCount", 0),
        )
        # Merge IX2 timings into the animation list for duration/easing data
        for ix in ix2_interactions:
            for timing in (ix.get("timings") or []):
                dur = timing.get("duration")
                try:
                    dur = float(dur) if dur is not None else None
                except (TypeError, ValueError):
                    dur = None
                if dur is not None and dur > 0:
                    trigger_type = ix.get("triggerType") or "ix2"
                    target_info = timing.get("target") or {}
                    animations.append({
                        "target": {
                            "tag": None,
                            "id": None,
                            "classes": [],
                            "text": None,
                            "selector": target_info.get("selector"),
                        },
                        "type": timing.get("actionTypeId") or "webflow-ix2",
                        "trigger": f"ix2:{trigger_type}",
                        "duration_ms": dur,
                        "easing": _ix2_easing_name(timing.get("easing")),
                        "delay_ms": timing.get("delay") or 0,
                        "transform_from": None,
                        "transform_to": None,
                        "properties": None,
                        "iterations": 1,
                        "direction": "normal",
                        "fill": None,
                        "stagger": None,
                        "source": "webflow-ix2",
                    })
        # Recompute global motion rules with IX2 data included
        global_motion_rules = _build_global_motion_rules(animations, initial_capture)

    # ------------------------------------------------------------------
    # Step 4C: Process computed transitions from DOM walk
    # ------------------------------------------------------------------
    computed_transitions = initial_capture.get("computed_transitions", [])
    if computed_transitions:
        logger.info("  [4C] Found %d elements with CSS transition-duration set", len(computed_transitions))

    # ------------------------------------------------------------------
    # Step 4D: Merge CDP passively-captured animations
    # ------------------------------------------------------------------
    cdp_merged = _merge_cdp_animations(cdp_captured, animations)
    if cdp_merged:
        animations.extend(cdp_merged)
        global_motion_rules = _build_global_motion_rules(animations, initial_capture)
        logger.info("  [4D] CDP captured %d additional animations (total: %d)", len(cdp_merged), len(cdp_captured))

    await _teardown_cdp_animation(cdp_session)

    # ------------------------------------------------------------------
    # Step 4E: Collect early hook data (preloader, IO targets, route changes)
    # ------------------------------------------------------------------
    early_hooks = {}
    try:
        early_hooks = await page.evaluate("() => window.__earlyHooks || {}") or {}
    except Exception:
        pass

    result = {
        "animations": animations,
        "scroll_animation_map": scroll_map,
        "hover_states": hover_states,
        "micro_interactions": micro_interactions,
        "global_motion_rules": global_motion_rules,
        "gsap_config": initial_capture.get("gsap_config") or {},
        "lenis_config": initial_capture.get("lenis_config") or {},
        "lottie_animations": initial_capture.get("lottie_animations", []),
        "keyframe_definitions": initial_capture.get("keyframe_definitions", []),
        "webflow_ix2": webflow_ix2 if webflow_ix2 else None,
        "computed_transitions": computed_transitions,
        "page_transition_libraries": initial_capture.get("page_transition_libraries") or {},
        "io_targets": early_hooks.get("ioTargets", []),
        "cdp_animation_count": len(cdp_captured),
        "gsap_authoring": initial_capture.get("gsap_authoring") or None,
    }

    logger.info(
        "Phase 3 complete: %d total animations, %d scroll triggers, "
        "%d micro-interactions, %d keyframe defs, %d computed transitions, "
        "%d CDP passive%s",
        len(animations),
        len(scroll_map),
        len(micro_interactions),
        len(result["keyframe_definitions"]),
        len(computed_transitions),
        len(cdp_merged),
        f", {len(ix2_interactions)} Webflow IX2 interactions" if ix2_interactions else "",
    )

    return result
