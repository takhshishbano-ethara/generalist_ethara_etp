"""
site_discoverer.py  –  Phase 1 of the Leviathon scraping pipeline.

Launches headless Chromium via Playwright, injects a tech-detection script,
auto-classifies the site into an SOP category, crawls internal links, and
returns a JSON-serialisable discovery dict.
"""

from __future__ import annotations

import sys
import os
import re
import logging
from pathlib import Path
from urllib.parse import urlparse, urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Config import – config.py lives one level above the modules/ package.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import (  # noqa: E402
    CATEGORIES,
    CATEGORY_RULES,
    LAMBDA_CHROMIUM_ARGS,
    PRIMARY_VIEWPORT,
    TECH_DETECTION,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_JS_INJECT_PATH = _PROJECT_ROOT / "scripts" / "inject_tech_detector.js"

# Maximum internal pages to collect.
_MAX_PAGES = 20

# Navigation / network-idle timeout (ms).
_NAV_TIMEOUT = 45_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_inject_script() -> str:
    """Read the tech-detector JS from disk (once per call)."""
    if not _JS_INJECT_PATH.exists():
        raise FileNotFoundError(
            f"Tech-detector JS not found at {_JS_INJECT_PATH}"
        )
    return _JS_INJECT_PATH.read_text(encoding="utf-8")


def _same_domain(base_url: str, candidate: str) -> bool:
    """Return True if *candidate* shares the same domain as *base_url*."""
    try:
        base_host = urlparse(base_url).netloc.lower()
        cand_host = urlparse(candidate).netloc.lower()
        return cand_host == base_host
    except Exception:
        return False


def _normalise_url(url: str) -> str:
    """Strip fragment and trailing slash for deduplication."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _detect_tech_from_raw(raw: dict) -> list[str]:
    """
    Merge signals from the injected-JS result and the TECH_DETECTION config
    to produce a flat list of confirmed technology keys.

    The JS result has:
      - globals:  {lib_key: [matched_global_names]}
      - scripts:  [full_script_urls]
      - dom_markers: {lib_key: [matched_selectors]}
      - libraries: {lib_key: {version, type, ...}}
      - meta: {canvas_count, webgl_contexts, horizontal_scroll_sections, ...}
    """
    detected: set[str] = set()
    globals_map: dict = raw.get("globals", {})
    scripts_list: list = raw.get("scripts", [])
    dom_markers_map: dict = raw.get("dom_markers", {})
    libraries_map: dict = raw.get("libraries", {})

    # 1. Anything the JS already surfaced in globals or dom_markers counts.
    for lib_key in globals_map:
        detected.add(lib_key)
    for lib_key in dom_markers_map:
        detected.add(lib_key)
    for lib_key in libraries_map:
        detected.add(lib_key)

    # 2. Cross-check script URLs against TECH_DETECTION patterns from config.
    scripts_lower = [s.lower() for s in scripts_list]
    for tech_key, patterns in TECH_DETECTION.items():
        for pat in patterns.get("script_patterns", []):
            if any(pat.lower() in src for src in scripts_lower):
                detected.add(tech_key)
                break

    # 3. Include bundled library detections (from inline script scanning).
    for lib in raw.get("bundled_libs", []):
        detected.add(lib)

    return sorted(detected)


def _build_tech_stack(raw: dict, detected_keys: list[str]) -> dict:
    """
    Build a {library: {version, type}} dict from the JS libraries map,
    filling in 'detected' for anything not already versioned.
    """
    libraries_map: dict = raw.get("libraries", {})
    stack: dict = {}
    for key in detected_keys:
        if key in libraries_map:
            entry = libraries_map[key]
            stack[key] = {
                "version": entry.get("version", "detected"),
                "type": entry.get("type", "unknown"),
            }
        else:
            # Derive a type hint from config if available.
            stack[key] = {"version": "detected", "type": "unknown"}
    return stack


def _classify_category(detected_keys: list[str], raw: dict) -> str:
    """
    Classify site into an SOP category based on detected runtime signals.

    Priority: 3D > Representation > SVG > Cool Transition > Normal Website.
    Each check uses actual JS globals, computed DOM state, and canvas
    measurements — never guesses.
    """
    meta: dict = raw.get("meta", {})
    libraries_map: dict = raw.get("libraries", {})
    key_set = set(detected_keys)

    # ---- 3D & WebGL (canvas must be a PRIMARY element, not decorative) ----
    # Tightened (May 2026): false-positive pattern was sites with small
    # Three.js logos / decorative WebGL backgrounds being labeled 3d_webgl.
    # New rule: require ALL FOUR signals together (3D lib + WebGL + actual
    # fullscreen canvas + meaningful area). Stripe-style decorative shaders
    # tend to be fullscreen but use no 3D library; the lib check filters
    # those out. The "no-lib fullscreen-canvas" fallback is gone — a
    # canvas without a 3D library is almost never a real 3D experience.
    has_three = "three_js" in key_set
    has_3d_lib = has_three or "babylon" in key_set or "pixi" in key_set
    has_webgl = meta.get("webgl_contexts", 0) > 0
    canvas_is_fullscreen = meta.get("canvas_is_fullscreen", False)
    largest_canvas_area = meta.get("largest_canvas_area", 0)
    if (has_3d_lib
            and has_webgl
            and canvas_is_fullscreen
            and largest_canvas_area > 1_500_000):
        return CATEGORIES["3d_webgl"]

    # ---- Representation Format (horizontal scroll / pinned / scrollytelling) ----
    # Tightened (May 2026): old rules were too permissive — sticky nav +
    # IntersectionObserver on a standard SaaS page would wrongly classify.
    # Kept: clear signals (horizontal scroll containers, pin+scrub at scale,
    # explicit scrollama-style data-step sections).
    # Removed: sticky_sections + io_targets noise combo (every modern site
    # hits this). translate_x + sticky_sections combo (false positives on
    # sticky-nav). Raised scrolly_steps + scroll_snap thresholds.
    horiz_inline = meta.get("horizontal_scroll_sections", 0)
    horiz_computed = meta.get("horizontal_scroll_computed", 0)
    has_horizontal_pin = meta.get("has_horizontal_pin", False)
    if horiz_inline > 0 or horiz_computed > 0 or has_horizontal_pin:
        return CATEGORIES["representation"]
    has_pin = meta.get("has_pin", False)
    has_scrub = meta.get("has_scrub", False)
    st_count = meta.get("scroll_trigger_count", 0)
    if has_pin and has_scrub and st_count >= 5:
        return CATEGORIES["representation"]
    scrolly_steps = meta.get("scrollytelling_step_count", 0)
    scroll_snap = meta.get("scroll_snap_count", 0)
    # Explicit scrollytelling pattern — raised from 3 to 5 (a 3-step
    # feature highlight is not a scrollytelling experience).
    if scrolly_steps >= 5:
        return CATEGORIES["representation"]
    # Scroll-snap gallery — raised from 3 to 5.
    if scroll_snap >= 5:
        return CATEGORIES["representation"]

    # ---- SVG & Vector Graphics ----
    # Tightened (May 2026): a single Lottie loading-spinner or 3 small
    # animated icons wrongly classified everyday sites as SVG. Now requires
    # multiple Lottie players OR a large animated-SVG count.
    has_lottie = meta.get("has_lottie_players", 0) >= 2 or (
        "lottie" in key_set and meta.get("has_lottie_players", 0) >= 1
    )
    has_svg_anim = meta.get("svg_animated_count", 0) >= 6
    has_draw_svg = (
        "DrawSVGPlugin" in libraries_map
        or "MorphSVGPlugin" in libraries_map
        or "draw_svg" in key_set
    )
    has_d3 = meta.get("d3_svg_count", 0) >= 5
    has_rive = "rive" in key_set
    has_anime_svg = "anime" in key_set and meta.get("has_svg_paths", False)
    # Tier A: Library signals — strong evidence of SVG animation site
    if has_lottie or has_svg_anim or has_draw_svg or has_d3 or has_rive or has_anime_svg:
        return CATEGORIES["svg_vector"]
    # Tier B: SVG density heuristic — requires meaningful on-screen SVG presence.
    # Key insight: small sites with navbar icons (stackoverflow) have many
    # SVG elements but small area; sites with hero illustrations (Airtable,
    # Duolingo) have large SVGs. Use size and large-count, not path count.
    #
    # Guard: If strong Cool Transition signals (Lenis, GSAP, page transitions)
    # are present, SVG density alone is insufficient — many "large" SVGs are
    # just logo/icon assets on scroll-driven sites (e.g., Rejouice with 25
    # SVGs that are Rivian/Behance/Awwwards badges).
    has_gsap_early = "gsap" in key_set
    has_lenis_early = "lenis" in key_set or "locomotive" in key_set
    has_page_transition_early = any(
        k in key_set or k in libraries_map
        for k in ("barba", "barba_js", "swup", "highway")
    )
    has_transition_signals = has_gsap_early or has_lenis_early or has_page_transition_early

    svg_area_ratio = meta.get("svg_area_ratio", 0)
    large_svg_count = meta.get("large_svg_count", 0)

    if not has_transition_signals:
        # At least one hero-sized SVG (>10% viewport) is the strongest signal
        if large_svg_count >= 1 and svg_area_ratio >= 0.2:
            return CATEGORIES["svg_vector"]
        # Or many large-ish SVGs covering meaningful area (no single hero, but gallery)
        if large_svg_count >= 5:
            return CATEGORIES["svg_vector"]

    # ---- Cool Transition (GSAP + scroll choreography OR smooth scroll) ----
    has_gsap = "gsap" in key_set
    has_scroll_trigger = (
        meta.get("has_scroll_trigger", False)
        or "ScrollTrigger" in libraries_map
    )
    has_lenis = "lenis" in key_set or "locomotive" in key_set
    has_page_transition = any(
        k in key_set or k in libraries_map
        for k in ("barba", "barba_js", "swup", "highway")
    )
    has_enough_triggers = (
        meta.get("scroll_trigger_count", 0) >= 3
        or meta.get("gsap_timeline_count", 0) >= 5
    )
    # GSAP + Lenis/Locomotive together = definitively Cool Transition
    if has_gsap and has_lenis:
        return CATEGORIES["cool_transition"]
    if has_gsap and has_scroll_trigger and has_enough_triggers:
        return CATEGORIES["cool_transition"]
    if has_gsap and has_page_transition:
        return CATEGORIES["cool_transition"]
    # Lenis alone (without GSAP) still signals a scroll-driven experience
    if has_lenis:
        return CATEGORIES["cool_transition"]

    # ---- Default ----
    return CATEGORIES["normal_website"]


async def _collect_internal_links(page, base_url: str) -> list[str]:
    """
    Extract same-domain links from the page.  Falls back gracefully on any
    error (e.g. detached frame).
    """
    links: set[str] = set()
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => a.href)",
        )
        for href in hrefs:
            if not href or href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            absolute = urljoin(base_url, href)
            if _same_domain(base_url, absolute):
                normalised = _normalise_url(absolute)
                links.add(normalised)
    except Exception as exc:
        logger.debug("Link extraction error: %s", exc)

    # Remove the base URL itself (already known).
    base_norm = _normalise_url(base_url)
    links.discard(base_norm)

    return sorted(links)[: _MAX_PAGES]


async def _try_sitemap(page, base_url: str) -> list[str]:
    """
    Attempt to fetch /sitemap.xml and extract <loc> URLs on the same domain.
    Returns an empty list on failure.
    """
    parsed = urlparse(base_url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    pages: list[str] = []
    try:
        response = await page.request.get(sitemap_url, timeout=10_000)
        if response.ok:
            text = await response.text()
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.IGNORECASE)
            for loc in locs:
                if _same_domain(base_url, loc):
                    pages.append(_normalise_url(loc))
    except Exception as exc:
        logger.debug("Sitemap fetch failed: %s", exc)
    return pages


async def _get_page_metadata(page) -> dict:
    """Return title and meta description from the current page."""
    title = ""
    description = ""
    try:
        title = await page.title() or ""
    except Exception:
        pass
    try:
        description = await page.eval_on_selector(
            'meta[name="description"]',
            "el => el.content",
        )
    except Exception:
        pass
    return {"title": title.strip(), "description": (description or "").strip()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def discover_site(url: str) -> dict:
    """
    Discover a website's technology stack, auto-classify it into an SOP
    category, and collect its internal page URLs.

    Parameters
    ----------
    url : str
        Fully-qualified URL (including scheme) of the site to analyse.

    Returns
    -------
    dict
        JSON-serialisable dictionary with keys:
            url, title, description, category, tech_stack,
            pages, raw_tech_detection
    """
    inject_js = _read_inject_script()

    result: dict = {
        "url": url,
        "title": "",
        "description": "",
        "category": CATEGORIES["normal_website"],
        "tech_stack": {},
        "pages": [],
        "raw_tech_detection": {},
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=LAMBDA_CHROMIUM_ARGS,
        )
        context = await browser.new_context(
            viewport={
                "width": PRIMARY_VIEWPORT["width"],
                "height": PRIMARY_VIEWPORT["height"],
            },
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(_NAV_TIMEOUT)

        # ---- Navigate (with domcontentloaded fallback + retry) ----
        nav_ok = False
        for attempt in range(2):
            try:
                await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT)
                nav_ok = True
                break
            except PlaywrightTimeout:
                logger.warning(
                    "networkidle timed out for %s – proceeding with partial load", url
                )
                nav_ok = True
                break
            except Exception as exc:
                logger.warning("Navigation attempt %d failed for %s: %s", attempt + 1, url, exc)
                if attempt == 0:
                    # Retry with fresh context + domcontentloaded
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser = await pw.chromium.launch(
                        headless=True,
                        args=LAMBDA_CHROMIUM_ARGS,
                    )
                    context = await browser.new_context(
                        viewport={"width": PRIMARY_VIEWPORT["width"], "height": PRIMARY_VIEWPORT["height"]},
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0.0.0 Safari/537.36"
                        ),
                    )
                    page = await context.new_page()
                    page.set_default_timeout(_NAV_TIMEOUT)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                        nav_ok = True
                        logger.info("Retry with domcontentloaded succeeded for %s", url)
                        break
                    except Exception as exc2:
                        logger.error("Retry navigation also failed for %s: %s", url, exc2)

        if not nav_ok:
            try:
                await browser.close()
            except Exception:
                pass
            result["error"] = f"Navigation failed after retries: {url}"
            return result

        # ---- Page metadata ----
        meta = await _get_page_metadata(page)
        result["title"] = meta["title"]
        result["description"] = meta["description"]

        # ---- Scroll down to trigger lazy GSAP/animation initialization ----
        try:
            await page.evaluate("""
            async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const h = document.body.scrollHeight;
                for (let y = 0; y <= h; y += window.innerHeight) {
                    window.scrollTo(0, y);
                    await delay(150);
                }
                window.scrollTo(0, 0);
                await delay(500);
            }
            """)
        except Exception:
            pass

        # ---- Scan inline script content for bundled library signatures ----
        bundled_libs: list[str] = []
        try:
            bundled_libs = await page.evaluate("""
            () => {
                const found = [];
                const inlineScripts = Array.from(document.querySelectorAll('script:not([src])'))
                    .map(s => s.textContent).join(' ');
                const allText = inlineScripts.substring(0, 200000);
                if (/gsap|ScrollTrigger|_gsap/.test(allText)) found.push('gsap');
                if (/[Ll]enis|smooth.?scroll.*lerp/.test(allText)) found.push('lenis');
                if (/[Bb]arba|data-barba/.test(allText)) found.push('barba');
                if (/lottie|bodymovin/.test(allText)) found.push('lottie');
                if (/ScrollSmoother/.test(allText)) found.push('scroll_smoother');
                if (/DrawSVG|MorphSVG/.test(allText)) found.push('draw_svg');
                if (/THREE\\./.test(allText)) found.push('three_js');
                return found;
            }
            """)
        except Exception:
            bundled_libs = []

        # ---- Inject tech detector ----
        raw_tech: dict = {}
        try:
            raw_tech = await page.evaluate(inject_js)
        except Exception as exc:
            logger.error("Tech-detection injection failed: %s", exc)
            raw_tech = {}

        # Merge bundled library detections into raw_tech
        if bundled_libs:
            raw_tech.setdefault("bundled_libs", bundled_libs)

        result["raw_tech_detection"] = raw_tech

        # ---- Derive detected techs, stack, and category ----
        detected_keys = _detect_tech_from_raw(raw_tech)
        result["tech_stack"] = _build_tech_stack(raw_tech, detected_keys)
        result["category"] = _classify_category(detected_keys, raw_tech)

        if raw_tech.get("platforms"):
            result["platforms"] = raw_tech["platforms"]
        if raw_tech.get("cms"):
            result["cms"] = raw_tech["cms"]
        if raw_tech.get("css_tools"):
            result["css_tools"] = raw_tech["css_tools"]

        # ---- Crawl internal links ----
        page_links = await _collect_internal_links(page, url)

        # ---- Try sitemap.xml for additional pages ----
        sitemap_links = await _try_sitemap(page, url)

        # Merge and deduplicate, respecting _MAX_PAGES limit.
        all_pages: list[str] = list(dict.fromkeys(page_links + sitemap_links))
        result["pages"] = all_pages[:_MAX_PAGES]

        await browser.close()

    return result
