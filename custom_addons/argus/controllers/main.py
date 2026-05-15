# -*- coding: utf-8 -*-
"""Argus video-preview controller.

Instagram's ``/reel/{code}/embed/`` iframe is *intentionally*
non-playable — clicking it bounces the user out to instagram.com.
For inline playback we have to grab the direct CDN video URL from
the public page and render our own ``<video>`` element pointed at it.

Extraction strategy
-------------------
We try, in order:

1. **yt-dlp** (when installed) — bulletproof.  yt-dlp handles
   Instagram's anti-bot dance, login-wall detection, mobile vs.
   desktop endpoints, signed CDN URLs, and the format-selection
   logic.  It's the same library that powers most "save Instagram
   video" sites and apps.  Soft dependency: if it's not installed
   we move on to step 2.

2. **Regex scraping** — fetch the public ``/embed/captioned/``
   page server-side and pluck a ``.mp4`` URL out of the ``og:video``
   meta tag or the page's ``"video_url"`` JSON.  Works for ~half of
   reels these days — Instagram increasingly serves a login wall
   instead of the public page to non-browser User-Agents.

3. **Fallback iframe** — when both fail, we render Instagram's own
   ``/embed/captioned/`` iframe with a clear yellow banner so the
   operator understands they can't play inline.  At least the
   thumbnail + caption shows.

To get reliable inline playback you should install ``yt-dlp``
inside the Odoo Python environment::

    pip install yt-dlp

It's listed under ``external_dependencies`` in the manifest as a
soft dep — Odoo won't refuse to install the module without it,
but extraction will fall back to the iframe.
"""

import json
import logging
import re
import time

import requests

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# yt-dlp soft import.  Installing this package upgrades preview success
# rate from "sometimes" to "almost always" — see module docstring.
# --------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    import yt_dlp  # type: ignore

    _HAS_YT_DLP = True
except ImportError:  # pragma: no cover
    yt_dlp = None  # type: ignore
    _HAS_YT_DLP = False


try:
    import instaloader  # type: ignore

    _HAS_INSTALOADER = True
except ImportError:
    instaloader = None  # type: ignore
    _HAS_INSTALOADER = False


# Realistic desktop-Chrome UA — Instagram serves a leaner login-walled
# HTML to obvious bot UAs.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# Order matters — earliest match wins.  We accept both escaped (``\/``)
# and unescaped slashes, with or without ``.mp4`` in the path, since
# Instagram's CDN URLs sometimes use signed paths without the extension.
_VIDEO_URL_PATTERNS = (
    # JSON-encoded "video_url" — most reliable for reels.
    re.compile(r'"video_url"\s*:\s*"(https:[^"]+)"'),
    # Open-Graph meta tags (server-rendered HTML).
    re.compile(
        r'<meta\s+property="og:video:secure_url"\s+content="([^"]+)"',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta\s+property="og:video"\s+content="([^"]+)"',
        re.IGNORECASE,
    ),
    # JSON-LD VideoObject payload.
    re.compile(r'"contentUrl"\s*:\s*"(https:[^"]+)"'),
    # Some templates use playable_url for the auto-play preview.
    re.compile(r'"playable_url"\s*:\s*"(https:[^"]+)"'),
)


_IG_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/([\w\-]{1,32})/?",
    re.IGNORECASE,
)


def _unescape(url):
    """Decode the encodings Instagram uses inline (JSON + HTML)."""
    return (
        url.replace(r"&", "&")
        .replace(r"\/", "/")
        .replace("&amp;", "&")
    )


# ==========================================================================
# Extraction
# ==========================================================================
def _fetch_via_yt_dlp(canonical_url):
    if not _HAS_YT_DLP:
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        "writeinfojson": False,
        "writethumbnail": False,
        "extractor_args": {"instagram": {"comment_count": ["0"]}},
        "socket_timeout": 12,
        "http_headers": {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        },
    }
    cookies_path = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("argus.yt_dlp_cookies_path")
    )
    if cookies_path:
        opts["cookiefile"] = cookies_path
    cookies_browser = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("argus.yt_dlp_cookies_from_browser")
    )
    if cookies_browser and not cookies_path:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
    except Exception as exc:
        _logger.warning(
            "Argus preview: yt-dlp failed on %s: %s",
            canonical_url, exc,
        )
        return None
    if not info:
        return None
    url = info.get("url")
    if url:
        return url
    for entry in info.get("entries") or []:
        if entry and entry.get("url"):
            return entry["url"]
    return None


def _fetch_via_scrape(shortcode):
    """Last-resort regex scrape of the public Instagram page.

    Instagram has been increasingly aggressive about login-walling
    bot User-Agents; this works for a portion of reels but is not
    reliable enough on its own.  We try the ``embed/captioned/``
    variant first because that endpoint is specifically designed for
    public consumption and is the most likely to be unwalled.
    """
    candidates = [
        f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/reel/{shortcode}/embed/",
        f"https://www.instagram.com/p/{shortcode}/embed/",
        f"https://www.instagram.com/reel/{shortcode}/",
        f"https://www.instagram.com/p/{shortcode}/",
    ]
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    for url in candidates:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as exc:
            _logger.warning(
                "Argus preview: scrape fetch failed for %s: %s",
                url, exc,
            )
            continue
        if resp.status_code != 200 or not resp.text:
            _logger.info(
                "Argus preview: %s returned HTTP %s (len=%s)",
                url, resp.status_code, len(resp.text or ""),
            )
            continue
        for pattern in _VIDEO_URL_PATTERNS:
            match = pattern.search(resp.text)
            if match:
                video_url = _unescape(match.group(1))
                _logger.info(
                    "Argus preview: scrape OK via %s (pattern=%s)",
                    url, pattern.pattern[:40],
                )
                return video_url
        _logger.info(
            "Argus preview: %s returned a page but no video URL "
            "matched (login wall?)",
            url,
        )
    return None


def _fetch_via_remote(shortcode, source_url):
    remote_url = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("argus.remote_extractor_url")
    )
    if not remote_url:
        return None
    canonical = source_url or f"https://www.instagram.com/reel/{shortcode}/"
    endpoint = remote_url.rstrip("/") + "/extract"
    try:
        resp = requests.post(
            endpoint,
            json={"url": canonical},
            timeout=15,
        )
    except requests.RequestException as exc:
        _logger.warning(
            "Argus preview: remote extractor %s unreachable: %s",
            endpoint, exc,
        )
        return None
    if resp.status_code != 200:
        _logger.info(
            "Argus preview: remote extractor returned HTTP %s",
            resp.status_code,
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data.get("video_url") or None
    return None


def _fetch_via_instaloader(shortcode):
    if not _HAS_INSTALOADER:
        return None
    try:
        loader = instaloader.Instaloader(
            download_videos=False,
            download_video_thumbnails=False,
            download_pictures=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            sleep=False,
            quiet=True,
        )
        params = request.env["ir.config_parameter"].sudo()
        session_path = params.get_param("argus.instaloader_session_path")
        session_user = params.get_param("argus.instaloader_session_user")
        username = params.get_param("argus.instaloader_username")
        password = params.get_param("argus.instaloader_password")

        logged_in = False
        if session_path and session_user:
            try:
                loader.load_session_from_file(session_user, session_path)
                logged_in = True
            except Exception as exc:
                _logger.warning(
                    "Argus preview: instaloader session load failed (%s): %s",
                    session_path, exc,
                )

        if not logged_in and username and password:
            try:
                loader.login(username, password)
                logged_in = True
                if session_path:
                    try:
                        loader.save_session_to_file(session_path)
                        if not session_user:
                            params.set_param(
                                "argus.instaloader_session_user", username
                            )
                    except Exception as exc:
                        _logger.warning(
                            "Argus preview: instaloader session save failed (%s): %s",
                            session_path, exc,
                        )
            except Exception as exc:
                _logger.warning(
                    "Argus preview: instaloader login failed for %s: %s",
                    username, exc,
                )

        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        if not post.is_video:
            return None
        return post.video_url or None
    except Exception as exc:
        _logger.warning(
            "Argus preview: instaloader failed on %s: %s",
            shortcode, exc,
        )
        return None


_VIDEO_URL_CACHE_TTL = 240
_VIDEO_URL_CACHE = {}


def _fetch_video_url(shortcode, source_url):
    now = time.time()
    cached = _VIDEO_URL_CACHE.get(shortcode)
    if cached and cached[1] > now:
        return cached[0]
    canonical = source_url or f"https://www.instagram.com/reel/{shortcode}/"
    video_url = (
        _fetch_via_remote(shortcode, canonical)
        or _fetch_via_instaloader(shortcode)
        or _fetch_via_yt_dlp(canonical)
        or _fetch_via_scrape(shortcode)
    )
    if video_url:
        _VIDEO_URL_CACHE[shortcode] = (video_url, now + _VIDEO_URL_CACHE_TTL)
    return video_url


def get_instagram_video_url(instagram_url):
    if not instagram_url:
        return None
    match = _IG_URL_RE.match(instagram_url.strip())
    if not match:
        return None
    return _fetch_video_url(match.group(1), instagram_url)


# ==========================================================================
# HTML rendering
# ==========================================================================
def _esc(s):
    """Conservative HTML-attribute escape."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _player_html(video_url, source_url, title=""):
    """Fullscreen native <video> page rendered inside the wizard iframe."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Argus Preview</title>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#000;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ display:flex; flex-direction:column; }}
  .bar {{ background:#111; color:#eee; padding:8px 12px;
          display:flex; justify-content:space-between; align-items:center;
          font-size:12px; }}
  .bar a {{ color:#7ab8ff; text-decoration:none; }}
  .bar a:hover {{ text-decoration:underline; }}
  .stage {{ flex:1; display:flex; align-items:center; justify-content:center;
            overflow:hidden; }}
  video {{ max-width:100%; max-height:100%; background:#000; outline:none; }}
</style>
</head>
<body>
<div class="bar">
  <span>{_esc(title)}</span>
  <a href="{_esc(source_url)}" target="_blank" rel="noopener">Open in Instagram ↗</a>
</div>
<div class="stage">
  <video controls autoplay playsinline preload="auto" src="{_esc(video_url)}"></video>
</div>
</body>
</html>"""


def _fallback_iframe_html(shortcode, source_url, title=""):
    """When extraction fails, fall back to Instagram's official embed."""
    embed_url = f"https://www.instagram.com/reel/{shortcode}/embed/captioned/"
    install_hint = (
        "yt-dlp isn't installed in this Odoo environment — "
        "install it (pip install yt-dlp) to enable inline playback."
        if not _HAS_YT_DLP else
        "Instagram is gating this reel behind a login wall — "
        "inline playback isn't possible from this server."
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Argus Preview (fallback)</title>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#000;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ display:flex; flex-direction:column; }}
  .bar {{ background:#111; color:#eee; padding:8px 12px;
          display:flex; justify-content:space-between; align-items:center;
          font-size:12px; }}
  .bar a {{ color:#7ab8ff; text-decoration:none; }}
  .note {{ background:#3b2d00; color:#ffd784; padding:8px 12px; font-size:11px;
           border-bottom:1px solid #5a4400; }}
  .stage {{ flex:1; display:flex; align-items:center; justify-content:center; }}
  iframe {{ width:540px; height:100%; max-width:100%; border:0; background:#000; }}
</style>
</head>
<body>
  <div class="bar">
    <span>{_esc(title)}</span>
    <a href="{_esc(source_url)}" target="_blank" rel="noopener">Open in Instagram ↗</a>
  </div>
  <div class="note">
    Inline playback unavailable. {install_hint}
  </div>
  <div class="stage">
    <iframe src="{embed_url}" scrolling="no" allowtransparency="true"
            allow="autoplay; encrypted-media; picture-in-picture"></iframe>
  </div>
</body>
</html>"""


def _embed_html(shortcode, source_url, title=""):
    embed_url = f"https://www.instagram.com/reel/{shortcode}/embed/captioned/"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Argus Preview</title>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#000;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ display:flex; flex-direction:column; }}
  .bar {{ background:#111; color:#eee; padding:8px 12px;
          display:flex; justify-content:space-between; align-items:center;
          font-size:12px; }}
  .bar a {{ color:#7ab8ff; text-decoration:none; }}
  .bar a:hover {{ text-decoration:underline; }}
  .stage {{ flex:1; display:flex; align-items:center; justify-content:center;
            padding:16px; box-sizing:border-box; overflow:hidden; }}
  iframe {{ width:min(540px, 100%); height:100%; border:0; background:#000;
            border-radius:8px; }}
</style>
</head>
<body>
  <div class="bar">
    <span>{_esc(title)}</span>
    <a href="{_esc(source_url)}" target="_blank" rel="noopener">Open in Instagram ↗</a>
  </div>
  <div class="stage">
    <iframe src="{embed_url}" scrolling="no" allowtransparency="true"
            allow="autoplay; encrypted-media; picture-in-picture"></iframe>
  </div>
</body>
</html>"""


# ==========================================================================
# Route
# ==========================================================================
class ArgusVideoPreviewController(http.Controller):
    """Serves the inline video player iframe for the Argus preview wizard."""

    @http.route(
        "/argus/preview/<string:shortcode>",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def preview(self, shortcode, title="", source_url="", **kwargs):
        if not re.match(r"^[\w\-]{1,32}$", shortcode or ""):
            return request.not_found()
        source = source_url or f"https://www.instagram.com/reel/{shortcode}/"
        try:
            video_url = _fetch_video_url(shortcode, source)
        except Exception as exc:
            _logger.warning("Argus preview: extraction crashed: %s", exc)
            video_url = None
        if video_url:
            html = _player_html(
                video_url=video_url, source_url=source, title=title or "",
            )
        else:
            html = _embed_html(
                shortcode=shortcode, source_url=source, title=title or "",
            )
        return request.make_response(
            html,
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store, max-age=0"),
            ],
        )

    @http.route(
        "/argus/instagram/url",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        cors="*",
    )
    def instagram_url(self, link=None, url=None, **kwargs):
        instagram_link = (
            link
            or url
            or kwargs.get("ig_url")
            or kwargs.get("instagram_url")
        )
        if not instagram_link:
            try:
                body = json.loads(request.httprequest.get_data() or b"{}")
            except (ValueError, TypeError):
                body = {}
            if isinstance(body, dict):
                instagram_link = (
                    body.get("link")
                    or body.get("url")
                    or body.get("ig_url")
                    or body.get("instagram_url")
                )
        instagram_link = (instagram_link or "").strip()
        try:
            match = _IG_URL_RE.match(instagram_link)
            if not match:
                payload = {
                    "ok": False,
                    "type": "none",
                    "error": "Not a valid Instagram URL",
                    "source_url": instagram_link,
                }
                status = 400
            else:
                shortcode = match.group(1)
                embed_url = f"https://www.instagram.com/reel/{shortcode}/embed/captioned/"
                try:
                    video_url = _fetch_video_url(shortcode, instagram_link)
                except Exception as exc:
                    _logger.warning("Argus instagram_url: extraction crashed: %s", exc)
                    video_url = None
                if video_url:
                    payload = {
                        "ok": True,
                        "type": "video",
                        "video_url": video_url,
                        "embed_url": embed_url,
                        "source_url": instagram_link,
                    }
                else:
                    payload = {
                        "ok": True,
                        "type": "iframe",
                        "embed_url": embed_url,
                        "source_url": instagram_link,
                    }
                status = 200
        except Exception as exc:
            _logger.exception("Argus instagram_url: unhandled error")
            payload = {
                "ok": False,
                "type": "error",
                "error": str(exc),
                "source_url": instagram_link,
            }
            status = 500
        return request.make_response(
            json.dumps(payload),
            status=status,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store, max-age=0"),
            ],
        )
