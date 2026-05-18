"""
Phase 4: Screenshot & Asset Collection.
Captures reference screenshots and downloads multimedia assets via network interception.
"""

from __future__ import annotations

import logging
import os
import asyncio
import json
import re
import ssl
import urllib.request
from urllib.parse import urlparse, urljoin

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import PRIMARY_VIEWPORT, BREAKPOINTS, ASSET_MIME_TYPES

logger = logging.getLogger(__name__)

_FONT_MIME_TYPES = {
    "font/woff2", "font/woff", "font/ttf", "font/otf",
    "application/font-woff2", "application/font-woff",
    "application/x-font-ttf", "application/x-font-opentype",
    "application/vnd.ms-fontobject",
}
_FONT_EXTS = {".woff2", ".woff", ".ttf", ".otf", ".eot"}


def setup_font_intercept(page) -> dict:
    """Register a response listener that captures font file bytes during page load.

    Must be called BEFORE page.goto(). Returns a dict that will be populated
    as fonts are loaded: {url: bytes}.
    """
    intercepted: dict[str, bytes] = {}

    async def _on_response(response):
        try:
            url = response.url
            ct = (response.headers.get("content-type") or "").lower().split(";")[0].strip()
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path.split("?")[0])[1].lower()

            is_font = ct in _FONT_MIME_TYPES or ext in _FONT_EXTS
            if not is_font:
                return

            if not response.ok:
                return

            body = await response.body()
            if body and len(body) > 100:
                intercepted[url] = body
                logger.debug("Font intercepted during load: %s (%d bytes)", url, len(body))
        except Exception as exc:
            logger.debug("Font intercept handler error: %s", exc)

    page.on("response", _on_response)
    return intercepted


def setup_image_intercept(page) -> set:
    """Track image URLs seen during page load (network-level).

    Catches images loaded by JS, CSS, lazy loaders that DOM queries miss.
    Must be called BEFORE page.goto(). Returns a set of URLs.
    """
    _IMAGE_MIMES = {
        "image/png", "image/jpeg", "image/webp", "image/gif",
        "image/svg+xml", "image/avif", "image/bmp",
    }
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}
    seen_urls: set[str] = set()

    async def _on_response(response):
        try:
            url = response.url
            if not response.ok:
                return
            ct = (response.headers.get("content-type") or "").lower().split(";")[0].strip()
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path.split("?")[0])[1].lower()
            if ct in _IMAGE_MIMES or ext in _IMAGE_EXTS:
                # Only track URLs, don't store bytes (too much memory)
                seen_urls.add(url)
        except Exception:
            pass

    page.on("response", _on_response)
    return seen_urls


async def collect_assets(page, url: str, output_dir: str, pages: list = None,
                         intercepted_fonts: dict | None = None,
                         intercepted_image_urls: set | None = None) -> dict:
    """
    Capture screenshots and download assets from the site.

    Args:
        page: Playwright Page object (already navigated)
        url: The site URL
        output_dir: Base output directory for this site
        pages: List of discovered page URLs to capture

    Returns:
        dict with screenshot_paths, asset_inventory, asset_dir
    """
    screenshots_dir = os.path.join(output_dir, "screenshots")
    assets_dir = os.path.join(output_dir, "assets")
    for subdir in [screenshots_dir, assets_dir,
                   os.path.join(assets_dir, "images"),
                   os.path.join(assets_dir, "svgs"),
                   os.path.join(assets_dir, "fonts"),
                   os.path.join(assets_dir, "videos"),
                   os.path.join(assets_dir, "json")]:
        os.makedirs(subdir, exist_ok=True)

    result = {
        "screenshots": [],
        "assets": {
            "images": [],
            "svgs": [],
            "fonts": [],
            "videos": [],
            "json": [],
        },
    }

    # --- SCREENSHOT CAPTURE ---
    await _capture_screenshots(page, url, screenshots_dir, result, pages)

    # --- ASSET DOWNLOAD via network interception ---
    await _collect_network_assets(page, url, assets_dir, result, intercepted_fonts or {},
                                   intercepted_image_urls or set())

    return result


def _is_blank_screenshot(filepath):
    """Check if a screenshot is mostly solid black/blank."""
    try:
        from PIL import Image
        img = Image.open(filepath)
        w, h = img.size
        if w < 10 or h < 10:
            return True
        pixels = []
        for x, y in [(5, 5), (w-5, 5), (5, h-5), (w-5, h-5), (w//2, h//2),
                     (w//4, h//4), (3*w//4, h//4), (w//4, 3*h//4)]:
            p = img.getpixel((min(x, w-1), min(y, h-1)))
            if isinstance(p, (int, float)):
                pixels.append((p, p, p))
            elif isinstance(p, tuple) and len(p) >= 3:
                pixels.append(p[:3])
            else:
                pixels.append((0, 0, 0))
        if len(set(pixels)) <= 1:
            return True
        avg = sum(sum(p) for p in pixels) / (len(pixels) * 3)
        return avg < 5
    except Exception:
        return False


async def _screenshot_with_retry(page, path, *, full_page=False, element=None,
                                 label="", max_retries=1, base_wait=4):
    """Capture a screenshot; if it comes back blank/near-black, wait and retry.

    Returns True if a non-blank image was captured. A False result still leaves
    the last attempt on disk — _validate_all_images drops it before S3 upload if
    it is genuinely blank. Retries only fire on blank output, so well-behaved
    sites pay no extra time.
    """
    async def _shoot():
        try:
            if element is not None:
                await element.screenshot(path=path)
            else:
                await page.screenshot(path=path, full_page=full_page)
            return True
        except Exception as exc:
            logger.warning("Screenshot capture failed (%s): %s", label or path, exc)
            return False

    if not await _shoot():
        return False

    attempt = 0
    while max_retries > 0 and _is_blank_screenshot(path) and attempt < max_retries:
        attempt += 1
        wait = base_wait + attempt * 2
        logger.info("Screenshot blank (%s) — waiting %ds, retry %d/%d",
                    label or os.path.basename(path), wait, attempt, max_retries)
        await asyncio.sleep(wait)
        if not await _shoot():
            return False

    if _is_blank_screenshot(path):
        logger.warning("Screenshot still blank after %d retr%s: %s",
                       max_retries, "y" if max_retries == 1 else "ies",
                       label or os.path.basename(path))
        return False
    return True


async def _is_canvas_heavy(page):
    """Detect if the site is primarily a full-viewport canvas/WebGL experience."""
    return await page.evaluate("""
        () => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return false;
            const rect = canvas.getBoundingClientRect();
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            // Canvas covers >70% of viewport
            const coverage = (rect.width * rect.height) / (vw * vh);
            if (coverage < 0.7) return false;
            // Check if page has minimal scrollable DOM content
            const scrollH = document.documentElement.scrollHeight;
            const hasMinimalScroll = scrollH <= vh * 1.5;
            return hasMinimalScroll || coverage > 0.9;
        }
    """)


async def _capture_canvas_screenshots(page, screenshots_dir, result):
    """Capture screenshots for canvas/WebGL/game sites via interaction."""

    await page.set_viewport_size(PRIMARY_VIEWPORT)
    await asyncio.sleep(2)  # Extra wait for WebGL init

    # Screenshot 1: Initial state (start screen / loading) — WebGL needs render time
    path = os.path.join(screenshots_dir, "01_hero_desktop.png")
    await _screenshot_with_retry(page, path, label="01_hero_desktop.png (canvas)",
                                 max_retries=2, base_wait=5)
    result["screenshots"].append({
        "path": path,
        "category": "style_target",
        "description": "Initial canvas state - start/loading screen at 1920px",
    })

    # Try to interact: click center of canvas to start the experience
    canvas = await page.query_selector("canvas")
    if canvas:
        box = await canvas.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.mouse.click(cx, cy)
            await asyncio.sleep(2)

    # Screenshot 2: After first click (game started / intro state)
    path = os.path.join(screenshots_dir, "02_after_interaction.png")
    await _screenshot_with_retry(page, path, label="02_after_interaction.png (canvas)",
                                 max_retries=1, base_wait=4)
    result["screenshots"].append({
        "path": path,
        "category": "style_target",
        "description": "Canvas state after initial interaction / click-to-start",
    })

    # Navigate using keyboard to get different views
    # Try arrow keys and WASD for 3D navigation
    movements = [
        (["ArrowUp", "ArrowUp", "ArrowUp", "w", "w", "w"], 2.0, "03_forward_view.png", "Forward navigation state"),
        (["ArrowRight", "ArrowRight", "d", "d"], 1.5, "04_right_view.png", "Right turn / alternate angle"),
        (["ArrowLeft", "ArrowLeft", "ArrowLeft", "a", "a"], 1.5, "05_left_view.png", "Left turn / another perspective"),
        (["ArrowDown", "ArrowDown", "s", "s"], 1.5, "06_reverse_view.png", "Reverse / backward state"),
    ]

    for keys, wait, filename, desc in movements:
        for key in keys:
            await page.keyboard.press(key)
            await asyncio.sleep(0.15)
        await asyncio.sleep(wait)
        path = os.path.join(screenshots_dir, filename)
        await _screenshot_with_retry(page, path, label=filename, max_retries=1)
        result["screenshots"].append({
            "path": path,
            "category": "style_target",
            "description": desc,
        })

    # Screenshot 7: Try to find and capture UI overlays
    overlay = await page.evaluate("""
        () => {
            const els = document.querySelectorAll(
                '[class*="ui"], [class*="overlay"], [class*="hud"], ' +
                '[class*="menu"], [class*="panel"], [class*="info"], ' +
                '[class*="controls"], [class*="sidebar"]'
            );
            for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.width > 50 && r.height > 50) return true;
            }
            return false;
        }
    """)
    if overlay:
        ui_el = await _find_element_with_fallbacks(page, [
            "[class*='ui']", "[class*='overlay']", "[class*='hud']",
            "[class*='menu']", "[class*='panel']", "[class*='info']",
            "[class*='controls']",
        ])
        if ui_el:
            path = os.path.join(screenshots_dir, "07_ui_overlay.png")
            try:
                await ui_el.screenshot(path=path)
                result["screenshots"].append({
                    "path": path,
                    "category": "component",
                    "description": "UI overlay / HUD component on canvas",
                })
            except Exception:
                pass

    # Screenshot 8: Try pressing Escape or clicking for menu/pause screen
    await page.keyboard.press("Escape")
    await asyncio.sleep(1)
    path = os.path.join(screenshots_dir, "08_menu_state.png")
    await _screenshot_with_retry(page, path, label="08_menu_state.png", max_retries=1)
    result["screenshots"].append({
        "path": path,
        "category": "structural",
        "description": "Menu / pause state (after Escape key)",
    })

    # Screenshot 9: Mobile viewport
    await page.set_viewport_size({"width": BREAKPOINTS["mobile"]["width"],
                                   "height": BREAKPOINTS["mobile"]["height"]})
    await asyncio.sleep(1)
    path = os.path.join(screenshots_dir, "09_mobile_375px.png")
    await _screenshot_with_retry(page, path, label="09_mobile_375px.png (canvas)", max_retries=1)
    result["screenshots"].append({
        "path": path,
        "category": "structural",
        "description": "Mobile breakpoint (375px) canvas rendering",
    })

    # Reset viewport
    await page.set_viewport_size(PRIMARY_VIEWPORT)
    await asyncio.sleep(0.5)


async def _capture_screenshots(page, url, screenshots_dir, result, pages):
    """Capture 8-9 screenshots following SOP image ordering strategy."""

    # Set primary viewport
    await page.set_viewport_size(PRIMARY_VIEWPORT)
    await asyncio.sleep(1)

    # Detect canvas-heavy sites and use interaction-based capture
    if await _is_canvas_heavy(page):
        await _capture_canvas_screenshots(page, screenshots_dir, result)
        return

    # Wait for any preloader/splash to finish (hard-capped at 10s so a loader
    # that never flips a "hidden"/"loaded" class can't stall the whole phase)
    try:
        await page.evaluate("""
            () => new Promise(resolve => {
                const start = Date.now();
                const check = () => {
                    if (Date.now() - start > 10000) { resolve(); return; }
                    const loader = document.querySelector(
                        '[class*="preloader"],[class*="loader"],[class*="loading"],[class*="splash"]'
                    );
                    if (!loader || loader.style.display === 'none' ||
                        loader.style.opacity === '0' || loader.style.visibility === 'hidden' ||
                        loader.classList.contains('hidden') || loader.classList.contains('loaded')) {
                        resolve();
                    } else {
                        setTimeout(check, 500);
                    }
                };
                check();
            })
        """)
    except Exception:
        pass
    await asyncio.sleep(1)

    # Resolve total scroll height with a sane floor. An unrendered SPA can report
    # scrollHeight 0, which would collapse every scroll shot onto the hero.
    viewport_h = PRIMARY_VIEWPORT["height"]
    try:
        total_height = await page.evaluate(
            "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 0)"
        )
    except Exception:
        total_height = 0
    if not total_height or total_height < viewport_h:
        await asyncio.sleep(1.5)
        try:
            total_height = await page.evaluate(
                "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 0)"
            )
        except Exception:
            total_height = 0
    total_height = max(total_height or 0, viewport_h)

    # Shot plan in capture order: (filename, scroll_fraction, settle_s, category, description, retries)
    # 06 (footer) and 08 (full page) / 09 (mobile) are handled separately below.
    shots = [
        ("01_hero_desktop.png",      0.00, 1.5, "style_target", "Hero section - full above-the-fold at 1920px", 2),
        ("02_mid_content.png",       0.30, 1.5, "style_target", "Mid-page content section showing primary content area", 1),
        ("03_secondary_content.png", 0.60, 1.0, "style_target", "Secondary content section showing additional UI patterns", 1),
        ("04_nav_in_context.png",    0.15, 1.0, "component",    "Navigation bar visible in context at 15% scroll", 1),
        ("05_grid_section.png",      0.50, 1.0, "component",    "Content grid section in full viewport context", 1),
    ]

    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(1)
    for fname, frac, settle, category, desc, retries in shots:
        await page.evaluate(f"window.scrollTo(0, {int(total_height * frac)})")
        await asyncio.sleep(settle)
        path = os.path.join(screenshots_dir, fname)
        await _screenshot_with_retry(page, path, label=fname, max_retries=retries)
        result["screenshots"].append({"path": path, "category": category, "description": desc})

    # Screenshot 6: Footer section in context (bottom of page)
    footer_scroll = max(0, total_height - viewport_h)
    await page.evaluate(f"window.scrollTo(0, {footer_scroll})")
    await asyncio.sleep(1)
    path = os.path.join(screenshots_dir, "06_footer_section.png")
    await _screenshot_with_retry(page, path, label="06_footer_section.png", max_retries=1)
    result["screenshots"].append({
        "path": path,
        "category": "component",
        "description": "Footer section in full viewport context",
    })

    # Screenshot 7: Mid-scroll state (animation in progress)
    await page.evaluate(f"window.scrollTo(0, {int(total_height * 0.45)})")
    await asyncio.sleep(1.5)
    path = os.path.join(screenshots_dir, "07_mid_scroll_state.png")
    await _screenshot_with_retry(page, path, label="07_mid_scroll_state.png", max_retries=1)
    result["screenshots"].append({
        "path": path,
        "category": "structural",
        "description": "Mid-scroll state showing animated elements mid-transition",
    })

    # Screenshot 8: Full page overview (scaled) — no retry, full_page can be flaky
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)
    path = os.path.join(screenshots_dir, "08_full_page.png")
    if await _screenshot_with_retry(page, path, full_page=True,
                                    label="08_full_page.png", max_retries=0):
        result["screenshots"].append({
            "path": path,
            "category": "structural",
            "description": "Full page layout overview showing content hierarchy",
        })

    # Screenshot 9: Mobile breakpoint
    await page.set_viewport_size({"width": BREAKPOINTS["mobile"]["width"],
                                   "height": BREAKPOINTS["mobile"]["height"]})
    await asyncio.sleep(1)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)
    path = os.path.join(screenshots_dir, "09_mobile_375px.png")
    await _screenshot_with_retry(page, path, label="09_mobile_375px.png", max_retries=1)
    result["screenshots"].append({
        "path": path,
        "category": "structural",
        "description": "Mobile breakpoint (375px) showing responsive layout",
    })

    # Reset to primary viewport
    await page.set_viewport_size(PRIMARY_VIEWPORT)
    await asyncio.sleep(0.5)


async def _collect_network_assets(page, url, assets_dir, result, intercepted_fonts: dict = None,
                                  intercepted_image_urls: set = None):
    """Download assets discovered via network requests during page load."""
    intercepted_fonts = intercepted_fonts or {}
    intercepted_image_urls = intercepted_image_urls or set()
    parsed = urlparse(url)
    base_domain = parsed.netloc

    collected_urls = set()

    # Scroll entire page to trigger lazy loading before collecting URLs
    try:
        total_h = await page.evaluate("document.documentElement.scrollHeight")
        viewport_h = await page.evaluate("window.innerHeight")
        steps = min(20, max(3, int(total_h / viewport_h)))
        for i in range(steps):
            scroll_y = int((i / steps) * total_h)
            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
            await asyncio.sleep(0.3)
        # Scroll back to top
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # Collect asset URLs from the page
    asset_urls = await page.evaluate("""
    async () => {
        const assets = { images: [], svgs: [], fonts: [], videos: [], json: [] };

        // Images — prefer currentSrc (actual rendered source after srcset resolution)
        // over src (which may be a tiny LQIP/blur-up placeholder)
        document.querySelectorAll('img[src], img[data-src], img[srcset], picture source[srcset], img[data-lazy], img[data-original]').forEach(el => {
            let src;
            if (el.tagName === 'SOURCE') {
                // <picture><source srcset="..."> — pick highest resolution
                const srcset = el.getAttribute('srcset') || '';
                const candidates = srcset.split(',').map(s => s.trim().split(/[ \t]+/));
                // Sort by width descriptor (largest first), fallback to last entry
                candidates.sort((a, b) => (parseInt(b[1]) || 0) - (parseInt(a[1]) || 0));
                src = candidates[0]?.[0];
            } else {
                // <img> — currentSrc is the browser's chosen source from srcset
                src = el.currentSrc || el.dataset.src || el.dataset.lazy || el.dataset.original || el.src;
                // Also collect all srcset URLs to ensure we get full-size versions
                const srcset = el.getAttribute('srcset') || '';
                if (srcset) {
                    const candidates = srcset.split(',').map(s => s.trim().split(/[ \t]+/));
                    candidates.sort((a, b) => (parseInt(b[1]) || 0) - (parseInt(a[1]) || 0));
                    const best = candidates[0]?.[0];
                    if (best && best !== src) assets.images.push(best);
                }
            }
            if (src) assets.images.push(src);
        });

        // Background images from inline styles AND computed styles
        // Scoped to elements likely to have backgrounds (not all '*')
        const bgSelectors = 'div,section,header,footer,main,article,aside,figure,span,a,li,td,th,[class*="hero"],[class*="banner"],[class*="bg"],[class*="background"],[class*="cover"],[class*="image"],[class*="photo"],[class*="thumb"],[style*="background"]';
        document.querySelectorAll(bgSelectors).forEach(el => {
            try {
                const computed = getComputedStyle(el).backgroundImage;
                if (computed && computed !== 'none') {
                    const matches = computed.matchAll(/url\(["']?([^"')]+)["']?\)/g);
                    for (const m of matches) {
                        if (!m[1].startsWith('data:')) assets.images.push(m[1]);
                    }
                }
            } catch(e) {}
        });

        // SVGs
        document.querySelectorAll('img[src$=".svg"], object[data$=".svg"]').forEach(el => {
            const src = el.src || el.data;
            if (src) assets.svgs.push(src);
        });

        // Inline SVGs - capture full serialized content (skip trivial icons < 2 children)
        document.querySelectorAll('svg').forEach((svg, i) => {
            if (svg.children.length > 1 && i < 20) {
                const serialized = new XMLSerializer().serializeToString(svg);
                // Skip SVGs over 200KB (likely embedded illustrations that should be external)
                if (serialized.length <= 200000) {
                    assets.svgs.push('inline:' + serialized);
                }
            }
        });

        // Videos (including poster images)
        document.querySelectorAll('video source[src], video[src]').forEach(el => {
            const src = el.src;
            if (src) assets.videos.push(src);
        });
        document.querySelectorAll('video[poster]').forEach(el => {
            if (el.poster) assets.images.push(el.poster);
        });

        // Fonts — extract from @font-face rules in stylesheets
        const fontExts = ['.woff2', '.woff', '.ttf', '.otf', '.eot'];
        const urlRe = /url\\(["']?([^"')]+)["']?\\)/g;
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules) {
                    if (rule instanceof CSSFontFaceRule) {
                        const src = rule.style.getPropertyValue('src');
                        let m;
                        while ((m = urlRe.exec(src)) !== null) {
                            const u = m[1];
                            if (fontExts.some(ext => u.split('?')[0].toLowerCase().endsWith(ext))) {
                                assets.fonts.push(u);
                            }
                        }
                    }
                }
            } catch(e) {
                // Cross-origin stylesheets: fetch CSS text and parse font URLs
                try {
                    const href = sheet.href;
                    if (href && !href.startsWith('data:')) {
                        const resp = await fetch(href, { mode: 'cors', credentials: 'omit' });
                        if (resp.ok) {
                            const css = await resp.text();
                            const faceRe = /@font-face\\s*\\{[^}]+\\}/gi;
                            let faceMatch;
                            while ((faceMatch = faceRe.exec(css)) !== null) {
                                let um;
                                const innerUrlRe = /url\\(["']?([^"')]+)["']?\\)/g;
                                while ((um = innerUrlRe.exec(faceMatch[0])) !== null) {
                                    let fontUrl = um[1];
                                    if (fontUrl.startsWith('../') || fontUrl.startsWith('./') || !fontUrl.startsWith('http')) {
                                        fontUrl = new URL(fontUrl, href).href;
                                    }
                                    if (fontExts.some(ext => fontUrl.split('?')[0].toLowerCase().endsWith(ext))) {
                                        assets.fonts.push(fontUrl);
                                    }
                                }
                            }
                        }
                    }
                } catch(e2) {}
            }
        }

        // Font links from stylesheet link elements
        document.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
            if (link.href?.includes('fonts.googleapis.com') || link.href?.includes('fonts.gstatic.com')) {
                if (!assets.fonts.includes(link.href)) assets.fonts.push(link.href);
            }
        });

        // Preload font links
        document.querySelectorAll('link[rel="preload"][as="font"]').forEach(link => {
            if (link.href && !assets.fonts.includes(link.href)) assets.fonts.push(link.href);
        });

        // JSON — external sources
        document.querySelectorAll('script[type="application/json"][src]').forEach(el => {
            if (el.src) assets.json.push({ type: 'external', url: el.src });
        });
        document.querySelectorAll('lottie-player[src], [data-animation-path]').forEach(el => {
            const src = el.getAttribute('src') || el.getAttribute('data-animation-path');
            if (src) assets.json.push({ type: 'external', url: src });
        });

        // JSON — inline script tags with JSON content
        document.querySelectorAll('script[type="application/json"]:not([src]), script[type="application/ld+json"]').forEach((el, i) => {
            const text = (el.textContent || '').trim();
            if (text.length > 10 && text.length < 500000) {
                assets.json.push({ type: 'inline', name: el.type.replace('application/', '') + '_' + (i+1), content: text });
            }
        });

        // JSON — Webflow IX2 interaction data from runtime store
        try {
            if (window.Webflow && typeof window.Webflow.require === 'function') {
                const ix2 = window.Webflow.require('ix2');
                if (ix2 && ix2.store) {
                    const state = typeof ix2.store.getState === 'function' ? ix2.store.getState() : ix2.store;
                    if (state && state.ixData) {
                        assets.json.push({ type: 'inline', name: 'webflow_ix2_data', content: JSON.stringify(state.ixData) });
                    }
                }
            }
        } catch(e) {}

        // JSON — embedded data in non-src script tags (common: site config, page data)
        document.querySelectorAll('script:not([src]):not([type="application/json"]):not([type="application/ld+json"])').forEach((el, i) => {
            const text = (el.textContent || '').trim();
            if (text.startsWith('{') && text.endsWith('}') && text.length > 50 && text.length < 200000) {
                try {
                    JSON.parse(text);
                    assets.json.push({ type: 'inline', name: 'embedded_data_' + (i+1), content: text });
                } catch(e) {}
            }
        });

        // Meta assets: og:image, twitter:image, favicon, apple-touch-icon
        document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"], meta[property="og:image:url"]').forEach(el => {
            const url = el.getAttribute('content');
            if (url) assets.images.push(url);
        });
        document.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"]').forEach(el => {
            const href = el.getAttribute('href');
            if (href && !href.startsWith('data:')) assets.images.push(href);
        });

        return assets;
    }
    """)

    # Merge network-intercepted image URLs (catches JS/CSS-loaded images missed by DOM)
    dom_image_set = set(asset_urls.get("images", []))
    for iurl in intercepted_image_urls:
        if iurl not in dom_image_set and not iurl.startswith("data:"):
            asset_urls.setdefault("images", []).append(iurl)
    logger.info("Asset URLs: %d images (%d from network intercept), %d SVGs, %d fonts, %d videos",
                len(asset_urls.get("images", [])), len(intercepted_image_urls - dom_image_set),
                len(asset_urls.get("svgs", [])), len(asset_urls.get("fonts", [])),
                len(asset_urls.get("videos", [])))

    # Download images (up to 25) — skip LQIP placeholders (< 2KB)
    MIN_IMAGE_BYTES = 2048  # Skip blur-up/LQIP placeholder images
    for img_url in asset_urls.get("images", [])[:40]:  # Collect more, filter later
        if img_url.startswith("data:") or img_url in collected_urls:
            continue
        collected_urls.add(img_url)
        abs_url = urljoin(url, img_url)
        filename = _safe_filename(abs_url, "images")
        filepath = os.path.join(assets_dir, "images", filename)
        if await _download_asset(page, abs_url, filepath):
            # Skip tiny placeholder images (LQIP/blur-up patterns)
            file_size = os.path.getsize(filepath)
            if file_size < MIN_IMAGE_BYTES:
                os.remove(filepath)
                logger.debug("Skipped LQIP placeholder (%d bytes): %s", file_size, abs_url[:100])
                continue
            result["assets"]["images"].append({
                "url": abs_url,
                "path": filepath,
                "filename": filename,
            })
        if len(result["assets"]["images"]) >= 25:
            break

    # Save inline SVGs
    for i, svg_data in enumerate(asset_urls.get("svgs", [])):
        if svg_data.startswith("inline:"):
            svg_content = svg_data[7:]
            filename = f"inline_svg_{i+1}.svg"
            filepath = os.path.join(assets_dir, "svgs", filename)
            with open(filepath, "w") as f:
                f.write(svg_content)
            result["assets"]["svgs"].append({
                "url": "inline",
                "path": filepath,
                "filename": filename,
            })
        elif svg_data not in collected_urls:
            collected_urls.add(svg_data)
            abs_url = urljoin(url, svg_data)
            filename = _safe_filename(abs_url, "svgs")
            filepath = os.path.join(assets_dir, "svgs", filename)
            if await _download_asset(page, abs_url, filepath):
                result["assets"]["svgs"].append({
                    "url": abs_url,
                    "path": filepath,
                    "filename": filename,
                })

    # Download fonts — belt-and-suspenders: intercepted bytes first, then download fallback
    raw_font_urls = asset_urls.get("fonts", [])

    # Also include any intercepted font URLs not in the JS-extracted list
    for iurl in intercepted_fonts:
        if iurl not in raw_font_urls:
            raw_font_urls.append(iurl)

    # Resolve Google Fonts CSS links to actual binary font URLs
    resolved_font_urls: list[str] = []
    for font_url in raw_font_urls:
        if "fonts.googleapis.com/css" in font_url:
            binary_urls = await _resolve_google_fonts_css(page, font_url, url)
            resolved_font_urls.extend(binary_urls)
        else:
            resolved_font_urls.append(font_url)

    # Deduplicate by resolved absolute URL
    seen_font_urls: set[str] = set()
    deduped_font_urls: list[str] = []
    for font_url in resolved_font_urls:
        abs_url = urljoin(url, font_url)
        if abs_url not in seen_font_urls:
            seen_font_urls.add(abs_url)
            deduped_font_urls.append(font_url)

    for font_url in deduped_font_urls[:15]:
        if font_url in collected_urls:
            continue
        collected_urls.add(font_url)
        abs_url = urljoin(url, font_url)
        filename = _safe_filename(abs_url, "fonts")
        filepath = os.path.join(assets_dir, "fonts", filename)

        # Try 1: Use intercepted bytes (captured during page load — always works)
        downloaded = False
        source = "download"
        if abs_url in intercepted_fonts:
            try:
                with open(filepath, "wb") as f:
                    f.write(intercepted_fonts[abs_url])
                downloaded = True
                source = "network_intercept"
            except Exception as exc:
                logger.warning("Failed to write intercepted font %s: %s", abs_url, exc)

        # Try 2: Standard download (with retry fallback)
        if not downloaded:
            downloaded = await _download_asset(page, abs_url, filepath)

        result["assets"]["fonts"].append({
            "url": abs_url,
            "path": filepath if downloaded else None,
            "filename": filename if downloaded else None,
            "type": "google_fonts" if "gstatic.com" in abs_url or "googleapis" in font_url else "custom",
            "source": source if downloaded else "failed",
        })

    # Log font download summary
    font_succeeded = sum(1 for f in result["assets"]["fonts"] if f.get("path"))
    font_failed = sum(1 for f in result["assets"]["fonts"] if not f.get("path"))
    if font_failed:
        logger.warning("Font downloads: %d succeeded, %d failed", font_succeeded, font_failed)

    # Save JSON data files (external downloads + inline extractions)
    for json_entry in asset_urls.get("json", [])[:15]:
        if not isinstance(json_entry, dict):
            continue
        if json_entry.get("type") == "external":
            json_url = json_entry.get("url", "")
            if json_url in collected_urls:
                continue
            collected_urls.add(json_url)
            abs_url = urljoin(url, json_url)
            filename = _safe_filename(abs_url, "json")
            filepath = os.path.join(assets_dir, "json", filename)
            if await _download_asset(page, abs_url, filepath):
                result["assets"]["json"].append({
                    "url": abs_url,
                    "path": filepath,
                    "filename": filename,
                    "source": "external",
                })
        elif json_entry.get("type") == "inline":
            name = json_entry.get("name", "data")
            content = json_entry.get("content", "")
            if not content:
                continue
            filename = f"{name}.json"
            filepath = os.path.join(assets_dir, "json", filename)
            try:
                parsed = json.loads(content)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
            result["assets"]["json"].append({
                "url": "inline",
                "path": filepath,
                "filename": filename,
                "source": "inline",
            })

    # Download videos (up to 3)
    for vid_url in asset_urls.get("videos", [])[:3]:
        if vid_url.startswith("blob:") or vid_url in collected_urls:
            continue
        collected_urls.add(vid_url)
        abs_url = urljoin(url, vid_url)
        filename = _safe_filename(abs_url, "videos")
        filepath = os.path.join(assets_dir, "videos", filename)
        if await _download_asset(page, abs_url, filepath):
            result["assets"]["videos"].append({
                "url": abs_url,
                "path": filepath,
                "filename": filename,
            })


async def _resolve_google_fonts_css(page, css_url: str, page_url: str) -> list[str]:
    """Fetch Google Fonts CSS and extract actual .woff2 binary URLs."""
    font_urls: list[str] = []
    try:
        response = await page.request.get(css_url, timeout=10000, headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })
        if response.ok:
            css_text = (await response.body()).decode("utf-8", errors="ignore")
            url_re = re.compile(r'url\(([^)]+\.woff2?[^)]*)\)', re.IGNORECASE)
            for match in url_re.finditer(css_text):
                furl = match.group(1).strip("'\" ")
                if furl and not furl.startswith("data:"):
                    font_urls.append(furl)
    except Exception as exc:
        logger.debug("Google Fonts CSS parse failed for %s: %s", css_url, exc)
    return font_urls


async def _download_asset(page, url, filepath):
    """Download a single asset. Two attempts: Playwright fetch, then urllib fallback."""

    def _is_html_response(body: bytes) -> bool:
        """Detect HTML error pages served as image responses."""
        if len(body) < 50:
            return False
        head = body[:200].lower()
        return b'<!doctype html' in head or b'<html' in head or b'<head' in head

    # Attempt 1: Playwright page.request (uses browser cookies/session)
    try:
        response = await page.request.get(url, timeout=10000)
        if response.ok:
            body = await response.body()
            if len(body) > 0 and len(body) < 50_000_000 and not _is_html_response(body):
                with open(filepath, "wb") as f:
                    f.write(body)
                return True
        else:
            logger.debug("Asset download HTTP %d: %s", response.status, url[:120])
    except Exception as exc:
        logger.debug("Asset download attempt 1 failed: %s → %s", url[:120], exc)

    # Attempt 2: urllib with browser-like headers (handles hotlink protection)
    try:
        page_url = page.url
        parsed_origin = urlparse(page_url)
        origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": page_url,
            "Origin": origin,
            "Accept": "*/*",
        })
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            body = resp.read()
            if len(body) > 0 and len(body) < 50_000_000 and not _is_html_response(body):
                with open(filepath, "wb") as f:
                    f.write(body)
                return True
    except Exception as exc:
        logger.debug("Asset download attempt 2 failed: %s → %s", url[:120], exc)

    return False


async def _find_element_with_fallbacks(page, selectors):
    """Try multiple selectors, return the first match."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box and box["width"] > 10 and box["height"] > 10:
                    return el
        except Exception:
            pass
    return None


def _safe_filename(url, category):
    """Generate a safe filename from URL."""
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "asset"
    name = "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)
    if len(name) > 80:
        name = name[:80]
    if not name or name == "asset":
        import hashlib
        name = hashlib.md5(url.encode()).hexdigest()[:12]
    ext = os.path.splitext(name)[1]
    if not ext:
        if category == "images":
            name += ".png"
        elif category == "svgs":
            name += ".svg"
        elif category == "videos":
            name += ".mp4"
        elif category == "fonts":
            name += ".woff2"
        elif category == "json":
            name += ".json"
    return name
