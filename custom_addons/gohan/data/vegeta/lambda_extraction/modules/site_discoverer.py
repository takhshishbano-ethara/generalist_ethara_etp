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
    PRIORITY_CRAWL_PATHS,
    TECH_DETECTION,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_JS_INJECT_PATH = _PROJECT_ROOT / "scripts" / "inject_tech_detector.js"

# Maximum internal pages to collect.
_MAX_PAGES = 40

_PROBE_TIMEOUT_MS = 8_000

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
    # Tightened: require an actual 3D library (three/babylon/pixi with webgl)
    # OR a fullscreen canvas that also has meaningful render area.
    # Raw webgl contexts alone over-promote decorative Stripe-style shaders.
    has_three = "three_js" in key_set
    has_3d_lib = has_three or "babylon" in key_set or "pixi" in key_set
    has_webgl = meta.get("webgl_contexts", 0) > 0
    canvas_is_fullscreen = meta.get("canvas_is_fullscreen", False)
    largest_canvas_area = meta.get("largest_canvas_area", 0)
    canvas_is_primary = canvas_is_fullscreen or largest_canvas_area > 800_000
    if has_3d_lib and has_webgl and canvas_is_primary:
        return CATEGORIES["3d_webgl"]
    if has_webgl and canvas_is_fullscreen and largest_canvas_area > 1_200_000:
        return CATEGORIES["3d_webgl"]

    # ---- Representation Format (horizontal scroll / pinned / scrollytelling) ----
    horiz_inline = meta.get("horizontal_scroll_sections", 0)
    horiz_computed = meta.get("horizontal_scroll_computed", 0)
    has_horizontal_pin = meta.get("has_horizontal_pin", False)
    has_translate_x = meta.get("has_translate_x_container", False)
    if horiz_inline > 0 or horiz_computed > 0 or has_horizontal_pin:
        return CATEGORIES["representation"]
    has_pin = meta.get("has_pin", False)
    has_scrub = meta.get("has_scrub", False)
    st_count = meta.get("scroll_trigger_count", 0)
    if has_pin and has_scrub and st_count >= 5:
        return CATEGORIES["representation"]
    # Scrollytelling pattern: scrollama-style [data-step] elements
    scrolly_steps = meta.get("scrollytelling_step_count", 0)
    sticky_sections = meta.get("sticky_section_count", 0)
    scroll_snap = meta.get("scroll_snap_count", 0)
    io_targets = meta.get("io_target_count", 0)
    if scrolly_steps >= 3:
        return CATEGORIES["representation"]
    # Sticky-backdrop scrollytelling (no explicit data-step but heavy IO + sticky)
    if sticky_sections >= 2 and io_targets >= 10:
        return CATEGORIES["representation"]
    # Horizontal translateX scroll (e.g., Porsche) combined with sticky pinning
    if has_translate_x and sticky_sections >= 1:
        return CATEGORIES["representation"]
    # Scroll-snap galleries with multiple snap sections
    if scroll_snap >= 3:
        return CATEGORIES["representation"]

    # ---- SVG & Vector Graphics ----
    has_lottie = "lottie" in key_set or meta.get("has_lottie_players", 0) > 0
    has_svg_anim = meta.get("svg_animated_count", 0) >= 3
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


async def _fetch_sitemap_locs(page, sitemap_url: str, base_url: str, depth: int = 0) -> list[str]:
    if depth > 2:
        return []
    locs_out: list[str] = []
    try:
        response = await page.request.get(sitemap_url, timeout=_PROBE_TIMEOUT_MS)
        if not response.ok:
            return []
        text = await response.text()
    except Exception as exc:
        logger.debug("Sitemap fetch failed for %s: %s", sitemap_url, exc)
        return []

    nested_indexes = re.findall(
        r"<sitemap>\s*<loc>\s*(.*?)\s*</loc>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    for nested in nested_indexes[:10]:
        if _same_domain(base_url, nested):
            locs_out.extend(await _fetch_sitemap_locs(page, nested, base_url, depth + 1))

    page_locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.IGNORECASE)
    nested_set = set(nested_indexes)
    for loc in page_locs:
        if loc in nested_set:
            continue
        if _same_domain(base_url, loc):
            locs_out.append(_normalise_url(loc))
    return locs_out


async def _try_sitemap(page, base_url: str) -> list[str]:
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    pages: list[str] = []

    robots_sitemaps: list[str] = []
    try:
        robots_resp = await page.request.get(f"{origin}/robots.txt", timeout=_PROBE_TIMEOUT_MS)
        if robots_resp.ok:
            robots_text = await robots_resp.text()
            for line in robots_text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url and _same_domain(base_url, sm_url):
                        robots_sitemaps.append(sm_url)
    except Exception as exc:
        logger.debug("robots.txt fetch failed: %s", exc)

    candidates: list[str] = []
    seen: set[str] = set()
    for c in robots_sitemaps + [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]:
        if c not in seen:
            seen.add(c)
            candidates.append(c)

    for sm in candidates[:5]:
        pages.extend(await _fetch_sitemap_locs(page, sm, base_url))
        if len(pages) >= _MAX_PAGES * 4:
            break

    deduped: list[str] = list(dict.fromkeys(pages))
    return deduped


async def _try_priority_paths(page, base_url: str) -> list[str]:
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found: list[str] = []
    base_norm = _normalise_url(base_url)

    for path in PRIORITY_CRAWL_PATHS:
        candidate = f"{origin}{path}"
        normalised = _normalise_url(candidate)
        if normalised == base_norm or normalised in found:
            continue
        try:
            resp = await page.request.get(
                candidate,
                timeout=_PROBE_TIMEOUT_MS,
                max_redirects=2,
            )
        except Exception:
            continue
        if not resp:
            continue
        status = resp.status
        if status >= 400:
            continue
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and "text/html" not in ctype and "application/xhtml" not in ctype:
            continue
        final_url = resp.url or candidate
        if not _same_domain(base_url, final_url):
            continue
        found.append(_normalise_url(final_url))
    return found


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

        page_links = await _collect_internal_links(page, url)
        sitemap_links = await _try_sitemap(page, url)
        priority_links = await _try_priority_paths(page, url)

        all_pages: list[str] = list(
            dict.fromkeys(priority_links + page_links + sitemap_links)
        )
        result["pages"] = all_pages[:_MAX_PAGES]

        await browser.close()

    return result
