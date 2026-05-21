"""Leviathan Extraction Lambda — AWS Lambda handler.

Receives extraction requests via Function URL (AWS_IAM auth), runs the full
extraction pipeline, uploads artifacts to S3, and POSTs results back to the
Odoo webhook.

Environment Variables:
    LEVIATHAN_WEBHOOK_TOKEN — token sent to Odoo webhook (X-Leviathan-Token header)
    S3_BUCKET               — bucket for artifact storage
    S3_REGION               — AWS region for S3 (default: us-east-1)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("leviathan-extraction")
logger.setLevel(logging.INFO)

LEVIATHAN_WEBHOOK_TOKEN = os.environ.get("LEVIATHAN_WEBHOOK_TOKEN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

# Internal deadline: stop gracefully at 13 min to leave time for upload + callback
EXTRACTION_DEADLINE_SECONDS = 780


# =============================================================================
# AWS LAMBDA ENTRY POINT
# =============================================================================

def lambda_handler(event, context):
    """Lambda entry. Accepts (1) direct async invoke from Odoo with payload
    {url, job_id, callback_url}, (2) Function URL POST /api/v1/extract, or
    (3) GET /health. Modes 1+2 share _handle_extract."""
    is_direct_invoke = (
        not event.get("httpMethod")
        and not event.get("requestContext", {}).get("http", {}).get("method")
        and not event.get("rawPath")
        and "url" in event
        and "job_id" in event
        and "callback_url" in event
    )

    if is_direct_invoke:
        synthetic = {
            "body": json.dumps({
                "url": event["url"],
                "job_id": event["job_id"],
                "callback_url": event["callback_url"],
            }),
            "isBase64Encoded": False,
        }
        logger.info(
            "Direct async invoke for job_id=%s, url=%s",
            event.get("job_id"), event.get("url"),
        )
        return _handle_extract(synthetic)

    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "")
    )

    if http_method == "OPTIONS":
        return _cors_response(204, "")

    path = event.get("path") or event.get("rawPath", "")

    if path == "/api/v1/extract" and http_method == "POST":
        return _handle_extract(event)

    if path == "/health" and http_method == "GET":
        return _cors_response(200, json.dumps({"status": "ok", "version": "2.1.0"}))

    return _cors_response(404, json.dumps({"error": "Not found"}))


def _handle_extract(event):
    """Handle POST /api/v1/extract."""
    try:
        body = event.get("body", "{}")
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _cors_response(400, json.dumps({"error": "Invalid JSON body"}))

    url = payload.get("url")
    job_id = payload.get("job_id")
    callback_url = payload.get("callback_url")

    if not url or not job_id or not callback_url:
        return _cors_response(400, json.dumps({
            "error": "Missing required fields: url, job_id, callback_url"
        }))

    # Validate callback_url is HTTPS and not an internal/private address
    try:
        from urllib.parse import urlparse as _cb_parse
        cb_parsed = _cb_parse(callback_url)
        if cb_parsed.scheme not in ("https", "http"):
            return _cors_response(400, json.dumps({"error": "callback_url must be HTTP(S)"}))
        cb_host = cb_parsed.hostname or ""
        if cb_host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or cb_host.startswith("10.") or cb_host.startswith("192.168."):
            logger.warning("callback_url points to private address: %s", cb_host)
            return _cors_response(400, json.dumps({"error": "callback_url must not be a private address"}))
    except Exception:
        pass  # Best-effort validation

    try:
        result = asyncio.run(_run_extraction(url, int(job_id), callback_url))
        return _cors_response(202, json.dumps({
            "success": True,
            "extraction_id": f"ext-{job_id}",
            "message": "Extraction complete, callback sent"
        }))
    except Exception as exc:
        logger.exception("Extraction failed for job %s", job_id)
        _send_callback(callback_url, {
            "job_id": int(job_id),
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return _cors_response(202, json.dumps({
            "success": True,
            "extraction_id": f"ext-{job_id}",
            "message": "Extraction failed, callback sent with error"
        }))


# =============================================================================
# EXTRACTION PIPELINE
# =============================================================================

async def _run_extraction(url: str, job_id: int, callback_url: str):
    """Run the full extraction pipeline and POST results to callback_url."""
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

    from config import PRIMARY_VIEWPORT, LAMBDA_CHROMIUM_ARGS
    from modules.site_discoverer import discover_site
    from modules.style_extractor import extract_styles
    from modules.animation_extractor import extract_animations
    from modules.asset_collector import collect_assets, setup_font_intercept, setup_image_intercept
    from modules.responsive_analyzer import analyze_responsive
    from modules.network_analyzer import analyze_network, summarize_network
    from modules.performance_analyzer import analyze_performance, generate_performance_targets
    from modules.brand_extractor import extract_brand
    from modules.component_token_extractor import extract_component_tokens
    from modules.dark_mode_extractor import extract_dark_mode
    from modules.auth_extractor import extract_auth
    from modules.interaction_capture import capture_interactions
    from modules.webgl_extractor import extract_webgl
    from modules.wireframe_generator import generate_wireframes, generate_wireframe_screenshots
    from modules.codegen_exporter import export_codegen_files
    from modules.prd_writer import build_prd_prompt
    from modules.audio_extractor import setup_audio_intercept, extract_audio
    from modules.cursor_extractor import extract_cursor
    from modules.phase_gate import get_phase_config

    async def _setup_page_hooks(page):
        """Register all pre-navigation intercepts + addInitScript hooks on a page.

        Must be called BEFORE page.goto(). Returns
        (audio_intercept_state, font_intercept_state, image_intercept_urls).
        Used by both the initial navigation and the crash-recovery retry path so
        the retry does not silently lose WebGL/GSAP/audio/early-hook injection.
        """
        audio_state = {}
        if "audio_extraction" not in skip_phases:
            try:
                audio_state = setup_audio_intercept(page)
            except Exception as exc:
                logger.warning("[job=%s] Audio intercept setup failed: %s", job_id, exc)

        font_state = None
        try:
            font_state = setup_font_intercept(page)
        except Exception as exc:
            logger.warning("[job=%s] Font intercept setup failed: %s", job_id, exc)

        image_urls = set()
        try:
            image_urls = setup_image_intercept(page)
        except Exception as exc:
            logger.warning("[job=%s] Image intercept setup failed: %s", job_id, exc)

        scripts_dir = Path(__file__).parent / "scripts"

        # Early hooks: preloader lifecycle, SPA routes from first paint
        try:
            await page.add_init_script((scripts_dir / "inject_early_hooks.js").read_text())
        except Exception as exc:
            logger.warning("[job=%s] Early hooks injection failed: %s", job_id, exc)

        # WebGL screenshot fix: force preserveDrawingBuffer so canvas isn't black
        try:
            await page.add_init_script("""
                (() => {
                    const origGetContext = HTMLCanvasElement.prototype.getContext;
                    HTMLCanvasElement.prototype.getContext = function(type, attrs) {
                        if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
                            attrs = Object.assign({}, attrs || {}, {
                                preserveDrawingBuffer: true,
                                powerPreference: 'high-performance'
                            });
                        }
                        return origGetContext.call(this, type, attrs);
                    };
                })();
            """)
        except Exception as exc:
            logger.warning("[job=%s] WebGL preserveDrawingBuffer hook failed: %s", job_id, exc)

        # GSAP authoring hook: patches gsap.to()/timeline() at definition time
        try:
            await page.add_init_script((scripts_dir / "inject_gsap_authoring_hook.js").read_text())
        except Exception as exc:
            logger.warning("[job=%s] GSAP authoring hook injection failed: %s", job_id, exc)

        # Audio hook: Web Audio API wrapping, Howler/Tone.js detection
        if "audio_extraction" not in skip_phases:
            try:
                await page.add_init_script((scripts_dir / "inject_audio_extractor.js").read_text())
            except Exception as exc:
                logger.warning("[job=%s] Audio hook injection failed: %s", job_id, exc)

        return audio_state, font_state, image_urls

    async def _relaunch_if_dead(page, context, browser):
        """If the page/browser died mid-extraction (heavy WebGL renderer crash,
        anti-headless kill), relaunch a fresh browser, re-wire hooks and
        re-navigate so the screenshot/asset phases can still run.

        Returns (page, context, browser, alive). alive=False means recovery
        failed and the caller should finalize with whatever it has.
        """
        nonlocal audio_intercept_state, font_intercept_state, image_intercept_urls
        try:
            if not page.is_closed():
                return page, context, browser, True
        except Exception:
            pass
        logger.warning("[job=%s] Page/browser died mid-extraction — relaunching to salvage screenshots", job_id)
        for _obj in (page, context, browser):
            try:
                await _obj.close()
            except Exception:
                pass
        try:
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
            page.set_default_timeout(30_000)
            audio_intercept_state, font_intercept_state, image_intercept_urls = \
                await _setup_page_hooks(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                logger.warning("[job=%s] Relaunch navigation issue (continuing anyway): %s", job_id, exc)
            await page.wait_for_timeout(3000)
            logger.info("[job=%s] Browser relaunch succeeded", job_id)
            return page, context, browser, True
        except Exception as exc:
            logger.error("[job=%s] Browser relaunch failed: %s", job_id, exc)
            return page, context, browser, False

    async def _bounded(coro, timeout, label, default=None):
        """Run a phase coroutine under a hard wall-clock cap. A hung extractor
        (common on heavy WebGL/3D sites) can't stall the pipeline — on timeout
        the coroutine is cancelled and we move on with `default`.
        """
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[job=%s] %s exceeded %ds hard cap — skipping", job_id, label, timeout)
            return default
        except Exception as exc:
            logger.warning("[job=%s] %s failed: %s", job_id, label, exc)
            return default

    # Clean up stale /tmp junk from previous warm-start invocations BEFORE
    # allocating anything. Playwright and Chromium leave behind artifact,
    # profile and crashpad dirs that the Lambda runtime never reclaims — on a
    # warm container these accumulate until /tmp hits ENOSPC ("no space left on
    # device") and the browser (or even mkdtemp below) can't launch. One
    # container handles one invocation at a time, so anything here is dead.
    try:
        _junk_prefixes = ("lev-", "playwright", ".org.chromium.", ".com.google.Chrome")
        for entry in os.listdir("/tmp"):
            if not entry.startswith(_junk_prefixes):
                continue
            stale = os.path.join("/tmp", entry)
            try:
                if os.path.isdir(stale):
                    shutil.rmtree(stale, ignore_errors=True)
                else:
                    os.remove(stale)
            except Exception:
                pass
    except Exception:
        pass  # Non-critical cleanup

    # Reap orphan Chromium processes from prior failed invocations. On a warm
    # container, a previous invocation that crashed during launch can leave
    # zombie chrome/chromium/playwright_driver processes that accumulate
    # ~75 MB each and eventually exhaust container memory.
    try:
        import subprocess
        for pat in ("chromium", "chrome", "headless_shell", "playwright"):
            subprocess.run(
                ["pkill", "-9", "-f", pat],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
    except Exception:
        pass

    work_dir = tempfile.mkdtemp(prefix=f"lev-{job_id}-")
    start_time = time.time()
    deadline = start_time + EXTRACTION_DEADLINE_SECONDS

    def _over_deadline():
        return time.time() > deadline

    def _elapsed():
        return time.time() - start_time

    # EXTRACTION BEGIN banner — the first line CloudWatch shows for this
    # invocation. If a job is "stuck in extracting" on the Odoo side and
    # this line is ABSENT from CloudWatch for its job_id, the Lambda never
    # ran (the async-invoke event is still queued by AWS, or was dropped).
    logger.info(
        "[job=%s] ===== EXTRACTION BEGIN — url=%s deadline=%ds =====",
        job_id, url, EXTRACTION_DEADLINE_SECONDS,
    )

    try:
        _ensure_dirs(work_dir)
        raw_dir = os.path.join(work_dir, "raw_data")

        # Tell Odoo the Lambda has actually picked up this job. The watchdog
        # keys off last_heartbeat, so this distinguishes "running" from
        # "queued / never started" — fire-and-forget, never blocks extraction.
        _send_started_ping(callback_url, job_id)

        # ── Phase 1: Site Discovery ──────────────────────────────────────
        logger.info("[job=%s] (+%.1fs) Phase 1: Site Discovery — %s", job_id, _elapsed(), url)
        site_data = await discover_site(url)

        if site_data.get("error"):
            _send_callback(callback_url, {
                "job_id": job_id,
                "success": False,
                "error": f"Site discovery failed: {site_data['error']}",
            })
            return

        _save_json(site_data, os.path.join(raw_dir, "site_discovery.json"))
        logger.info("[job=%s] Category: %s, Title: %s", job_id, site_data.get("category"), site_data.get("title"))

        # ── Initialize containers ────────────────────────────────────────
        style_data = {}
        animation_data = {}
        webgl_data = {"detected": False}
        asset_data = {"screenshots": [], "assets": {"images": [], "svgs": [], "fonts": [], "videos": [], "json": []}}
        responsive_data = {}
        network_data = {}
        performance_data = {}
        auth_data = {"has_auth": False}
        brand_data = {}
        dark_mode_data = {}
        component_tokens = {}
        interaction_data = {}
        audio_data = {}
        cursor_data = {}

        # Phase gating (v2: intelligent routing per category)
        phase_config = get_phase_config(site_data)
        logger.info("[job=%s] Phase gate: preset=%s, skip=%s", job_id,
                    phase_config.get("preset"), phase_config.get("skip_phases", []))
        skip_phases = set(phase_config.get("skip_phases", []))

        async with async_playwright() as pw:
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
            page.set_default_timeout(30_000)

            # Pre-navigation setup: all intercepts + addInitScript hooks BEFORE goto() (v2 parity)
            audio_intercept_state, font_intercept_state, image_intercept_urls = \
                await _setup_page_hooks(page)

            # ── Phase 2: Network Interception ────────────────────────────
            logger.info("[job=%s] (+%.1fs) Phase 2: Network Interception", job_id, _elapsed())
            try:
                network_data_raw = await analyze_network(page, url)
            except Exception as exc:
                logger.warning("[job=%s] Network setup failed: %s", job_id, exc)
                network_data_raw = {}

            # Navigate (with domcontentloaded fallback on crash)
            nav_ok = False
            try:
                await page.goto(url, wait_until="networkidle", timeout=45_000)
                nav_ok = True
            except PlaywrightTimeout:
                logger.warning("[job=%s] networkidle timed out, proceeding", job_id)
                nav_ok = True
            except Exception as exc:
                nav_err = f"{type(exc).__name__}: {exc}"
                logger.warning("[job=%s] networkidle navigation failed: %s, retrying with domcontentloaded", job_id, nav_err)
                # Fresh browser + domcontentloaded fallback
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
                try:
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
                    page.set_default_timeout(30_000)
                    # Re-wire ALL hooks/intercepts on the fresh page (v2 parity —
                    # WebGL/GSAP/audio/early hooks must survive crash recovery)
                    audio_intercept_state, font_intercept_state, image_intercept_urls = \
                        await _setup_page_hooks(page)
                    # Network interception is page-bound — re-run it on the new page
                    try:
                        network_data_raw = await analyze_network(page, url)
                    except Exception:
                        network_data_raw = {}
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    nav_ok = True
                    logger.info("[job=%s] Retry with domcontentloaded succeeded", job_id)
                except Exception as exc2:
                    nav_err = f"{type(exc2).__name__}: {exc2}"
                    logger.error("[job=%s] Retry navigation also failed: %s", job_id, nav_err)

            if not nav_ok:
                try:
                    await browser.close()
                except Exception:
                    pass
                _send_callback(callback_url, {
                    "job_id": job_id,
                    "success": False,
                    "error": f"Navigation failed after retries: {nav_err}",
                })
                return

            # Post-navigation settle — guarded: a heavy site can kill the
            # renderer immediately after load, and an unguarded wait here would
            # abort the whole pipeline before crash recovery can run.
            try:
                await page.wait_for_timeout(2000)
            except Exception as exc:
                logger.warning("[job=%s] Post-nav settle wait failed: %s", job_id, exc)

            # Early crash recovery: if the browser died during/right after
            # navigation, relaunch NOW so the rest of the pipeline runs on a live
            # page instead of cascading TargetClosedError to the top-level handler.
            page, context, browser, _page_alive = await _relaunch_if_dead(page, context, browser)
            if not _page_alive:
                logger.warning("[job=%s] Browser unrecoverable after navigation — finalizing partial", job_id)
                try:
                    await browser.close()
                except Exception:
                    pass
                return await _finalize_and_callback(
                    job_id=job_id, callback_url=callback_url, work_dir=work_dir,
                    site_data=site_data, style_data=style_data,
                    animation_data=animation_data, responsive_data=responsive_data,
                    asset_data=asset_data, webgl_data=webgl_data,
                    network_data=network_data, performance_data=performance_data,
                    auth_data=auth_data, brand_data=brand_data,
                    component_tokens=component_tokens, dark_mode_data=dark_mode_data,
                    interaction_data=interaction_data, start_time=start_time,
                    partial=True,
                )

            # Extra wait for heavy rendering sites (WebGL, GSAP, video backgrounds)
            try:
                needs_extra_wait = await page.evaluate("""
                    () => {
                        const hasCanvas = !!document.querySelector('canvas');
                        const hasVideo = !!document.querySelector('video[autoplay], video[src]');
                        const hasGSAP = !!(window.gsap || window.GreenSockGlobals);
                        const hasThree = !!(window.THREE || window.AFRAME);
                        const hasLottie = !!document.querySelector('lottie-player, [data-lottie], svg[data-animation-path]');
                        const hasPreloader = !!document.querySelector(
                            '[class*="preloader"],[class*="loader"],[class*="loading"],[class*="splash"]'
                        );
                        return hasCanvas || hasVideo || hasGSAP || hasThree || hasLottie || hasPreloader;
                    }
                """)
                if needs_extra_wait:
                    logger.info("[job=%s] Heavy site detected, waiting extra 5s for rendering", job_id)
                    await page.wait_for_timeout(5000)
            except Exception:
                pass

            # ── SPA Nav Link Discovery (supplement Phase 1) ──────────────
            if len(site_data.get("pages", [])) < 3:
                try:
                    spa_links = await page.evaluate("""
                    () => {
                        const links = new Set();
                        const base = location.origin;
                        document.querySelectorAll('a[href], nav a, header a, [role="navigation"] a').forEach(a => {
                            try {
                                const href = a.href;
                                if (!href || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) return;
                                const url = new URL(href, base);
                                if (url.origin === base && url.pathname !== location.pathname) {
                                    links.add(url.origin + url.pathname.replace(/\\/$/, ''));
                                }
                            } catch(e) {}
                        });
                        return [...links];
                    }
                    """)
                    if spa_links:
                        existing = set(site_data.get("pages", []))
                        for link in spa_links[:20]:
                            existing.add(link)
                        parsed_url = urlparse(url)
                        base_norm = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path.rstrip('/')}"
                        existing.discard(base_norm)
                        existing.discard(url.rstrip("/"))
                        existing.discard(url)
                        site_data["pages"] = sorted(existing)[:20]
                        _save_json(site_data, os.path.join(raw_dir, "site_discovery.json"))
                        logger.info("[job=%s] SPA nav discovery: found %d links, total pages: %d",
                                    job_id, len(spa_links), len(site_data["pages"]))
                except Exception as exc:
                    logger.warning("[job=%s] SPA link discovery failed: %s", job_id, exc)

            # Summarize network
            try:
                network_data = summarize_network(network_data_raw)
                _save_json(network_data, os.path.join(raw_dir, "network_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Network summary failed: %s", job_id, exc)

            # ── Deadline check (between Phase 2 and 3) ──────────────
            if _over_deadline():
                logger.warning("[job=%s] Deadline reached after Phase 2, finalizing partial", job_id)
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                await browser.close()
                # Let _finalize_and_callback build the PRD prompt (it has all the
                # data and handles failures); matches the Phase 3/4 deadline paths.
                await _finalize_and_callback(
                    job_id=job_id, callback_url=callback_url, work_dir=work_dir,
                    site_data=site_data, style_data=style_data,
                    animation_data=animation_data, responsive_data=responsive_data,
                    asset_data=asset_data, webgl_data=webgl_data,
                    network_data=network_data, performance_data=performance_data,
                    auth_data=auth_data, brand_data=brand_data,
                    component_tokens=component_tokens, dark_mode_data=dark_mode_data,
                    interaction_data=interaction_data, start_time=start_time,
                    partial=True,
                )
                return

            # ── Phase 3: Style Extraction ────────────────────────────────
            logger.info("[job=%s] (+%.1fs) Phase 3: Style Extraction", job_id, _elapsed())
            try:
                style_data = await extract_styles(page)
                _save_json(style_data, os.path.join(raw_dir, "style_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Style extraction failed: %s", job_id, exc)

            # Brand
            try:
                brand_data = await extract_brand(page)
                _save_json(brand_data, os.path.join(raw_dir, "brand_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Brand extraction failed: %s", job_id, exc)

            # Dark mode
            try:
                dark_mode_data = await extract_dark_mode(page)
                if dark_mode_data.get("has_dark_mode"):
                    _save_json(dark_mode_data, os.path.join(raw_dir, "dark_mode_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Dark mode detection failed: %s", job_id, exc)

            # Component tokens
            try:
                component_tokens = await extract_component_tokens(page)
                _save_json(component_tokens, os.path.join(raw_dir, "component_tokens.json"))
            except Exception as exc:
                logger.warning("[job=%s] Component tokens failed: %s", job_id, exc)

            if _over_deadline():
                logger.warning("[job=%s] Deadline approaching after Phase 3, skipping remaining", job_id)
                for _closer in (page.close, context.close):
                    try:
                        await _closer()
                    except Exception:
                        pass
                await browser.close()
                return await _finalize_and_callback(
                    job_id=job_id, callback_url=callback_url, work_dir=work_dir,
                    site_data=site_data, style_data=style_data,
                    animation_data=animation_data, responsive_data=responsive_data,
                    asset_data=asset_data, webgl_data=webgl_data,
                    network_data=network_data, performance_data=performance_data,
                    auth_data=auth_data, brand_data=brand_data,
                    component_tokens=component_tokens, dark_mode_data=dark_mode_data,
                    interaction_data=interaction_data, start_time=start_time,
                    partial=True,
                )

            # ── Phase 4: Animation Extraction ────────────────────────────
            # Skipped for 3D/WebGL sites (phase_gate) — the DOM/GSAP/CDP scanner
            # destabilizes heavy WebGL renderers. Hard-capped everywhere else so
            # a hung extractor can't stall the pipeline.
            if "animation_extraction" not in skip_phases:
                logger.info("[job=%s] (+%.1fs) Phase 4: Animation Extraction", job_id, _elapsed())
                animation_data = await _bounded(
                    extract_animations(page), 180, "Animation extraction", default={}
                )
                if animation_data:
                    _save_json(animation_data, os.path.join(raw_dir, "animation_data.json"))
            else:
                logger.info("[job=%s] Phase 4: Animation Extraction skipped (preset=%s)",
                            job_id, phase_config.get("preset"))

            # WebGL (only for 3D sites) — the proper capture for 3D motion
            if site_data.get("category") == "3D & WebGL / Game":
                webgl_data = await _bounded(
                    extract_webgl(page), 90, "WebGL extraction", default=webgl_data
                )
                _save_json(webgl_data, os.path.join(raw_dir, "webgl_data.json"))

            # Auth
            try:
                auth_data = await extract_auth(page)
                _save_json(auth_data, os.path.join(raw_dir, "auth_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Auth detection failed: %s", job_id, exc)

            # Deep interaction capture (hard-capped — click-based nav can hang)
            interaction_data = await _bounded(
                capture_interactions(page, url, work_dir, site_data),
                180, "Interaction capture", default={}
            )
            if interaction_data:
                _save_json(interaction_data, os.path.join(raw_dir, "interaction_data.json"))

            # Audio extraction (v2)
            if "audio_extraction" not in skip_phases:
                try:
                    audio_data = await extract_audio(page, work_dir, intercepted_audio=audio_intercept_state)
                    if audio_data:
                        _save_json(audio_data, os.path.join(raw_dir, "audio_data.json"))
                except Exception as exc:
                    logger.warning("[job=%s] Audio extraction failed: %s", job_id, exc)

            # Cursor behavior extraction (v2)
            try:
                cursor_data = await extract_cursor(page)
                if cursor_data and cursor_data.get("has_custom_cursor"):
                    _save_json(cursor_data, os.path.join(raw_dir, "cursor_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Cursor extraction failed: %s", job_id, exc)

            if _over_deadline():
                logger.warning("[job=%s] Deadline approaching after Phase 4", job_id)
                for _closer in (page.close, context.close):
                    try:
                        await _closer()
                    except Exception:
                        pass
                await browser.close()
                return await _finalize_and_callback(
                    job_id=job_id, callback_url=callback_url, work_dir=work_dir,
                    site_data=site_data, style_data=style_data,
                    animation_data=animation_data, responsive_data=responsive_data,
                    asset_data=asset_data, webgl_data=webgl_data,
                    network_data=network_data, performance_data=performance_data,
                    auth_data=auth_data, brand_data=brand_data,
                    component_tokens=component_tokens, dark_mode_data=dark_mode_data,
                    interaction_data=interaction_data, start_time=start_time,
                    partial=True,
                )

            # If the browser died during the heavy Phase 4 work, relaunch so the
            # screenshot + asset phases (the core deliverable) can still run
            # instead of cascading TargetClosedError through every phase.
            page, context, browser, _page_alive = await _relaunch_if_dead(page, context, browser)
            if not _page_alive:
                logger.warning("[job=%s] Browser unrecoverable — finalizing with partial data", job_id)
                try:
                    await browser.close()
                except Exception:
                    pass
                return await _finalize_and_callback(
                    job_id=job_id, callback_url=callback_url, work_dir=work_dir,
                    site_data=site_data, style_data=style_data,
                    animation_data=animation_data, responsive_data=responsive_data,
                    asset_data=asset_data, webgl_data=webgl_data,
                    network_data=network_data, performance_data=performance_data,
                    auth_data=auth_data, brand_data=brand_data,
                    component_tokens=component_tokens, dark_mode_data=dark_mode_data,
                    interaction_data=interaction_data, start_time=start_time,
                    partial=True,
                )

            # ── Phase 5: Asset Collection ────────────────────────────────
            logger.info("[job=%s] (+%.1fs) Phase 5: Asset Collection", job_id, _elapsed())
            try:
                asset_data = await collect_assets(page, url, work_dir, site_data.get("pages", []),
                                                     intercepted_fonts=font_intercept_state,
                                                     intercepted_image_urls=image_intercept_urls)
                _save_json(asset_data, os.path.join(raw_dir, "asset_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Asset collection failed: %s", job_id, exc)

            # ── Phase 6: Responsive Analysis ─────────────────────────────
            logger.info("[job=%s] (+%.1fs) Phase 6: Responsive Analysis", job_id, _elapsed())
            try:
                responsive_data = await analyze_responsive(page, work_dir)
                _save_json(responsive_data, os.path.join(raw_dir, "responsive_data.json"))
            except Exception as exc:
                logger.warning("[job=%s] Responsive analysis failed: %s", job_id, exc)

            # ── Phase 7: Wireframes ──────────────────────────────────────
            if not _over_deadline():
                logger.info("[job=%s] (+%.1fs) Phase 7: Wireframes", job_id, _elapsed())
                try:
                    wireframe_data = await generate_wireframes(page, work_dir)
                    _save_json(wireframe_data, os.path.join(raw_dir, "wireframe_data.json"))
                except Exception as exc:
                    logger.warning("[job=%s] Wireframe generation failed: %s", job_id, exc)

            # ── Phase 8: Performance ─────────────────────────────────────
            if not _over_deadline():
                logger.info("[job=%s] (+%.1fs) Phase 8: Performance Analysis", job_id, _elapsed())
                try:
                    await page.set_viewport_size({"width": PRIMARY_VIEWPORT["width"], "height": PRIMARY_VIEWPORT["height"]})
                    perf_raw = await analyze_performance(page)
                    performance_data = generate_performance_targets(perf_raw)
                    _save_json(performance_data, os.path.join(raw_dir, "performance_data.json"))
                except Exception as exc:
                    logger.warning("[job=%s] Performance analysis failed: %s", job_id, exc)

            # ── Phase 8A: Codegen Export ─────────────────────────────────
            if not _over_deadline():
                try:
                    await export_codegen_files(
                        page, work_dir,
                        site_data=site_data,
                        style_data=style_data,
                        animation_data=animation_data,
                        responsive_data=responsive_data,
                        asset_data=asset_data,
                        network_data=network_data,
                        performance_data=performance_data,
                        brand_data=brand_data,
                        component_tokens=component_tokens,
                        dark_mode_data=dark_mode_data,
                    )
                except Exception as exc:
                    logger.warning("[job=%s] Codegen export failed: %s", job_id, exc)

            # ── Phase 8B: Lo-Fi Wireframe Screenshots (runs LAST, modifies DOM) ─
            if not _over_deadline():
                logger.info("[job=%s] (+%.1fs) Phase 8B: Lo-Fi Wireframe Screenshots", job_id, _elapsed())
                try:
                    wf_screenshots = await generate_wireframe_screenshots(page, work_dir)
                    logger.info("[job=%s] Wireframe PNGs: %d viewport(s)", job_id, len(wf_screenshots))
                except Exception as exc:
                    logger.warning("[job=%s] Wireframe screenshots failed: %s", job_id, exc)

            # Close page, context and browser explicitly — all guarded: the
            # browser may already be dead after the last phase, and an unguarded
            # close would abort a finished run before it can finalize.
            for _closer in (page.close, context.close, browser.close):
                try:
                    await _closer()
                except Exception:
                    pass

        # Merge network CMS/CDN data into site_data for richer PRD prompt
        if network_data.get("cms_detected"):
            site_data.setdefault("cms", {})
            for cms_name in network_data["cms_detected"]:
                site_data["cms"][cms_name] = {"evidence": "network CDN pattern"}
        if network_data.get("cdn_detected"):
            site_data.setdefault("cdn", network_data["cdn_detected"])

        # ── Phase 9: Build PRD Prompt ────────────────────────────────────
        logger.info("[job=%s] (+%.1fs) Phase 9: Building PRD prompt", job_id, _elapsed())
        prd_prompt = build_prd_prompt(
            site_data=site_data,
            style_data=style_data,
            animation_data=animation_data,
            responsive_data=responsive_data,
            asset_data=asset_data,
            webgl_data=webgl_data,
            network_data=network_data,
            performance_data=performance_data,
            auth_data=auth_data,
            brand_data=brand_data,
            component_tokens=component_tokens,
            dark_mode_data=dark_mode_data,
            output_dir=work_dir,
        )

        # ── Finalize & Callback ──────────────────────────────────────────
        await _finalize_and_callback(
            job_id=job_id, callback_url=callback_url, work_dir=work_dir,
            site_data=site_data, style_data=style_data,
            animation_data=animation_data, responsive_data=responsive_data,
            asset_data=asset_data, webgl_data=webgl_data,
            network_data=network_data, performance_data=performance_data,
            auth_data=auth_data, brand_data=brand_data,
            component_tokens=component_tokens, dark_mode_data=dark_mode_data,
            interaction_data=interaction_data, start_time=start_time,
            partial=False, prd_prompt_override=prd_prompt,
        )

    except Exception as exc:
        logger.exception("[job=%s] Pipeline failed", job_id)
        _send_callback(callback_url, {
            "job_id": job_id,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# =============================================================================
# FINALIZE: Upload to S3, send callback
# =============================================================================

async def _finalize_and_callback(
    job_id, callback_url, work_dir, site_data, style_data,
    animation_data, responsive_data, asset_data, webgl_data,
    network_data, performance_data, auth_data, brand_data,
    component_tokens, dark_mode_data, interaction_data, start_time,
    partial=False, prd_prompt_override=None,
):
    """Upload artifacts to S3 (if configured) and send callback to Odoo."""
    from modules.prd_writer import build_prd_prompt

    elapsed = time.time() - start_time
    logger.info("[job=%s] Finalizing (%.1fs elapsed, partial=%s)", job_id, elapsed, partial)

    # Build prd_prompt if not provided
    if prd_prompt_override:
        prd_prompt = prd_prompt_override
    else:
        try:
            prd_prompt = build_prd_prompt(
                site_data=site_data,
                style_data=style_data,
                animation_data=animation_data,
                responsive_data=responsive_data,
                asset_data=asset_data,
                webgl_data=webgl_data,
                network_data=network_data,
                performance_data=performance_data,
                auth_data=auth_data,
                brand_data=brand_data,
                component_tokens=component_tokens,
                dark_mode_data=dark_mode_data,
                output_dir=work_dir,
            )
        except Exception as exc:
            logger.warning("[job=%s] PRD prompt build failed: %s", job_id, exc)
            prd_prompt = ""

    # Collect raw data artifacts for callback
    artifacts = _collect_artifacts(work_dir)

    # Validate image integrity (remove corrupted files)
    _validate_all_images(work_dir)

    # Build SOP-compliant deliverables (Page Assets, _unused, References)
    try:
        _build_sop_deliverables(work_dir, site_data, brand_data, url=site_data.get("url", ""))
    except Exception as exc:
        logger.warning("[job=%s] SOP deliverables build failed: %s", job_id, exc)

    # Upload to S3 if configured
    screenshot_keys = []
    asset_keys = []
    if S3_BUCKET:
        try:
            screenshot_keys, asset_keys = _upload_to_s3(
                job_id, work_dir,
                hard_deadline=start_time + 870,  # 14.5 min — leave 30s for callback
            )
        except Exception as exc:
            logger.warning("[job=%s] S3 upload failed: %s", job_id, exc)

    # ── Success + transparency payload ──────────────────────────────────
    # The deliverable is the PRD prompt, NOT screenshots. A run is a SUCCESS
    # whenever it produced a usable prd_prompt — the tasker supplies/fixes
    # assets manually at review time. Missing/blank screenshots are surfaced
    # as `partial` + `warnings`, never as a hard failure. We only report
    # success=False when extraction yielded nothing usable for a PRD.
    ss_dir = os.path.join(work_dir, "screenshots")
    n_screenshots = 0
    if os.path.isdir(ss_dir):
        n_screenshots = sum(
            1 for f in os.listdir(ss_dir)
            if f.lower().endswith((".png", ".jpg", ".webp"))
            and os.path.getsize(os.path.join(ss_dir, f)) > 0
        )

    assets = (asset_data or {}).get("assets", {}) if isinstance(asset_data, dict) else {}
    extraction_summary = {
        "screenshots_usable": n_screenshots,
        "screenshot_keys": len(screenshot_keys),
        "asset_keys": len(asset_keys),
        "images": len(assets.get("images", []) or []),
        "svgs": len(assets.get("svgs", []) or []),
        "fonts": len(assets.get("fonts", []) or []),
        "videos": len(assets.get("videos", []) or []),
        "pages_discovered": len(site_data.get("pages", []) or []),
        "elapsed_seconds": round(elapsed, 1),
        "deadline_hit": partial,
    }

    # phase_log: derived from which phases produced data — zero instrumentation.
    def _ran(d):
        return "ran" if d else "empty"
    phase_log = {
        "site_discovery": _ran(site_data and not site_data.get("error")),
        "network": _ran(network_data),
        "style": _ran(style_data),
        "brand": _ran(brand_data),
        "animation": _ran(animation_data),
        "webgl": _ran(webgl_data and webgl_data.get("detected")),
        "auth": _ran(auth_data and auth_data.get("has_auth")),
        "interaction": _ran(interaction_data),
        "assets": _ran(assets),
        "responsive": _ran(responsive_data),
        "component_tokens": _ran(component_tokens),
        "dark_mode": _ran(dark_mode_data and dark_mode_data.get("has_dark_mode")),
    }

    logger.info(
        "[job=%s] (+%.1fs) Extraction phases complete — %s",
        job_id, time.time() - start_time,
        ", ".join(f"{k}:{v}" for k, v in phase_log.items()),
    )

    warnings = []
    if n_screenshots == 0:
        warnings.append(
            "0 usable screenshots captured — tasker should supply reference images manually"
        )
    elif n_screenshots < 4:
        warnings.append(f"Only {n_screenshots} usable screenshot(s) captured")
    if partial:
        warnings.append("Extraction hit the time deadline — some phases were skipped")

    # SUCCESS = a usable PRD prompt was produced. That is the deliverable.
    success = bool(prd_prompt and prd_prompt.strip())

    callback_payload = {
        "job_id": job_id,
        "success": success,
        "partial": partial or bool(warnings),
        "elapsed_seconds": round(elapsed, 1),
        "site_discovery": {
            "title": site_data.get("title", ""),
            "description": site_data.get("description", ""),
            "url": site_data.get("url", ""),
            "category": site_data.get("category", ""),
            "tech_stack": site_data.get("tech_stack", {}),
            "pages": site_data.get("pages", []),
        },
        "prd_prompt": prd_prompt,
        "artifacts": artifacts,
        "extraction_summary": extraction_summary,
        "phase_log": phase_log,
    }
    if warnings:
        callback_payload["warnings"] = warnings
        logger.info("[job=%s] Extraction succeeded with warnings: %s", job_id, warnings)
    if not success:
        callback_payload["error"] = (
            "Extraction produced no usable PRD prompt — site discovery or the "
            "pipeline failed to yield any data. Retry recommended."
        )
        logger.warning("[job=%s] No usable prd_prompt — reporting success=False", job_id)

    if screenshot_keys:
        callback_payload["screenshot_keys"] = screenshot_keys
    if asset_keys:
        callback_payload["asset_keys"] = asset_keys

    # Last line before handing back to Odoo. If this appears in CloudWatch
    # but the Odoo job is still stuck in `extracting`, the callback HTTP
    # POST is the failure point — check the `_send_callback` attempt logs
    # immediately below and the Odoo webhook/ALB logs.
    logger.info(
        "[job=%s] ===== EXTRACTION END (+%.1fs) — success=%s partial=%s "
        "prd_prompt=%dB screenshots=%d assets=%d — sending callback =====",
        job_id, time.time() - start_time, success, callback_payload["partial"],
        len(prd_prompt or ""), len(screenshot_keys), len(asset_keys),
    )
    _send_callback(callback_url, callback_payload)


def _upload_to_s3(job_id: int, work_dir: str, hard_deadline: float = 0) -> tuple[list, list]:
    """Upload screenshots and assets to S3. Returns (screenshot_keys, asset_keys).

    If hard_deadline > 0, stops uploading when time.time() exceeds it
    to avoid running past Lambda's 15-min hard timeout.
    """
    import boto3

    client = boto3.client("s3", region_name=S3_REGION)
    prefix = f"leviathan/{job_id}"
    screenshot_keys = []
    asset_keys = []

    def _time_left():
        return hard_deadline <= 0 or time.time() < hard_deadline

    # Upload screenshots
    ss_dir = os.path.join(work_dir, "screenshots")
    if os.path.isdir(ss_dir):
        for fname in sorted(os.listdir(ss_dir))[:10]:
            if not _time_left():
                logger.warning("S3 upload deadline reached during screenshots")
                break
            fpath = os.path.join(ss_dir, fname)
            if (os.path.isfile(fpath) and os.path.getsize(fpath) > 0
                    and fname.lower().endswith((".png", ".jpg", ".webp"))):
                key = f"{prefix}/screenshots/{fname}"
                try:
                    content_type = "image/png" if fname.endswith(".png") else "image/jpeg"
                    client.upload_file(fpath, S3_BUCKET, key, ExtraArgs={"ContentType": content_type})
                    screenshot_keys.append(key)
                except Exception as exc:
                    logger.warning("S3 upload failed for %s: %s", fname, exc)

    # Upload assets (preserve subfolder structure)
    assets_dir = os.path.join(work_dir, "assets")
    if os.path.isdir(assets_dir):
        for subdir in ["images", "svgs", "fonts", "videos", "logos", "json", "textures"]:
            sub_path = os.path.join(assets_dir, subdir)
            if not os.path.isdir(sub_path):
                continue
            for fname in sorted(os.listdir(sub_path)):
                if not _time_left():
                    logger.warning("S3 upload deadline reached during assets")
                    break
                fpath = os.path.join(sub_path, fname)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                    key = f"{prefix}/assets/{subdir}/{fname}"
                    try:
                        client.upload_file(fpath, S3_BUCKET, key)
                        asset_keys.append(key)
                    except Exception as exc:
                        logger.warning("S3 upload failed for %s: %s", fname, exc)

    # Upload raw_data as single JSON bundle
    raw_dir = os.path.join(work_dir, "raw_data")
    if os.path.isdir(raw_dir):
        bundle = {}
        for fname in os.listdir(raw_dir):
            fpath = os.path.join(raw_dir, fname)
            if os.path.isfile(fpath) and fname.endswith(".json"):
                try:
                    with open(fpath, "r") as f:
                        bundle[fname.replace(".json", "")] = json.load(f)
                except Exception:
                    pass
        if bundle:
            key = f"{prefix}/raw_data.json"
            try:
                client.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=json.dumps(bundle, default=str).encode("utf-8"),
                    ContentType="application/json",
                )
            except Exception as exc:
                logger.warning("S3 upload failed for raw_data.json: %s", exc)

    # Upload SOP deliverables (Page Assets, _unused, References)
    dlv_dir = os.path.join(work_dir, "deliverables")
    if os.path.isdir(dlv_dir):
        for root, _dirs, files in os.walk(dlv_dir):
            if not _time_left():
                logger.warning("S3 upload deadline reached during deliverables")
                break
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
                    continue
                rel = os.path.relpath(fpath, dlv_dir)
                key = f"{prefix}/deliverables/{rel}"
                try:
                    client.upload_file(fpath, S3_BUCKET, key)
                    asset_keys.append(key)
                except Exception as exc:
                    logger.warning("S3 upload failed for deliverable %s: %s", rel, exc)

    logger.info("[job=%s] S3 upload: %d screenshots, %d assets", job_id, len(screenshot_keys), len(asset_keys))
    return screenshot_keys, asset_keys


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def _send_callback(callback_url: str, payload: dict):
    """POST results back to the Odoo webhook with exponential backoff retry.

    Retries up to 5 times with exponential backoff (2, 4, 8, 16s waits = ~30s total).
    This covers EKS rolling updates and transient network issues.
    """
    headers = {"Content-Type": "application/json"}
    if LEVIATHAN_WEBHOOK_TOKEN:
        headers["X-Leviathan-Token"] = LEVIATHAN_WEBHOOK_TOKEN

    jid = payload.get("job_id")
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(callback_url, json=payload, headers=headers)
                logger.info(
                    "[job=%s] Callback returned %d (attempt %d/%d)",
                    jid, resp.status_code, attempt + 1, max_attempts,
                )
                if resp.status_code < 500:
                    # 2xx/3xx/4xx = Odoo received it. A 4xx (e.g. 401 bad
                    # token, 413 too large) is a DELIVERY that Odoo rejected
                    # — the job will stay stuck in `extracting`, so flag it.
                    if resp.status_code >= 400:
                        logger.error(
                            "[job=%s] Callback DELIVERED but Odoo rejected it "
                            "(%d) — job will stay stuck in `extracting`: %s",
                            jid, resp.status_code, resp.text[:300],
                        )
                    else:
                        logger.info("[job=%s] Callback delivered OK", jid)
                    return
        except Exception as exc:
            logger.warning(
                "[job=%s] Callback attempt %d/%d failed: %s",
                jid, attempt + 1, max_attempts, exc,
            )

        if attempt < max_attempts - 1:
            wait = 2 ** (attempt + 1)  # 2, 4, 8, 16s
            logger.info("[job=%s] Retrying callback in %ds...", jid, wait)
            time.sleep(wait)

    # Every attempt failed — Odoo never learned this extraction finished.
    # This is THE cause of a job stuck in `extracting` until the watchdog.
    logger.error(
        "[job=%s] ALL %d callback attempts FAILED to %s — job will hang in "
        "`extracting` until the Odoo watchdog times it out",
        jid, max_attempts, callback_url,
    )


def _send_started_ping(callback_url: str, job_id: int):
    """Fire-and-forget 'extraction started' ping to the Odoo webhook.

    Lets Odoo's watchdog measure real progress (last_heartbeat) instead of
    time-since-state-change, so a job that is merely *queued* isn't killed.
    Single short attempt — never blocks or fails the extraction.
    """
    headers = {"Content-Type": "application/json"}
    if LEVIATHAN_WEBHOOK_TOKEN:
        headers["X-Leviathan-Token"] = LEVIATHAN_WEBHOOK_TOKEN
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                callback_url,
                json={"job_id": job_id, "status": "started"},
                headers=headers,
            )
        logger.info("[job=%s] Started ping → %d", job_id, resp.status_code)
    except Exception as exc:
        logger.warning("[job=%s] Started ping failed (non-fatal): %s", job_id, exc)


def _collect_artifacts(work_dir: str) -> dict:
    """Collect raw_data JSON files as strings for the callback payload."""
    artifacts = {}
    raw_dir = os.path.join(work_dir, "raw_data")
    if not os.path.isdir(raw_dir):
        return artifacts

    for fname in sorted(os.listdir(raw_dir)):
        fpath = os.path.join(raw_dir, fname)
        if os.path.isfile(fpath) and fname.endswith(".json"):
            size = os.path.getsize(fpath)
            if size < 1 * 1024 * 1024:  # Skip JSON > 1MB
                with open(fpath, "r") as f:
                    artifacts[f"raw_data/{fname}"] = f.read()

    return artifacts


def _ensure_dirs(output_dir: str):
    for subdir in [
        "", "raw_data", "screenshots", "screenshots/responsive",
        "wireframes", "assets", "assets/images", "assets/svgs",
        "assets/fonts", "assets/videos", "assets/json",
        "deliverables", "deliverables/Page Assets",
        "deliverables/_unused", "deliverables/References",
    ]:
        os.makedirs(os.path.join(output_dir, subdir), exist_ok=True)


def _save_json(data: dict, path: str):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _cors_response(status_code: int, body: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
        "body": body,
    }


# =============================================================================
# IMAGE INTEGRITY VALIDATION
# =============================================================================

def _validate_all_images(work_dir: str):
    """Walk all image files and remove any that are corrupted or truncated."""
    IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    removed = 0
    checked = 0

    for root, _dirs, files in os.walk(work_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in IMG_EXTS:
                continue
            fpath = os.path.join(root, fname)
            checked += 1
            if not _is_valid_image(fpath):
                logger.warning("Removing corrupted image: %s", fpath)
                try:
                    os.remove(fpath)
                except OSError:
                    pass
                removed += 1

    if removed:
        logger.info("Image validation: %d checked, %d corrupted removed", checked, removed)


def _is_valid_image(filepath: str) -> bool:
    """Verify an image file is not corrupted/truncated/blank."""
    try:
        size = os.path.getsize(filepath)
        if size < 100:  # Too small to be a real image
            return False

        # Check magic bytes
        with open(filepath, "rb") as f:
            header = f.read(16)

        if not header:
            return False

        ext = os.path.splitext(filepath)[1].lower()

        # PNG: must start with PNG signature
        if ext == ".png":
            if header[:8] != b'\x89PNG\r\n\x1a\n':
                return False
            # Check for IEND chunk (file not truncated)
            with open(filepath, "rb") as f:
                f.seek(-12, 2)  # Last 12 bytes
                tail = f.read()
            if b'IEND' not in tail:
                logger.debug("PNG missing IEND: %s", filepath)
                return False

        # JPEG: must start with FF D8 and end with FF D9
        elif ext in (".jpg", ".jpeg"):
            if header[:2] != b'\xff\xd8':
                return False
            with open(filepath, "rb") as f:
                f.seek(-2, 2)
                tail = f.read()
            if tail != b'\xff\xd9':
                logger.debug("JPEG missing EOI marker: %s", filepath)
                return False

        # WebP: must start with RIFF....WEBP
        elif ext == ".webp":
            if header[:4] != b'RIFF' or header[8:12] != b'WEBP':
                return False

        # GIF: must start with GIF87a or GIF89a
        elif ext == ".gif":
            if header[:4] != b'GIF8':
                return False

        # Additional: try Pillow if available (catches more corruption)
        try:
            from PIL import Image
            img = Image.open(filepath)
            img.verify()  # Checks for structural corruption
        except ImportError:
            pass  # Pillow not available, header checks are sufficient
        except Exception:
            return False

        # Check for blank/solid-color images (black screenshots, loading placeholders)
        try:
            from PIL import Image
            img = Image.open(filepath)
            # Sample pixels from corners + center to detect solid color
            w, h = img.size
            if w > 10 and h > 10:
                pixels = []
                for x, y in [(5, 5), (w-5, 5), (5, h-5), (w-5, h-5), (w//2, h//2),
                             (w//4, h//4), (3*w//4, h//4), (w//4, 3*h//4), (3*w//4, 3*h//4)]:
                    pixels.append(img.getpixel((min(x, w-1), min(y, h-1))))
                # Normalize to RGB tuples
                rgb_pixels = []
                for p in pixels:
                    if isinstance(p, (int, float)):
                        rgb_pixels.append((p, p, p))
                    elif isinstance(p, tuple) and len(p) >= 3:
                        rgb_pixels.append(p[:3])
                    else:
                        rgb_pixels.append((0, 0, 0))
                # All same color = blank image
                if len(set(rgb_pixels)) == 1:
                    logger.info("Blank/solid-color image rejected: %s (color=%s)", filepath, rgb_pixels[0])
                    return False
                # Mostly dark (all pixels very low brightness) = black screenshot
                avg_brightness = sum(sum(p) for p in rgb_pixels) / (len(rgb_pixels) * 3)
                if avg_brightness < 5:  # Nearly pure black
                    logger.info("Near-black image rejected: %s (avg_brightness=%.1f)", filepath, avg_brightness)
                    return False
        except ImportError:
            pass  # No Pillow, skip blank detection
        except Exception:
            pass  # Don't reject on analysis failure

        return True
    except Exception:
        return False


# =============================================================================
# SOP DELIVERABLES BUILDER (V2 parity)
# =============================================================================

# Proprietary font → Google Fonts equivalent mapping
_GOOGLE_FONT_EQUIVALENTS = {
    "suisseintl": "Inter", "suisse intl": "Inter", "circular": "DM Sans",
    "gilroy": "Manrope", "avenir": "Nunito Sans", "futura": "Jost",
    "proxima nova": "Montserrat", "gotham": "Poppins",
    "brandon grotesque": "Raleway", "graphik": "Inter",
    "neue haas grotesk": "Inter", "aktiv grotesk": "Source Sans 3",
    "apercu": "IBM Plex Sans", "d-din": "Exo 2", "neue montreal": "Space Grotesk",
    "cabinet grotesk": "Outfit", "clash display": "Sora", "satoshi": "DM Sans",
    "general sans": "General Sans", "walsheim": "DM Sans",
    "canela": "Cormorant Garamond", "editorial new": "Playfair Display",
    "reckless neue": "Libre Baskerville", "freight": "Lora",
    "tiempos": "Source Serif 4", "recoleta": "Lora",
    "sf mono": "JetBrains Mono", "dank mono": "Fira Code",
    "operator mono": "JetBrains Mono", "roboto mono": "Roboto Mono",
    "neue haas": "Inter", "helvetica neue": "Inter",
    "acumin": "Source Sans 3", "cereal": "Nunito",
    "objektiv": "Inter", "gt walsheim": "DM Sans",
    "gt america": "Inter", "gt super": "Playfair Display",
    "pp neue montreal": "Space Grotesk", "pp mori": "DM Sans",
    "agrandir": "Sora", "matter": "Inter",
}

_SYSTEM_FONTS = {
    "ui-sans-serif", "system-ui", "sans-serif", "serif", "monospace",
    "arial", "helvetica", "times new roman", "georgia", "verdana",
    "tahoma", "trebuchet ms", "courier new", "cursive", "fantasy",
}


def _resolve_font_reference(style_data: dict) -> dict:
    """Map the site's primary font to a Google Fonts equivalent."""
    fonts = style_data.get("fonts", [])
    if not fonts:
        return {"family": "Inter", "google_font": "Inter",
                "google_fonts_url": "https://fonts.google.com/specimen/Inter",
                "source_type": "default_fallback"}

    primary = None
    for f in sorted(fonts, key=lambda x: x.get("count", 0), reverse=True):
        if f.get("family", "").lower() not in _SYSTEM_FONTS:
            primary = f
            break
    if not primary:
        primary = fonts[0]

    family = primary.get("family", "Inter")
    source = primary.get("source", "computed")

    if source == "Google Fonts":
        safe = family.replace(" ", "+")
        return {"family": family, "google_font": family,
                "google_fonts_url": f"https://fonts.google.com/specimen/{safe}",
                "source_type": "google_fonts_detected"}

    lookup = family.lower().replace("-", " ").strip()
    equivalent = _GOOGLE_FONT_EQUIVALENTS.get(lookup)
    if equivalent:
        safe = equivalent.replace(" ", "+")
        return {"family": family, "google_font": equivalent,
                "google_fonts_url": f"https://fonts.google.com/specimen/{safe}",
                "source_type": "mapped_equivalent"}

    return {"family": family, "google_font": family,
            "google_fonts_url": f"https://fonts.google.com/?query={family.replace(' ', '+')}",
            "source_type": "unresolved_passthrough"}


def _generate_text_logo_svg(brand_name: str, output_dir: str, font_family: str = "Inter") -> str | None:
    """Generate a copyright-free SVG text rendering of the brand name."""
    clean = brand_name.strip()
    if not clean:
        return None

    # Escape for SVG
    escaped = (clean.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))

    char_w = 28
    text_size = 48
    w = max(200, len(clean) * char_w + 40)
    h = 80

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"'
        f' width="{w}" height="{h}">\n'
        f"  <style>\n"
        f"    .brand-text {{\n"
        f"      font-family: '{font_family}', 'Inter', sans-serif;\n"
        f"      font-size: {text_size}px;\n"
        f"      font-weight: 600;\n"
        f"      fill: #0A0A0A;\n"
        f"      dominant-baseline: central;\n"
        f"    }}\n"
        f"  </style>\n"
        f'  <text x="20" y="{h // 2}" class="brand-text">{escaped}</text>\n'
        f"</svg>"
    )

    filepath = os.path.join(output_dir, "text_logo.svg")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg)
    return filepath


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_safe_svg(path: str) -> bool:
    """Check if an SVG is a safe decorative asset (not a logo/brand/favicon).

    Checks both filename patterns AND file content for brand/trademark indicators.
    """
    name = os.path.basename(path).lower()
    # Filename-based filtering
    if any(w in name for w in ("logo", "favicon", "brand", "icon-")):
        return False
    # Skip SVGs with known award-site / brand-name patterns (V2 parity)
    if any(w in name for w in ("awward", "behance", "webby", "fwa", "dribbble")):
        return False
    # Skip CDN hash names (e.g. Z0ck9pbqstJ970bM_name.svg)
    basename_ne = os.path.splitext(name)[0]
    if "_" in basename_ne:
        prefix = basename_ne.split("_")[0]
        if len(prefix) > 15 and prefix.isalnum():
            return False
    # Content-based filtering: check for brand text inside the SVG
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(10_000).lower()
        # Skip SVGs containing trademark / copyright indicators
        if any(marker in content for marker in (
            "trademark", "\u00a9",  # copyright symbol
            "\u00ae",  # registered trademark
            "\u2122",  # TM symbol
            "all rights reserved",
        )):
            return False
        # Skip SVGs that are essentially just a brand wordmark
        # (contain <text> with very few characters — likely a logo)
        import re
        text_els = re.findall(r"<text[^>]*>([^<]{1,30})</text>", content)
        if text_els and len(text_els) <= 2:
            # Small number of short text elements = likely a wordmark
            total_chars = sum(len(t.strip()) for t in text_els)
            if total_chars < 40:
                return False
    except Exception:
        pass  # Binary or unreadable — allow through
    return True


def _build_sop_deliverables(
    work_dir: str, site_data: dict, brand_data: dict, url: str = ""
):
    """Build SOP-compliant deliverables folder with copyright handling.

    Structure:
        deliverables/
            References/          — up to 10 unique screenshots (style, component, wireframe, interaction)
            Page Assets/         — up to 5 copyright-free files (text logo SVG, decorative SVG, stock/safe images)
            _unused/             — all potentially copyrighted extracted assets
            image_credits.json   — attribution and font mapping metadata
    """
    brand_data = brand_data or {}
    dlv_dir = os.path.join(work_dir, "deliverables")
    refs_dir = os.path.join(dlv_dir, "References")
    pa_dir = os.path.join(dlv_dir, "Page Assets")
    unused_dir = os.path.join(dlv_dir, "_unused")
    assets_dir = os.path.join(work_dir, "assets")

    for d in [refs_dir, pa_dir, unused_dir]:
        os.makedirs(d, exist_ok=True)

    # ---- Build References (up to 10 unique screenshots) ----
    _build_references(work_dir, refs_dir)

    # ---- Resolve font for copyright metadata ----
    style_path = os.path.join(work_dir, "raw_data", "style_data.json")
    style_data = {}
    if os.path.isfile(style_path):
        try:
            with open(style_path) as f:
                style_data = json.load(f)
        except Exception:
            pass
    font_ref = _resolve_font_reference(style_data)

    # ---- Build Page Assets (5 copyright-free files) ----
    selected = {}
    used_paths = set()

    # Slot 1: Generated text-logo SVG (copyright-free)
    brand_name = brand_data.get("site_name", "")
    if not brand_name:
        title = site_data.get("title", "")
        if title:
            brand_name = title.split(" - ")[0].split(" | ")[0].strip()
    if not brand_name:
        # Derive from URL
        parsed = urlparse(url)
        brand_name = parsed.netloc.replace("www.", "").split(".")[0].title()
    logo_path = _generate_text_logo_svg(brand_name, pa_dir, font_ref.get("google_font", "Inter"))
    if logo_path:
        selected["logo"] = os.path.basename(logo_path)

    # Collect all extracted asset files
    IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif")
    SVG_EXT = ".svg"
    all_files = []
    if os.path.isdir(assets_dir):
        for root, _dirs, files in os.walk(assets_dir):
            for fname in files:
                if fname.startswith("."):
                    continue
                all_files.append(os.path.join(root, fname))

    svgs = [p for p in all_files if p.lower().endswith(SVG_EXT)]
    rasters = [p for p in all_files if os.path.splitext(p)[1].lower() in IMG_EXTS]

    # Slot 2: Decorative SVG (skip logos, favicons, brand SVGs)
    inline_svgs = [s for s in svgs if os.path.basename(os.path.dirname(s)) == "svgs" and _is_safe_svg(s)]
    other_svgs = [s for s in svgs if s not in inline_svgs and _is_safe_svg(s)]
    for svg_path in inline_svgs + other_svgs:
        dst = os.path.join(pa_dir, os.path.basename(svg_path))
        shutil.copy2(svg_path, dst)
        selected["svg"] = os.path.basename(svg_path)
        used_paths.add(svg_path)
        break

    # Slots 3-4: Stock images (copyright-free sources only)
    # Try stock image fetcher first (needs API keys to be available)
    stock_downloaded = []
    image_source = "none"  # Track actual source for credits accuracy
    try:
        from modules.stock_image_fetcher import is_available, generate_queries, fetch_and_download, get_target_aspect_ratio
        if is_available():
            queries = generate_queries(site_data)
            aspect_range = get_target_aspect_ratio()
            stock_downloaded = fetch_and_download(queries, pa_dir, count=2, aspect_range=aspect_range)
            for i, sp in enumerate(stock_downloaded):
                slot = "content_image" if i == 0 else "content_image_2"
                selected[slot] = os.path.basename(sp)
            if stock_downloaded:
                image_source = "stock"
    except Exception as exc:
        logger.debug("Stock image fetch unavailable: %s", exc)

    # NO copyrighted raster fallback. If stock APIs are unavailable,
    # Page Assets will have fewer than 5 files rather than include
    # copyrighted website images. Extracted rasters go to _unused/.
    if not stock_downloaded:
        logger.info("No stock API keys available; skipping content images (copyright compliance)")

    # Backfill to 5 with extra safe SVGs
    extra_svgs = [s for s in (inline_svgs + other_svgs) if s not in used_paths and _is_safe_svg(s)]
    for svg_path in extra_svgs:
        if len(selected) >= 5:
            break
        dst = os.path.join(pa_dir, os.path.basename(svg_path))
        if not os.path.exists(dst):
            shutil.copy2(svg_path, dst)
            selected[f"svg_{len(selected) + 1}"] = os.path.basename(svg_path)
            used_paths.add(svg_path)

    # ---- Move all extracted assets to _unused/ (copyright-encumbered) ----
    for fpath in all_files:
        if fpath in used_paths:
            continue
        dst = os.path.join(unused_dir, os.path.basename(fpath))
        counter = 1
        while os.path.exists(dst):
            name, ext = os.path.splitext(os.path.basename(fpath))
            dst = os.path.join(unused_dir, f"{name}_{counter}{ext}")
            counter += 1
        shutil.copy2(fpath, dst)

    # ---- image_credits.json (accurate attribution) ----
    # Attribution reflects actual source, not a generic claim
    if image_source == "stock":
        img_attribution = "Photos: Pexels/Pixabay/Unsplash (free for commercial use)."
    else:
        img_attribution = "No stock images included. Content images omitted for copyright compliance."

    # Font license reflects actual resolution status
    font_source = font_ref["source_type"]
    if font_source in ("google_fonts_detected", "mapped_equivalent"):
        font_license = "SIL Open Font License 1.1"
        font_attribution = "Font: Google Fonts (SIL OFL 1.1)."
    elif font_source == "default_fallback":
        font_license = "SIL Open Font License 1.1"
        font_attribution = "Font: Inter via Google Fonts (SIL OFL 1.1)."
    else:
        # unresolved_passthrough — we cannot claim OFL for a proprietary font
        font_license = "Unknown — original font license applies"
        font_attribution = f"Font: {font_ref['family']} (proprietary, not redistributable). Google Fonts suggestion: {font_ref['google_font']}."

    # Read per-image credits from stock fetcher if it wrote them
    stock_credits_path = os.path.join(pa_dir, "image_credits.json")
    per_image_credits = []
    if os.path.isfile(stock_credits_path):
        try:
            with open(stock_credits_path) as f:
                existing = json.load(f)
            per_image_credits = existing.get("images", [])
        except Exception:
            pass

    credits = {
        "attribution": f"{img_attribution} {font_attribution}",
        "images": per_image_credits,
        "fonts": [{
            "original_family": font_ref["family"],
            "google_font": font_ref["google_font"],
            "google_fonts_url": font_ref["google_fonts_url"],
            "match_type": font_ref["source_type"],
            "license": font_license,
        }],
        "text_logo": {
            "file": selected.get("logo", "text_logo.svg"),
            "generated": True,
            "brand_name": brand_name,
            "note": "Generated SVG text. Not the original trademarked logo.",
        },
        "page_assets": selected,
    }
    credits_path = os.path.join(pa_dir, "image_credits.json")
    with open(credits_path, "w") as f:
        json.dump(credits, f, indent=2)

    logger.info("SOP deliverables built: %d Page Assets, %d References, %d _unused",
                len(selected),
                len(os.listdir(refs_dir)),
                len(os.listdir(unused_dir)))


def _build_references(work_dir: str, refs_dir: str):
    """Build up to 10 unique reference images from screenshots, wireframes, and interactions."""
    ss_dir = os.path.join(work_dir, "screenshots")
    wf_dir = os.path.join(work_dir, "wireframes")
    interact_dir = os.path.join(ss_dir, "interactions")
    resp_dir = os.path.join(ss_dir, "responsive")

    IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

    def _ls_images(directory):
        if not os.path.isdir(directory):
            return []
        return [os.path.join(directory, n) for n in sorted(os.listdir(directory))
                if os.path.isfile(os.path.join(directory, n)) and n.lower().endswith(IMG_EXTS)]

    # Categorize screenshots by naming convention
    style_kw = ["hero", "mid_content", "secondary", "forward_view", "right_view", "left_view", "reverse_view"]
    comp_kw = ["nav_in_context", "grid_section", "footer_section", "menu", "after_interaction", "ui_overlay"]
    wire_kw = ["wireframe", "mobile"]

    style, component, wireframe_pool = [], [], []
    for path in _ls_images(ss_dir):
        lower = os.path.basename(path).lower()
        if any(k in lower for k in comp_kw):
            component.append(path)
        elif any(k in lower for k in wire_kw):
            wireframe_pool.append(path)
        elif any(k in lower for k in style_kw) or lower.startswith(("01_", "02_", "03_")):
            style.append(path)
        else:
            style.append(path)

    for path in _ls_images(wf_dir):
        wireframe_pool.append(path)

    interactions = _ls_images(interact_dir)
    responsive = _ls_images(resp_dir)
    selected_md5s = set()
    idx = 1

    def _add_unique(candidates, max_count):
        nonlocal idx
        added = 0
        for src in candidates:
            if added >= max_count:
                break
            md5 = _file_md5(src)
            if md5 in selected_md5s:
                continue
            selected_md5s.add(md5)
            dst = os.path.join(refs_dir, f"{idx:02d}_{os.path.basename(src)}")
            shutil.copy2(src, dst)
            idx += 1
            added += 1

    # Slots 1-3: style targets
    _add_unique(style[:6], 3)
    if idx < 4:
        idx = 4

    # Slots 4-6: component closeups
    _add_unique(component[:6], 3)
    if idx < 7:
        idx = 7

    # Slots 7-8: wireframes
    _add_unique(wireframe_pool[:6], 2)
    if idx < 9:
        idx = 9

    # Slots 9-10: interactions / responsive
    _add_unique(interactions[:4], 2)

    # Backfill to 10
    current = len([f for f in os.listdir(refs_dir)
                   if os.path.isfile(os.path.join(refs_dir, f)) and not f.startswith(".")])
    if current < 10:
        backfill = responsive + [p for p in _ls_images(ss_dir) if p not in style and p not in component]
        _add_unique(backfill, 10 - current)
