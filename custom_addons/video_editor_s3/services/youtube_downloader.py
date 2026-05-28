# -*- coding: utf-8 -*-
import getpass
import logging
import os
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)

_YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "music.youtube.com",
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# yt-dlp >= 2026.03 uses an n-sig JS challenge served by YouTube. Enabling
# the ejs remote component lets yt-dlp fetch and run the solver, which is
# what makes real video/audio formats appear (otherwise only storyboard
# images are returned and we ship a JPEG to S3).
_DEFAULT_REMOTE_COMPONENTS = ("ejs:github",)

_BOT_CHALLENGE_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you\u2019re not a bot",
    "cookies-from-browser",
)


def parse_youtube_url(url):
    if not url or not isinstance(url, str):
        return (None, None)
    candidate = url.strip()
    if not candidate:
        return (None, None)
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return (None, None)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host_no_www = host[4:]
    else:
        host_no_www = host
    if host_no_www not in _YOUTUBE_HOSTS and host not in _YOUTUBE_HOSTS:
        return (None, None)

    video_id = None
    path = parsed.path or ""
    if host_no_www == "youtu.be" or host == "youtu.be":
        video_id = path.lstrip("/").split("/", 1)[0]
    else:
        if path == "/watch":
            qs = parse_qs(parsed.query or "")
            v = qs.get("v") or []
            if v:
                video_id = v[0]
        elif path.startswith("/shorts/"):
            video_id = path[len("/shorts/"):].split("/", 1)[0]
        elif path.startswith("/embed/"):
            video_id = path[len("/embed/"):].split("/", 1)[0]
        elif path.startswith("/v/"):
            video_id = path[len("/v/"):].split("/", 1)[0]

    if not video_id or not _VIDEO_ID_RE.match(video_id):
        return (None, None)
    normalized = "https://www.youtube.com/watch?v=%s" % video_id
    return (video_id, normalized)


def _ensure_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise UserError(_(
            "yt-dlp is not installed. Install it in the Odoo Python environment: "
            "pip install yt-dlp"
        )) from exc
    return yt_dlp


def _parse_browser_spec(spec):
    if not spec:
        return None
    spec = spec.strip()
    if not spec:
        return None
    if ":" in spec:
        browser, profile = spec.split(":", 1)
        browser = browser.strip()
        profile = profile.strip() or None
        if not browser:
            return None
        return (browser, profile, None, None)
    return (spec,)


# Safari is intentionally excluded: on macOS the sandbox prevents non-Apple
# processes from reading ~/Library/Containers/com.apple.Safari/.../Cookies.binarycookies
# even when Odoo runs as the logged-in user, so autodetecting it would only
# produce permission errors. Operators who really want Safari can still set
# Cookies From Browser = safari explicitly.
_BROWSER_AUTODETECT_ORDER = (
    "chrome",
    "firefox",
    "edge",
    "brave",
    "chromium",
    "vivaldi",
    "opera",
)


def _browser_candidate_paths(home):
    return {
        "chrome": [
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data"),
        ],
        "firefox": [
            os.path.join(home, "Library", "Application Support", "Firefox", "Profiles"),
            os.path.join(home, ".mozilla", "firefox"),
            os.path.join(home, "AppData", "Roaming", "Mozilla", "Firefox", "Profiles"),
        ],
        "edge": [
            os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
            os.path.join(home, ".config", "microsoft-edge"),
            os.path.join(home, "AppData", "Local", "Microsoft", "Edge", "User Data"),
        ],
        "brave": [
            os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser"),
            os.path.join(home, ".config", "BraveSoftware", "Brave-Browser"),
            os.path.join(home, "AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data"),
        ],
        "chromium": [
            os.path.join(home, "Library", "Application Support", "Chromium"),
            os.path.join(home, ".config", "chromium"),
        ],
        "vivaldi": [
            os.path.join(home, "Library", "Application Support", "Vivaldi"),
            os.path.join(home, ".config", "vivaldi"),
        ],
        "opera": [
            os.path.join(home, "Library", "Application Support", "com.operasoftware.Opera"),
            os.path.join(home, ".config", "opera"),
        ],
    }


def _autodetect_browser():
    home = os.path.expanduser("~")
    if not home or not os.path.isdir(home):
        return None
    candidates = _browser_candidate_paths(home)
    for browser in _BROWSER_AUTODETECT_ORDER:
        for path in candidates.get(browser, ()):
            if os.path.isdir(path):
                return browser
    return None


# Reading Chromium-family cookies decrypts "Chrome Safe Storage" via the OS
# keychain on every call — and on macOS the Keychain ACL is not reliably
# persistent for the Python interpreter, so "Always Allow" does not stick
# and the user is re-prompted every job. We extract once, cache as a
# Netscape file under data_dir, and refresh on TTL expiry or bot-challenge.
_COOKIE_CACHE_FILENAME = "yt_cookies_cache.txt"
_DEFAULT_COOKIE_CACHE_TTL_SECONDS = 24 * 60 * 60
_cookie_cache_lock = threading.Lock()


def _cookie_cache_dir():
    data_dir = None
    try:
        from odoo.tools import config as _odoo_config
        if _odoo_config:
            data_dir = _odoo_config.get("data_dir")
    except Exception:
        data_dir = None
    if not data_dir:
        data_dir = os.path.join(tempfile.gettempdir(), "video_editor_s3")
    cache_dir = os.path.join(data_dir, "video_editor_s3")
    try:
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return cache_dir


def _cookie_cache_path():
    return os.path.join(_cookie_cache_dir(), _COOKIE_CACHE_FILENAME)


def _cookie_cache_is_fresh(path, ttl_seconds):
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= 0:
        return False
    return (time.time() - st.st_mtime) < ttl_seconds


def _extract_and_cache_browser_cookies(browser_tuple, cache_path):
    yt_dlp = _ensure_yt_dlp()
    browser_name = browser_tuple[0]
    profile = browser_tuple[1] if len(browser_tuple) >= 2 else None
    keyring = browser_tuple[2] if len(browser_tuple) >= 3 else None
    container = browser_tuple[3] if len(browser_tuple) >= 4 else None
    try:
        jar = yt_dlp.cookies.extract_cookies_from_browser(
            browser_name,
            profile=profile,
            keyring=keyring,
            container=container,
        )
    except Exception as exc:
        _logger.warning(
            "yt-dlp cookie extraction from %s failed: %s", browser_name, exc
        )
        return False
    tmp_path = "%s.tmp.%d" % (cache_path, os.getpid())
    try:
        jar.save(tmp_path, ignore_discard=True, ignore_expires=True)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, cache_path)
    except Exception as exc:
        _logger.warning(
            "Failed to write cookie cache to %s: %s", cache_path, exc
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    _logger.info(
        "yt-dlp cookies cached to %s (browser=%s)", cache_path, browser_name
    )
    return True


def _ensure_browser_cookie_cache(browser_tuple, cache_path, ttl_seconds):
    if _cookie_cache_is_fresh(cache_path, ttl_seconds):
        return "hit"
    with _cookie_cache_lock:
        if _cookie_cache_is_fresh(cache_path, ttl_seconds):
            return "hit"
        if _extract_and_cache_browser_cookies(browser_tuple, cache_path):
            return "refreshed"
        return "failed"


def _invalidate_cookie_cache():
    path = _cookie_cache_path()
    try:
        os.unlink(path)
        _logger.info("yt-dlp cookie cache invalidated: %s", path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _logger.warning("Could not invalidate cookie cache %s: %s", path, exc)


def _common_opts(cookies_path=None, proxy_url=None, cookies_from_browser=None,
                 player_clients=None):
    opts = {
        "http_headers": {"User-Agent": _DEFAULT_USER_AGENT},
        "geo_bypass": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "remote_components": list(_DEFAULT_REMOTE_COMPONENTS),
    }
    if cookies_path:
        if os.path.isfile(cookies_path):
            opts["cookiefile"] = cookies_path
            _logger.info("yt-dlp cookies file=%s", cookies_path)
        else:
            _logger.warning(
                "YouTube cookies file not found at %s — ignoring.", cookies_path
            )

    if "cookiefile" not in opts:
        browser_source = "configured"
        browser_tuple = _parse_browser_spec(cookies_from_browser)
        if not browser_tuple:
            autodetected = _autodetect_browser()
            if autodetected:
                browser_tuple = (autodetected,)
                browser_source = "autodetected"
        if browser_tuple:
            cache_path = _cookie_cache_path()
            status = _ensure_browser_cookie_cache(
                browser_tuple, cache_path, _DEFAULT_COOKIE_CACHE_TTL_SECONDS,
            )
            if status in ("hit", "refreshed"):
                opts["cookiefile"] = cache_path
                _logger.info(
                    "yt-dlp cookies-from-browser=%s (%s, cache %s)",
                    browser_tuple[0], browser_source, status,
                )
            else:
                _logger.warning(
                    "yt-dlp cookies-from-browser=%s (%s) extraction failed; "
                    "proceeding without cookies.",
                    browser_tuple[0], browser_source,
                )

    if proxy_url:
        opts["proxy"] = proxy_url
        _logger.info("yt-dlp proxy configured")
    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}
    return opts


def _looks_like_bot_challenge(exc):
    msg = str(exc).lower()
    return any(marker in msg for marker in _BOT_CHALLENGE_MARKERS)


def _bot_challenge_message(url, *, opts=None):
    opts = opts or {}
    browser_tuple = opts.get("cookiesfrombrowser")
    cookiefile = opts.get("cookiefile")
    proxy = opts.get("proxy")
    try:
        os_user = getpass.getuser()
    except Exception:
        os_user = "?"

    if browser_tuple:
        browser_line = _("  • Cookies From Browser: %s (active)") % browser_tuple[0]
    else:
        browser_line = _(
            "  • Cookies From Browser: not set — no installed browser was found in "
            "this user's home directory either."
        )
    cookiefile_line = (
        _("  • Cookies File Path: %s (active)") % cookiefile
        if cookiefile else _("  • Cookies File Path: not set")
    )
    proxy_line = _("  • YouTube Proxy URL: %s") % (
        _("configured") if proxy else _("not set")
    )

    return _(
        "YouTube blocked the download with a bot-protection challenge for %(url)s.\n\n"
        "Current configuration (Odoo OS user: %(user)s):\n"
        "%(browser)s\n"
        "%(cookiefile)s\n"
        "%(proxy)s\n\n"
        "Recommended fix (most reliable, works on servers):\n"
        "  1. Open a NEW private/incognito window in your browser.\n"
        "  2. Log in to YouTube in that window.\n"
        "  3. Visit https://www.youtube.com/robots.txt in the same incognito tab.\n"
        "  4. Export youtube.com cookies via 'Get cookies.txt LOCALLY' (Chrome) or "
        "'cookies.txt' (Firefox) extension to a Netscape-format file.\n"
        "  5. CLOSE the incognito window — this freezes the session so YouTube does "
        "not rotate the cookies.\n"
        "  6. Upload the file to the Odoo host (e.g. /var/lib/odoo/yt_cookies.txt), "
        "make it readable by the Odoo OS user (%(user)s), and set Settings → "
        "Crowley Sourcing → YouTube Ingest → Cookies File Path to that absolute path.\n\n"
        "Alternative for local development only:\n"
        "  • Sign in to YouTube in Chrome / Firefox / Edge / Brave on this host. The "
        "downloader will auto-pick the first installed browser. Note: YouTube rotates "
        "live-session cookies, so this can stop working without warning — prefer the "
        "incognito + cookies.txt flow above for anything production.\n\n"
        "If the Odoo host's IP is itself blocked, also set Settings → YouTube Ingest → "
        "YouTube Proxy URL to a residential HTTP / SOCKS5 proxy."
    ) % {
        "url": url,
        "user": os_user,
        "browser": browser_line,
        "cookiefile": cookiefile_line,
        "proxy": proxy_line,
    }


def extract_metadata(url, *, cookies_path=None, proxy_url=None,
                     cookies_from_browser=None):
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    opts.update(_common_opts(
        cookies_path=cookies_path,
        proxy_url=proxy_url,
        cookies_from_browser=cookies_from_browser,
    ))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_bot_challenge(exc):
            _invalidate_cookie_cache()
            raise UserError(_bot_challenge_message(normalized, opts=opts)) from exc
        raise UserError(_("Could not read YouTube metadata: %s") % exc) from exc
    return {
        "video_id": info.get("id") or video_id,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration_seconds": float(info.get("duration") or 0.0),
    }


def _make_progress_hook(progress_cb, max_size_bytes, cancel_event, cancel_exc):
    def hook(d):
        status = d.get("status")
        if cancel_event is not None and cancel_event.is_set():
            raise cancel_exc("YouTube download cancelled.")
        if status == "downloading":
            downloaded = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            if max_size_bytes and total and total > max_size_bytes:
                raise UserError(_(
                    "YouTube video exceeds configured max size (%(total)s bytes > %(max)s bytes).",
                    total=total, max=max_size_bytes,
                ))
            if progress_cb is not None:
                try:
                    progress_cb(downloaded, total, status)
                except Exception:
                    _logger.exception("progress_cb raised; ignoring")
        elif status == "finished" and progress_cb is not None:
            try:
                progress_cb(
                    int(d.get("downloaded_bytes") or 0),
                    int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0),
                    status,
                )
            except Exception:
                _logger.exception("progress_cb raised; ignoring")

    return hook


def download_to_tempdir(
    url,
    target_dir,
    *,
    max_size_bytes=None,
    progress_cb=None,
    cancel_event=None,
    cancel_exception=None,
    cookies_path=None,
    proxy_url=None,
    cookies_from_browser=None,
):
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    cancel_exc = cancel_exception or InterruptedError
    outtmpl = "%s/%%(id)s.%%(ext)s" % target_dir.rstrip("/")
    opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "progress_hooks": [
            _make_progress_hook(progress_cb, max_size_bytes, cancel_event, cancel_exc)
        ],
    }
    opts.update(_common_opts(
        cookies_path=cookies_path,
        proxy_url=proxy_url,
        cookies_from_browser=cookies_from_browser,
    ))
    if max_size_bytes:
        opts["max_filesize"] = int(max_size_bytes)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=True)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_bot_challenge(exc):
            _invalidate_cookie_cache()
            raise UserError(_bot_challenge_message(normalized, opts=opts)) from exc
        raise UserError(_("YouTube download failed: %s") % exc) from exc
    final_path = None
    requested = info.get("requested_downloads") or []
    if requested:
        final_path = requested[0].get("filepath")
    if not final_path:
        final_path = info.get("filepath") or info.get("_filename")
    if not final_path:
        raise UserError(_("yt-dlp did not report a downloaded file path."))
    return (final_path, info)
