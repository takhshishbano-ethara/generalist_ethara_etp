"""
Deep Interaction Capture — splash screens, click effects, menus, modals,
active states, form interactions, page transitions, scroll mutations.

Complements animation_extractor.py (which handles CSS/GSAP animations,
scroll-triggered animation diffing, and hover/focus states).
"""

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MAX_CLICK_TARGETS = 12
_MAX_FORM_FIELDS = 8
_MAX_SCROLL_MUTATIONS = 50
_SETTLE_MS = 600


async def capture_interactions(page, url: str, output_dir: str, site_data: dict = None) -> dict:
    """Master entry point — captures all interactive behaviors."""
    shots_dir = os.path.join(output_dir, "screenshots", "interactions")
    os.makedirs(shots_dir, exist_ok=True)

    result = {
        "splash_screen": None,
        "click_interactions": [],
        "menu_expansions": [],
        "modals": [],
        "active_states": [],
        "form_interactions": [],
        "page_transitions": None,
        "scroll_mutations": [],
    }

    # 1 — Splash / preloader
    try:
        result["splash_screen"] = await _capture_splash(page, shots_dir)
    except Exception as exc:
        logger.warning("Splash capture failed: %s", exc)

    # 2 — Scroll DOM mutations (before clicking things that change the page)
    try:
        result["scroll_mutations"] = await _capture_scroll_mutations(page)
    except Exception as exc:
        logger.warning("Scroll mutation capture failed: %s", exc)

    # Reset scroll
    try:
        await page.evaluate("window.scrollTo(0,0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    # 3 — Active (mousedown) states
    try:
        result["active_states"] = await _capture_active_states(page)
    except Exception as exc:
        logger.warning("Active state capture failed: %s", exc)

    # 4 — Form interactions
    try:
        result["form_interactions"] = await _capture_form_interactions(page)
    except Exception as exc:
        logger.warning("Form interaction capture failed: %s", exc)

    # 5 — Menu / dropdown / accordion expansion
    try:
        result["menu_expansions"] = await _capture_menu_expansions(page, shots_dir)
    except Exception as exc:
        logger.warning("Menu expansion capture failed: %s", exc)

    # 6 — Modal / overlay triggers
    try:
        result["modals"] = await _capture_modals(page, shots_dir)
    except Exception as exc:
        logger.warning("Modal capture failed: %s", exc)

    # 7 — General click interactions (buttons, cards, CTAs)
    try:
        result["click_interactions"] = await _capture_click_interactions(page, shots_dir)
    except Exception as exc:
        logger.warning("Click interaction capture failed: %s", exc)

    # 8 — Page transitions (navigate to an internal link)
    try:
        pages = (site_data or {}).get("pages", [])
        result["page_transitions"] = await _capture_page_transition(page, url, pages, shots_dir)
    except Exception as exc:
        logger.warning("Page transition capture failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# 1. Splash / Preloader — uses early hooks for lifecycle timing
# ---------------------------------------------------------------------------
async def _capture_splash(page, shots_dir: str):
    """
    Detect and document the splash/preloader state.
    Reads from window.__earlyHooks.preloader for lifecycle timing data
    (appeared/disappeared timestamps, exit animation method).
    Falls back to DOM query if early hooks didn't capture it.
    """
    # First: read early hooks preloader data (captured from first paint)
    early_preloader = None
    try:
        early_preloader = await page.evaluate("() => window.__earlyHooks?.preloader || null")
    except Exception:
        pass

    if early_preloader and early_preloader.get("found"):
        # Early hooks captured the full lifecycle
        splash = {
            "found": True,
            "selector": early_preloader.get("selector"),
            "tag": early_preloader.get("tag"),
            "classes": early_preloader.get("classes", []),
            "id": early_preloader.get("id"),
            "dimensions": early_preloader.get("dimensions"),
            "visible": early_preloader.get("visible", False),
            "position": early_preloader.get("position"),
            "zIndex": early_preloader.get("zIndex"),
            "background": early_preloader.get("background"),
            "animation_name": early_preloader.get("animationName"),
            "transition_duration": early_preloader.get("transitionDuration"),
            "visible_duration_ms": early_preloader.get("visibleDurationMs"),
            "exit_method": early_preloader.get("exitMethod"),
            "appeared_at": early_preloader.get("appearedAt"),
            "disappeared_at": early_preloader.get("disappearedAt"),
            "_source": "early_hooks",
        }
        # Enrich with inner animations from keyframes
        try:
            inner_anims = await page.evaluate("""
                (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return [];
                    const anims = [];
                    // Check children for animation-name
                    el.querySelectorAll('*').forEach(child => {
                        const cs = getComputedStyle(child);
                        if (cs.animationName && cs.animationName !== 'none') {
                            anims.push(cs.animationName);
                        }
                    });
                    return [...new Set(anims)].slice(0, 5);
                }
            """, early_preloader.get("selector"))
            if inner_anims:
                splash["inner_animations"] = inner_anims
        except Exception:
            pass
        return splash

    # Fallback: DOM query (preloader may have already finished before early hooks ran)
    splash = await page.evaluate("""
        () => {
            const preloaderSelectors = [
                '[class*="preloader"]', '[class*="Preloader"]',
                '[class*="loader"]', '[class*="Loader"]',
                '[class*="loading"]', '[class*="Loading"]',
                '[class*="splash"]', '[class*="Splash"]',
                '[class*="intro-screen"]', '[class*="page-loader"]',
                '[id*="preloader"]', '[id*="loader"]',
                '[data-preloader]', '[data-loader]',
                '.pace', '.nprogress',
            ];
            for (const sel of preloaderSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    // Check if it's already hidden (preloader finished)
                    const visible = cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.01;
                    return {
                        found: true,
                        selector: sel,
                        tag: el.tagName.toLowerCase(),
                        classes: Array.from(el.classList).slice(0, 8),
                        id: el.id || null,
                        dimensions: { w: Math.round(r.width), h: Math.round(r.height) },
                        visible: visible,
                        already_hidden: !visible,
                        position: cs.position,
                        zIndex: cs.zIndex,
                        background: cs.backgroundColor,
                        has_animation: cs.animationName !== 'none' || cs.transitionDuration !== '0s',
                        animation_name: cs.animationName !== 'none' ? cs.animationName : null,
                        transition_duration: cs.transitionDuration,
                        transition_property: cs.transitionProperty,
                    };
                }
            }
            // Check for progress bar elements
            const progress = document.querySelector('progress, [role="progressbar"], [class*="progress"]');
            if (progress) {
                return {
                    found: true,
                    selector: 'progress',
                    tag: progress.tagName.toLowerCase(),
                    classes: Array.from(progress.classList).slice(0, 5),
                    type: 'progress_bar',
                    visible: getComputedStyle(progress).display !== 'none',
                };
            }
            return { found: false };
        }
    """)

    if splash and splash.get("found"):
        splash["_source"] = "dom_query"
        # If the preloader was found but already hidden, try to infer exit duration
        # from its transition-duration CSS property
        if splash.get("already_hidden") and splash.get("transition_duration"):
            try:
                raw = splash["transition_duration"]
                max_ms = 0
                for part in raw.split(","):
                    part = part.strip().lower()
                    if part.endswith("ms"):
                        max_ms = max(max_ms, float(part[:-2]))
                    elif part.endswith("s"):
                        max_ms = max(max_ms, float(part[:-1]) * 1000)
                if max_ms > 0:
                    splash["exit_duration_ms"] = max_ms
                    splash["exit_method"] = "fade-out (inferred from transition-duration)"
            except Exception:
                pass

        if splash.get("visible"):
            path = os.path.join(shots_dir, "splash_preloader.png")
            try:
                await page.screenshot(path=path, full_page=False)
                splash["screenshot"] = path
            except Exception:
                pass
        return splash

    return {"found": False}


# ---------------------------------------------------------------------------
# 2. Scroll DOM Mutations
# ---------------------------------------------------------------------------
async def _capture_scroll_mutations(page) -> list[dict]:
    """Scroll through the page and record class/visibility changes on elements."""
    mutations = []

    await page.evaluate("""
        () => {
            window.__scrollMutations = [];
            const observer = new MutationObserver(muts => {
                for (const m of muts) {
                    if (m.type === 'attributes' && m.attributeName === 'class') {
                        const el = m.target;
                        if (!(el instanceof HTMLElement)) continue;
                        const oldVal = m.oldValue || '';
                        const newVal = el.className?.toString() || '';
                        if (oldVal === newVal) continue;
                        const oldSet = new Set(oldVal.split(/\\s+/).filter(Boolean));
                        const newSet = new Set(newVal.split(/\\s+/).filter(Boolean));
                        const added = [...newSet].filter(c => !oldSet.has(c));
                        const removed = [...oldSet].filter(c => !newSet.has(c));
                        if (added.length === 0 && removed.length === 0) continue;
                        const r = el.getBoundingClientRect();
                        window.__scrollMutations.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            classes: Array.from(el.classList).slice(0, 5),
                            added, removed,
                            scrollY: Math.round(window.scrollY),
                            y: Math.round(r.y),
                        });
                    }
                }
            });
            observer.observe(document.body, {
                attributes: true, attributeFilter: ['class'],
                attributeOldValue: true, subtree: true,
            });
            window.__scrollMutationObserver = observer;
        }
    """)

    dims = await page.evaluate(
        "() => ({ sh: document.documentElement.scrollHeight, ih: window.innerHeight })"
    )
    max_scroll = max(dims["sh"] - dims["ih"], 1)

    for pct in range(0, 105, 5):
        y = int((min(pct, 100) / 100) * max_scroll)
        await page.evaluate(f"window.scrollTo({{ top: {y}, behavior: 'instant' }})")
        await page.wait_for_timeout(250)

    # Collect results
    raw = await page.evaluate("""
        () => {
            if (window.__scrollMutationObserver) window.__scrollMutationObserver.disconnect();
            const data = window.__scrollMutations || [];
            delete window.__scrollMutations;
            delete window.__scrollMutationObserver;
            return data.slice(0, 200);
        }
    """)

    if not raw:
        return []

    # Deduplicate and summarize
    seen = set()
    for m in raw:
        key = f"{m.get('tag')}#{m.get('id')}:{','.join(m.get('added', []))}"
        if key in seen:
            continue
        seen.add(key)
        scroll_pct = round((m.get("scrollY", 0) / max(max_scroll, 1)) * 100)
        mutations.append({
            "scroll_percent": scroll_pct,
            "tag": m.get("tag"),
            "id": m.get("id"),
            "classes": m.get("classes"),
            "classes_added": m.get("added"),
            "classes_removed": m.get("removed"),
            "element_y": m.get("y"),
        })
        if len(mutations) >= _MAX_SCROLL_MUTATIONS:
            break

    # Reset scroll
    await page.evaluate("window.scrollTo(0,0)")
    await page.wait_for_timeout(200)

    return mutations


# ---------------------------------------------------------------------------
# 3. Active (:active / mousedown) States
# ---------------------------------------------------------------------------
async def _capture_active_states(page) -> list[dict]:
    """Capture mousedown style changes on interactive elements."""
    results = []

    elements = await page.evaluate("""
        () => {
            const els = document.querySelectorAll('button, a[href], [role="button"], [class*="btn"], [class*="cta"]');
            const out = [];
            for (const el of els) {
                if (out.length >= 10) break;
                const r = el.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) continue;
                if (r.y < 0 || r.y > window.innerHeight * 2) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                out.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    classes: Array.from(el.classList).slice(0, 5),
                    text: (el.textContent || '').trim().substring(0, 40),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                    default_styles: {
                        transform: cs.transform,
                        boxShadow: cs.boxShadow,
                        backgroundColor: cs.backgroundColor,
                        color: cs.color,
                        scale: cs.scale,
                        opacity: cs.opacity,
                        outline: cs.outline,
                        borderColor: cs.borderColor,
                    },
                });
            }
            return out;
        }
    """)

    for el in (elements or []):
        try:
            cx, cy = el["x"], el["y"]
            await page.mouse.move(cx, cy)
            await page.mouse.down()
            await page.wait_for_timeout(150)

            active_styles = await page.evaluate(f"""
                () => {{
                    const el = document.elementFromPoint({cx}, {cy});
                    if (!el) return null;
                    const cs = getComputedStyle(el);
                    return {{
                        transform: cs.transform,
                        boxShadow: cs.boxShadow,
                        backgroundColor: cs.backgroundColor,
                        color: cs.color,
                        scale: cs.scale,
                        opacity: cs.opacity,
                        outline: cs.outline,
                        borderColor: cs.borderColor,
                    }};
                }}
            """)

            await page.mouse.up()
            await page.wait_for_timeout(100)

            if active_styles:
                diff = {}
                for prop in el["default_styles"]:
                    before = el["default_styles"][prop]
                    after = active_styles.get(prop)
                    if before != after:
                        diff[prop] = {"from": before, "to": after}
                if diff:
                    results.append({
                        "tag": el["tag"],
                        "id": el["id"],
                        "classes": el["classes"],
                        "text": el["text"],
                        "active_changes": diff,
                    })
        except Exception:
            try:
                await page.mouse.up()
            except Exception:
                pass

    return results


# ---------------------------------------------------------------------------
# 4. Form Interactions
# ---------------------------------------------------------------------------
async def _capture_form_interactions(page) -> list[dict]:
    """Focus into form fields and capture label/placeholder/validation animations."""
    results = []

    fields = await page.evaluate("""
        () => {
            const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]), textarea, select');
            const out = [];
            for (const el of inputs) {
                if (out.length >= 8) break;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none') continue;
                // Find associated label
                let labelText = null;
                if (el.id) {
                    const label = document.querySelector('label[for="' + el.id + '"]');
                    if (label) labelText = label.textContent.trim().substring(0, 40);
                }
                if (!labelText && el.parentElement) {
                    const label = el.parentElement.querySelector('label');
                    if (label) labelText = label.textContent.trim().substring(0, 40);
                }
                // Check for floating label
                let floatingLabel = null;
                const prev = el.previousElementSibling;
                const next = el.nextElementSibling;
                const parent = el.parentElement;
                for (const candidate of [prev, next, parent?.querySelector('label'), parent?.querySelector('span')]) {
                    if (candidate) {
                        const ccs = getComputedStyle(candidate);
                        if (ccs.position === 'absolute' || ccs.position === 'relative') {
                            floatingLabel = {
                                tag: candidate.tagName.toLowerCase(),
                                text: candidate.textContent.trim().substring(0, 30),
                                transform: ccs.transform,
                                top: ccs.top,
                                fontSize: ccs.fontSize,
                                color: ccs.color,
                                transition: ccs.transitionDuration,
                            };
                            break;
                        }
                    }
                }
                out.push({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || null,
                    id: el.id || null,
                    name: el.name || null,
                    placeholder: el.placeholder || null,
                    label: labelText,
                    floatingLabel: floatingLabel,
                    default_border: cs.borderColor,
                    default_boxShadow: cs.boxShadow,
                    default_outline: cs.outline,
                    default_background: cs.backgroundColor,
                    transition: cs.transitionDuration,
                });
            }
            return out;
        }
    """)

    for field in (fields or []):
        selector = f"#{field['id']}" if field.get("id") else f"{field['tag']}[name='{field.get('name', '')}']"
        try:
            el = await page.query_selector(selector)
            if not el:
                continue
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.focus()
            await page.wait_for_timeout(400)

            safe_sel = selector.replace("'", "\\'")
            focused_state = await page.evaluate(f"""
                () => {{
                    const el = document.querySelector('{safe_sel}');
                    if (!el) return null;
                    const cs = getComputedStyle(el);
                    let labelState = null;
                    // Re-check floating label position
                    const parent = el.parentElement;
                    if (parent) {{
                        for (const candidate of [parent.querySelector('label'), parent.querySelector('span')]) {{
                            if (candidate) {{
                                const ccs = getComputedStyle(candidate);
                                labelState = {{
                                    transform: ccs.transform,
                                    top: ccs.top,
                                    fontSize: ccs.fontSize,
                                    color: ccs.color,
                                }};
                                break;
                            }}
                        }}
                    }}
                    return {{
                        borderColor: cs.borderColor,
                        boxShadow: cs.boxShadow,
                        outline: cs.outline,
                        outlineColor: cs.outlineColor,
                        backgroundColor: cs.backgroundColor,
                        labelState: labelState,
                    }};
                }}
            """)

            await page.evaluate("document.activeElement?.blur()")
            await page.wait_for_timeout(200)

            if focused_state:
                border_changed = focused_state.get("borderColor") != field.get("default_border")
                shadow_changed = focused_state.get("boxShadow") != field.get("default_boxShadow")
                outline_changed = focused_state.get("outline") != field.get("default_outline")

                label_animated = False
                if field.get("floatingLabel") and focused_state.get("labelState"):
                    before_label = field["floatingLabel"]
                    after_label = focused_state["labelState"]
                    label_animated = (
                        before_label.get("transform") != after_label.get("transform") or
                        before_label.get("top") != after_label.get("top") or
                        before_label.get("fontSize") != after_label.get("fontSize")
                    )

                results.append({
                    "tag": field["tag"],
                    "type": field.get("type"),
                    "id": field.get("id"),
                    "name": field.get("name"),
                    "placeholder": field.get("placeholder"),
                    "label": field.get("label"),
                    "has_floating_label": bool(field.get("floatingLabel")),
                    "label_animates_on_focus": label_animated,
                    "focus_changes": {
                        "border_changed": border_changed,
                        "shadow_changed": shadow_changed,
                        "outline_changed": outline_changed,
                        "focus_border_color": focused_state.get("borderColor"),
                        "focus_box_shadow": focused_state.get("boxShadow"),
                        "focus_outline": focused_state.get("outline"),
                    },
                    "transition_duration": field.get("transition"),
                })
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# 5. Menu / Dropdown / Accordion Expansion
# ---------------------------------------------------------------------------
async def _capture_menu_expansions(page, shots_dir: str) -> list[dict]:
    """Find and expand hamburger menus, dropdowns, and accordions."""
    results = []

    triggers = await page.evaluate("""
        () => {
            const found = [];
            // Hamburger / mobile menu triggers
            const hamburgerSels = [
                '[class*="hamburger"]', '[class*="burger"]', '[class*="menu-toggle"]',
                '[class*="menu-btn"]', '[class*="nav-toggle"]', '[class*="mobile-menu"]',
                'button[aria-label*="menu" i]', 'button[aria-label*="Menu" i]',
                'button[aria-label*="navigation" i]',
                '[aria-controls][aria-expanded]',
                '[class*="MenuButton"]', '[class*="menu-button"]',
            ];
            for (const sel of hamburgerSels) {
                const el = document.querySelector(sel);
                if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 5 && r.height > 5) {
                        found.push({
                            type: 'hamburger',
                            selector: sel,
                            tag: el.tagName.toLowerCase(),
                            text: (el.textContent || '').trim().substring(0, 30),
                            ariaExpanded: el.getAttribute('aria-expanded'),
                            x: Math.round(r.x + r.width/2),
                            y: Math.round(r.y + r.height/2),
                        });
                        break;
                    }
                }
            }
            // Dropdowns
            document.querySelectorAll('[class*="dropdown"] > button, [class*="dropdown"] > a, details > summary').forEach(el => {
                if (found.length >= 6) return;
                const r = el.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) return;
                found.push({
                    type: 'dropdown',
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().substring(0, 30),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                });
            });
            // Accordions
            document.querySelectorAll('[class*="accordion"] button, [class*="accordion"] [role="button"], [class*="faq"] button').forEach(el => {
                if (found.length >= 8) return;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) return;
                found.push({
                    type: 'accordion',
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().substring(0, 40),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                });
            });
            return found;
        }
    """)

    for i, trigger in enumerate(triggers or []):
        try:
            cx, cy = trigger["x"], trigger["y"]

            # Capture before state
            before = await page.evaluate(f"""
                () => {{
                    const el = document.elementFromPoint({cx}, {cy});
                    if (!el) return null;
                    // Find the expandable target
                    const target = el.closest('[class*="dropdown"]') || el.closest('[class*="accordion"]') ||
                                   el.closest('details') || el.closest('nav') || el.parentElement;
                    const r = target ? target.getBoundingClientRect() : {{ width: 0, height: 0 }};
                    return {{ h: Math.round(r.height), w: Math.round(r.width), ariaExpanded: el.getAttribute('aria-expanded') }};
                }}
            """)

            await page.mouse.click(cx, cy)
            await page.wait_for_timeout(_SETTLE_MS)

            # Capture after state
            after = await page.evaluate(f"""
                () => {{
                    const el = document.elementFromPoint({cx}, {cy});
                    if (!el) return null;
                    const target = el.closest('[class*="dropdown"]') || el.closest('[class*="accordion"]') ||
                                   el.closest('details') || el.closest('nav') || el.parentElement;
                    const r = target ? target.getBoundingClientRect() : {{ width: 0, height: 0 }};
                    // Check for newly visible panels
                    let panelInfo = null;
                    const panel = document.querySelector('[class*="menu-panel"]:not([style*="display: none"]), [class*="nav-menu"]:not([style*="display: none"]), [class*="dropdown-list"]:not([style*="display: none"]), [aria-expanded="true"]');
                    if (panel) {{
                        const pr = panel.getBoundingClientRect();
                        const pcs = getComputedStyle(panel);
                        panelInfo = {{
                            w: Math.round(pr.width), h: Math.round(pr.height),
                            transform: pcs.transform, opacity: pcs.opacity,
                            transition: pcs.transitionDuration,
                        }};
                    }}
                    return {{
                        h: Math.round(r.height), w: Math.round(r.width),
                        ariaExpanded: el.getAttribute('aria-expanded'),
                        panel: panelInfo,
                    }};
                }}
            """)

            # Take screenshot of expanded state
            shot_path = os.path.join(shots_dir, f"menu_{trigger['type']}_{i}.png")
            await page.screenshot(path=shot_path, full_page=False)

            expanded = False
            if before and after:
                expanded = (
                    (after.get("h", 0) > before.get("h", 0) + 20) or
                    (before.get("ariaExpanded") == "false" and after.get("ariaExpanded") == "true") or
                    after.get("panel") is not None
                )

            results.append({
                "type": trigger["type"],
                "trigger_text": trigger.get("text"),
                "expanded": expanded,
                "before_height": before.get("h") if before else None,
                "after_height": after.get("h") if after else None,
                "panel": after.get("panel") if after else None,
                "screenshot": shot_path,
            })

            # Close it back (click again or press Escape)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        except Exception as exc:
            logger.debug("Menu expansion %d failed: %s", i, exc)

    return results


# ---------------------------------------------------------------------------
# 6. Modal / Overlay Triggers
# ---------------------------------------------------------------------------
async def _capture_modals(page, shots_dir: str) -> list[dict]:
    """Click elements that trigger modals/dialogs and capture the result."""
    results = []

    triggers = await page.evaluate("""
        () => {
            const candidates = [];
            // Buttons with modal-related attributes
            const sels = [
                '[data-modal]', '[data-toggle="modal"]', '[data-bs-toggle="modal"]',
                '[aria-haspopup="dialog"]', '[class*="modal-trigger"]',
                '[class*="open-modal"]', '[class*="popup-trigger"]',
                'button[class*="play"]', 'button[class*="video"]',
                '[class*="lightbox"]',
            ];
            for (const sel of sels) {
                document.querySelectorAll(sel).forEach(el => {
                    if (candidates.length >= 4) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) return;
                    candidates.push({
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().substring(0, 40),
                        classes: Array.from(el.classList).slice(0, 5),
                        x: Math.round(r.x + r.width/2),
                        y: Math.round(r.y + r.height/2),
                    });
                });
            }
            return candidates;
        }
    """)

    for i, trigger in enumerate(triggers or []):
        try:
            await page.mouse.click(trigger["x"], trigger["y"])
            await page.wait_for_timeout(_SETTLE_MS)

            modal_info = await page.evaluate("""
                () => {
                    const sels = [
                        '[role="dialog"]', '[class*="modal"]:not([style*="display: none"])',
                        '[class*="Modal"]:not([style*="display: none"])',
                        '[class*="popup"]:not([style*="display: none"])',
                        '[class*="lightbox"]:not([style*="display: none"])',
                        '[class*="overlay"]:not([style*="display: none"])',
                        'dialog[open]',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width < 50 || r.height < 50) continue;
                            const cs = getComputedStyle(el);
                            if (cs.display === 'none' || parseFloat(cs.opacity) < 0.1) continue;
                            // Check for backdrop
                            let hasBackdrop = false;
                            const prev = el.previousElementSibling;
                            if (prev) {
                                const pcs = getComputedStyle(prev);
                                if (pcs.position === 'fixed' && parseFloat(pcs.opacity) > 0) hasBackdrop = true;
                            }
                            return {
                                found: true,
                                tag: el.tagName.toLowerCase(),
                                classes: Array.from(el.classList).slice(0, 8),
                                w: Math.round(r.width), h: Math.round(r.height),
                                transform: cs.transform,
                                opacity: cs.opacity,
                                transition: cs.transitionDuration,
                                animation: cs.animationName !== 'none' ? cs.animationName : null,
                                hasBackdrop: hasBackdrop,
                                position: cs.position,
                                zIndex: cs.zIndex,
                            };
                        }
                    }
                    return { found: false };
                }
            """)

            if modal_info and modal_info.get("found"):
                shot_path = os.path.join(shots_dir, f"modal_{i}.png")
                await page.screenshot(path=shot_path, full_page=False)

                results.append({
                    "trigger_text": trigger.get("text"),
                    "trigger_classes": trigger.get("classes"),
                    "modal": modal_info,
                    "screenshot": shot_path,
                })

            # Close modal
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            # Click backdrop if still open
            try:
                await page.evaluate("""
                    () => {
                        const overlay = document.querySelector('[class*="overlay"], [class*="backdrop"]');
                        if (overlay) overlay.click();
                    }
                """)
            except Exception:
                pass
            await page.wait_for_timeout(200)

        except Exception as exc:
            logger.debug("Modal trigger %d failed: %s", i, exc)

    return results


# ---------------------------------------------------------------------------
# 7. General Click Interactions
# ---------------------------------------------------------------------------
async def _capture_click_interactions(page, shots_dir: str) -> list[dict]:
    """Click buttons/CTAs and record resulting animations and DOM changes."""
    results = []

    targets = await page.evaluate("""
        () => {
            const out = [];
            const sels = 'button:not([class*="hamburger"]):not([class*="menu"]), [class*="cta"], [class*="btn"]:not(input), [role="button"]';
            document.querySelectorAll(sels).forEach(el => {
                if (out.length >= 8) return;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 15) return;
                if (r.y < -50 || r.y > window.innerHeight * 1.5) return;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return;
                out.push({
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().substring(0, 40),
                    classes: Array.from(el.classList).slice(0, 5),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2),
                });
            });
            return out;
        }
    """)

    for i, target in enumerate(targets or []):
        if i >= _MAX_CLICK_TARGETS:
            break
        try:
            # Count animations before click
            before_count = await page.evaluate("() => document.getAnimations().length")

            await page.mouse.click(target["x"], target["y"])
            await page.wait_for_timeout(400)

            # Check what happened
            after_state = await page.evaluate(f"""
                () => {{
                    const animCount = document.getAnimations().length;
                    // Check for new DOM elements (modals, toasts, tooltips)
                    const newElements = [];
                    const candidates = document.querySelectorAll(
                        '[class*="toast"], [class*="tooltip"], [class*="snackbar"], ' +
                        '[class*="notification"], [role="alert"], [role="status"]'
                    );
                    for (const el of candidates) {{
                        const cs = getComputedStyle(el);
                        if (cs.display !== 'none' && parseFloat(cs.opacity) > 0.1) {{
                            newElements.push({{
                                tag: el.tagName.toLowerCase(),
                                classes: Array.from(el.classList).slice(0, 5),
                                text: (el.textContent || '').trim().substring(0, 60),
                            }});
                        }}
                    }}
                    // Get new animations
                    const newAnims = [];
                    for (const a of document.getAnimations()) {{
                        if (a.playState === 'running') {{
                            const t = a.effect?.getTiming() || {{}};
                            newAnims.push({{
                                duration: t.duration,
                                easing: t.easing,
                                target_tag: a.effect?.target?.tagName?.toLowerCase(),
                            }});
                        }}
                    }}
                    return {{
                        animation_count: animCount,
                        new_elements: newElements,
                        running_animations: newAnims.slice(0, 5),
                    }};
                }}
            """)

            new_anim_count = (after_state.get("animation_count", 0) or 0) - (before_count or 0)

            result_type = "none"
            if after_state.get("new_elements"):
                result_type = "notification"
            elif new_anim_count > 0:
                result_type = "animation"

            if result_type != "none" or after_state.get("running_animations"):
                results.append({
                    "trigger_text": target.get("text"),
                    "trigger_classes": target.get("classes"),
                    "result_type": result_type,
                    "new_animations_fired": new_anim_count,
                    "running_animations": after_state.get("running_animations", []),
                    "new_elements": after_state.get("new_elements", []),
                })

        except Exception as exc:
            logger.debug("Click interaction %d failed: %s", i, exc)

    return results


# ---------------------------------------------------------------------------
# 8. Page Transitions — click-based navigation to capture SPA transitions
# ---------------------------------------------------------------------------
async def _find_clickable_internal_links(page) -> list[dict]:
    """Find visible internal links suitable for click-based navigation."""
    return await page.evaluate("""
        () => {
            const origin = window.location.origin;
            const current = window.location.pathname;
            const found = [];
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href]')) {
                if (found.length >= 5) break;
                try {
                    const url = new URL(a.href, origin);
                    if (url.origin !== origin) continue;
                    if (url.pathname === current || url.pathname === '/' || url.hash) continue;
                    if (seen.has(url.pathname)) continue;
                    const r = a.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) continue;
                    if (r.y < 0 || r.y > window.innerHeight * 2) continue;
                    const cs = getComputedStyle(a);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    seen.add(url.pathname);
                    found.push({
                        href: url.href,
                        pathname: url.pathname,
                        text: (a.textContent || '').trim().substring(0, 40),
                        x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2),
                    });
                } catch (e) {}
            }
            return found;
        }
    """) or []


async def _capture_single_transition(page, link: dict, shots_dir: str, index: int) -> dict:
    """Click a single internal link and record transition animations."""
    from_url = page.url
    to_url = link["href"]
    result = {
        "from_url": from_url,
        "to_url": to_url,
        "link_text": link.get("text"),
        "transition_type": "unknown",
        "exit_animations": [],
        "enter_animations": [],
        "dom_mutations": [],
        "total_duration_ms": None,
        "animation_duration_ms": None,
        "library_hooks": None,
    }

    try:
        # Set up animation polling and mutation observer before clicking
        await page.evaluate("""
            () => {
                window.__transitionCapture = {
                    startTime: performance.now(),
                    animSnapshots: [],
                    mutations: [],
                    routeChangeBefore: (window.__earlyHooks?.routeChanges || []).length,
                };
                // Poll animations during transition
                window.__transitionCapture._interval = setInterval(() => {
                    const anims = document.getAnimations().filter(a => a.playState === 'running');
                    if (anims.length > 0) {
                        for (const a of anims) {
                            window.__transitionCapture.animSnapshots.push({
                                name: a.animationName || a.id || null,
                                duration: a.effect?.getTiming()?.duration,
                                easing: a.effect?.getTiming()?.easing,
                                target_tag: a.effect?.target?.tagName?.toLowerCase(),
                                target_classes: Array.from(a.effect?.target?.classList || []).slice(0, 3),
                                currentTime: a.currentTime,
                                t: performance.now(),
                            });
                        }
                    }
                }, 50);
                // Watch for DOM content swap (childList on main/body)
                const mainEl = document.querySelector('main, [data-barba="container"], [class*="page-content"], [class*="transition-"], body');
                if (mainEl) {
                    window.__transitionCapture._observer = new MutationObserver((muts) => {
                        for (const m of muts) {
                            if (m.type === 'childList') {
                                for (const n of m.addedNodes) {
                                    if (n.nodeType === 1) {
                                        window.__transitionCapture.mutations.push({
                                            type: 'added', tag: n.tagName?.toLowerCase(),
                                            classes: Array.from(n.classList || []).slice(0, 4), t: performance.now()
                                        });
                                    }
                                }
                                for (const n of m.removedNodes) {
                                    if (n.nodeType === 1) {
                                        window.__transitionCapture.mutations.push({
                                            type: 'removed', tag: n.tagName?.toLowerCase(),
                                            classes: Array.from(n.classList || []).slice(0, 4), t: performance.now()
                                        });
                                    }
                                }
                            }
                        }
                    });
                    window.__transitionCapture._observer.observe(mainEl, { childList: true, subtree: false });
                }
                // Stop after 4 seconds max
                setTimeout(() => {
                    clearInterval(window.__transitionCapture._interval);
                    if (window.__transitionCapture._observer) window.__transitionCapture._observer.disconnect();
                }, 4000);
            }
        """)

        # Take before screenshot
        before_path = os.path.join(shots_dir, f"transition_{index}_before.png")
        await page.screenshot(path=before_path, full_page=False)

        # Click the link instead of page.goto()
        try:
            await page.mouse.click(link["x"], link["y"])
        except Exception:
            # Fallback: find by href and click
            el = await page.query_selector(f'a[href="{link["pathname"]}"], a[href="{link["href"]}"]')
            if el:
                await el.click()
            else:
                return None

        # Wait for navigation — either SPA URL change or hard navigation
        try:
            await page.wait_for_url(f'**{link["pathname"]}*', timeout=8000)
        except Exception:
            # May be a slow transition; wait a bit more
            await page.wait_for_timeout(2000)

        # Wait for animations to settle
        await page.wait_for_timeout(1500)

        # Collect transition data
        capture_data = await page.evaluate("""
            () => {
                clearInterval(window.__transitionCapture?._interval);
                if (window.__transitionCapture?._observer) window.__transitionCapture._observer.disconnect();
                const cap = window.__transitionCapture || {};
                const endTime = performance.now();
                // Deduplicate animation snapshots by name
                const seen = new Set();
                const uniqueAnims = [];
                for (const snap of (cap.animSnapshots || [])) {
                    const key = snap.name + '|' + snap.target_tag + '|' + snap.duration;
                    if (!seen.has(key)) { seen.add(key); uniqueAnims.push(snap); }
                }
                // Collect entry animations currently running
                const entryAnims = document.getAnimations().map(a => ({
                    name: a.animationName || a.id || null,
                    state: a.playState,
                    duration: a.effect?.getTiming()?.duration,
                    easing: a.effect?.getTiming()?.easing,
                    target_tag: a.effect?.target?.tagName?.toLowerCase(),
                    target_classes: Array.from(a.effect?.target?.classList || []).slice(0, 3),
                }));
                // Check for route changes via early hooks
                const routeChanges = (window.__earlyHooks?.routeChanges || []).slice(cap.routeChangeBefore || 0);
                const transitionLog = window.__earlyHooks?.transitionLog || [];
                // Determine transition type
                let transitionType = 'hard-navigation';
                if (routeChanges.length > 0) {
                    const types = routeChanges.map(r => r.type);
                    if (types.includes('navigation-api')) transitionType = 'spa-navigation-api';
                    else if (types.includes('pushState')) transitionType = 'spa-pushstate';
                    else if (types.includes('popstate')) transitionType = 'spa-popstate';
                }
                if (window.__earlyHooks?.transitionLibrary) {
                    transitionType = window.__earlyHooks.transitionLibrary.name || transitionType;
                }
                return {
                    transitionType,
                    animSnapshots: uniqueAnims.slice(0, 20),
                    entryAnims: entryAnims.slice(0, 15),
                    mutations: (cap.mutations || []).slice(0, 30),
                    durationMs: Math.round(endTime - (cap.startTime || endTime)),
                    routeChanges,
                    transitionLog: transitionLog.slice(-10),
                };
            }
        """) or {}

        # After screenshot
        after_path = os.path.join(shots_dir, f"transition_{index}_after.png")
        try:
            await page.screenshot(path=after_path, full_page=False)
        except Exception:
            after_path = None

        result["transition_type"] = capture_data.get("transitionType", "unknown")
        result["exit_animations"] = [
            a for a in capture_data.get("animSnapshots", [])
            if (a.get("t") or 0) < (capture_data.get("durationMs", 0) / 2 + (capture_data.get("animSnapshots", [{}])[0].get("t") or 0))
        ][:10]
        result["enter_animations"] = capture_data.get("entryAnims", [])[:10]
        result["dom_mutations"] = capture_data.get("mutations", [])
        result["total_duration_ms"] = capture_data.get("durationMs")
        result["library_hooks"] = capture_data.get("transitionLog") or None
        result["route_changes"] = capture_data.get("routeChanges", [])
        result["screenshots"] = {"before": before_path, "after": after_path}

        # Compute actual animation duration (not wall-clock navigation time)
        # total_duration_ms includes network latency + 1500ms settle wait + framework overhead
        exit_durations = [a.get("duration") or 0 for a in result["exit_animations"]]
        enter_durations = [a.get("duration") or 0 for a in result["enter_animations"]]
        max_exit = max(exit_durations) if exit_durations else 0
        max_enter = max(enter_durations) if enter_durations else 0
        animation_duration = max(max_exit, max_enter)
        if animation_duration <= 0:
            # Fallback: subtract settle wait, cap at reasonable default
            total = result["total_duration_ms"] or 0
            animation_duration = min(max(total - 1500, 0), 800)
        result["animation_duration_ms"] = round(animation_duration)

        return result

    except Exception as exc:
        logger.debug("Single transition capture failed for %s: %s", to_url, exc)
        return None


async def _capture_page_transition(page, current_url: str, pages: list, shots_dir: str):
    """
    Navigate to internal links using click-based navigation to capture
    SPA page transitions (Barba, Swup, Vue Router, etc.).

    Falls back to hard navigation if click-based approach fails.
    """
    # Gather candidate links
    if not pages:
        links = await _find_clickable_internal_links(page)
    else:
        # Convert pages list to link format
        links = []
        for p in pages[:3]:
            url = p if isinstance(p, str) else p.get("url", "")
            if url:
                links.append({"href": url, "pathname": urlparse(url).path, "text": "", "x": 0, "y": 0})
        # Also find clickable links on page for better coordinates
        page_links = await _find_clickable_internal_links(page)
        # Merge: prefer page_links that match our target URLs
        for pl in page_links:
            if not any(l["pathname"] == pl["pathname"] for l in links):
                links.append(pl)
        links = links[:5]

    if not links:
        return None

    transitions = []

    for i, link in enumerate(links[:2]):
        transition = await _capture_single_transition(page, link, shots_dir, i)
        if transition:
            transitions.append(transition)

        # Navigate back for next transition
        try:
            await page.go_back(timeout=10000)
            await page.wait_for_timeout(1000)
        except Exception:
            # Fallback: hard navigate back
            try:
                await page.goto(current_url, wait_until="networkidle", timeout=15000)
            except Exception:
                break

    if not transitions:
        return None

    # Return list if multiple, single dict if one (backward compatible)
    if len(transitions) == 1:
        return transitions[0]
    return transitions
