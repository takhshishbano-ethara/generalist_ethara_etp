# -*- coding: utf-8 -*-
import getpass
import logging
import os
import re
import shutil
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

_MIN_HEIGHT = 2160
_MAX_HEIGHT = 2160
_MIN_FPS = 50
_MAX_FPS = 60
_DISK_HEADROOM = 1.05
_AUDIO_FALLBACK_BYTES = 200 * 1024 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

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
    "captcha",
)

# Split out from bot-challenge because the remediation is different: a 429
# means the host IP is throttled (proxy / backoff fixes it), not that
# YouTube wants a logged-in cookie.
_RATE_LIMIT_MARKERS = (
    "http error 429",
    "too many requests",
    "rate-limited",
    "rate limited",
)

# yt-dlp accepts the Netscape format only; an exported JSON / SQLite dump
# silently fails as "no cookies found" once the actual download starts, so
# we validate the header upfront and surface a clear error instead.
_NETSCAPE_HEADER_MARKERS = (
    "# netscape http cookie file",
    "# http cookie file",
)

_PROXY_SCHEMES = (
    "http://",
    "https://",
    "socks5://",
    "socks5h://",
    "socks4://",
    "socks4a://",
)


def _strip_ansi(value):
    return _ANSI_RE.sub("", value or "")


def _human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return "%.2f %s" % (n, unit)
        n /= 1024
    return "%.2f PiB" % n


def _ensure_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise UserError(_(
            "yt-dlp is not installed. Install it in the Odoo Python environment: "
            "pip install yt-dlp"
        )) from exc
    return yt_dlp


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
    host_no_www = host[4:] if host.startswith("www.") else host
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


def validate_cookies_file(path):
    if not os.path.isfile(path):
        raise UserError(_(
            "YouTube cookies file not found: %s\n\n"
            "Export a Netscape-format cookies.txt from a logged-in YouTube "
            "session (use the 'Get cookies.txt LOCALLY' extension for "
            "Chrome / Edge, or 'cookies.txt' for Firefox), upload it to the "
            "Odoo host, then set Settings \u2192 Crowly Sourcing \u2192 "
            "YouTube Ingest \u2192 Cookies File Path to the absolute path."
        ) % path)
    if not os.access(path, os.R_OK):
        try:
            os_user = getpass.getuser()
        except Exception:
            os_user = "?"
        raise UserError(_(
            "YouTube cookies file at %(path)s is not readable by the Odoo "
            "OS user (%(user)s).\n\n"
            "Fix on the server:\n"
            "  chown odoo:odoo %(path)s\n"
            "  chmod 600 %(path)s"
        ) % {"path": path, "user": os_user})
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise UserError(_(
            "Could not stat YouTube cookies file %s: %s"
        ) % (path, exc)) from exc
    if size <= 0:
        raise UserError(_(
            "YouTube cookies file %s is empty. Re-export it from a "
            "logged-in YouTube session and upload again."
        ) % path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().strip().lower()
    except OSError as exc:
        raise UserError(_(
            "Could not read YouTube cookies file %s: %s"
        ) % (path, exc)) from exc
    if not any(marker in first_line for marker in _NETSCAPE_HEADER_MARKERS):
        raise UserError(_(
            "YouTube cookies file %s is not in Netscape format (first line "
            "must start with '# Netscape HTTP Cookie File'). Re-export it "
            "using the 'Get cookies.txt LOCALLY' (Chrome / Edge) or "
            "'cookies.txt' (Firefox) extension."
        ) % path)


def validate_proxy_url(url):
    lower = url.strip().lower()
    if not any(lower.startswith(scheme) for scheme in _PROXY_SCHEMES):
        raise UserError(_(
            "YouTube Proxy URL %s has an unsupported scheme. Use one of: "
            "http://, https://, socks5://, socks5h://, socks4://, socks4a://"
        ) % url)


def _common_opts(*, cookies_path=None, proxy_url=None, cookies_from_browser=None):
    """Base ydl opts: retries + cookies + proxy + headers + remote components."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "http_headers": {"User-Agent": _DEFAULT_USER_AGENT},
        "geo_bypass": True,
        "remote_components": list(_DEFAULT_REMOTE_COMPONENTS),
    }
    if cookies_path:
        validate_cookies_file(cookies_path)
        opts["cookiefile"] = cookies_path
        _logger.info("yt-dlp cookies file=%s", cookies_path)

    # Browser cookies are opt-in only — auto-detecting a signed-in browser
    # makes YouTube serve a stripped format set, which breaks the 2160p gate.
    if "cookiefile" not in opts:
        browser_tuple = _parse_browser_spec(cookies_from_browser)
        if browser_tuple:
            cache_path = _cookie_cache_path()
            status = _ensure_browser_cookie_cache(
                browser_tuple, cache_path, _DEFAULT_COOKIE_CACHE_TTL_SECONDS,
            )
            if status in ("hit", "refreshed"):
                opts["cookiefile"] = cache_path
                _logger.info(
                    "yt-dlp cookies-from-browser=%s (configured, cache %s)",
                    browser_tuple[0], status,
                )
            else:
                _logger.warning(
                    "yt-dlp cookies-from-browser=%s extraction failed; "
                    "proceeding without cookies.",
                    browser_tuple[0],
                )

    if proxy_url:
        validate_proxy_url(proxy_url)
        opts["proxy"] = proxy_url
        _logger.info("yt-dlp proxy configured")
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
        "Crowly Sourcing → YouTube Ingest → Cookies File Path to that absolute path.\n\n"
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


def _looks_like_rate_limit(exc):
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _rate_limit_message(url, *, opts=None):
    opts = opts or {}
    proxy = opts.get("proxy")
    proxy_line = (
        _("  • YouTube Proxy URL: configured (active)")
        if proxy else _("  • YouTube Proxy URL: not set")
    )
    return _(
        "YouTube rate-limited this Odoo host (HTTP 429 / too many requests) "
        "while fetching %(url)s.\n\n"
        "Current configuration:\n"
        "%(proxy)s\n\n"
        "Remediation:\n"
        "  1. Wait 10–30 minutes before retrying — the limit resets on its own.\n"
        "  2. If retries keep failing, the Odoo host's IP itself is throttled. "
        "Set Settings → Crowly Sourcing → YouTube Ingest → "
        "Proxy URL to a residential HTTP / SOCKS5 proxy and retry.\n"
        "  3. Reduce concurrency of bulk ingests via Settings → "
        "Crowly Sourcing → Processing Limits → Max Concurrent Jobs."
    ) % {"url": url, "proxy": proxy_line}


def _is_hdr(fmt):
    dr = (fmt.get("dynamic_range") or "").upper()
    if dr in ("HDR", "HDR10", "HDR10+", "DV", "HLG"):
        return True
    if dr == "SDR":
        return False
    note = (fmt.get("format_note") or "").upper()
    return "HDR" in note or "DOLBY" in note


def _is_playlist(info):
    if (info.get("_type") or "") in ("playlist", "multi_video", "url_transparent"):
        return True
    return isinstance(info.get("entries"), list)


def _qualifying_formats(formats, *, min_height, max_height, min_fps, max_fps):
    out = []
    for f in formats or []:
        if (f.get("vcodec") or "none") == "none":
            continue
        # YouTube caps pre-muxed (video+audio) streams at 1080p, so they would
        # never satisfy a 2160p floor; skipping them lets the DASH pair win.
        if (f.get("acodec") or "none") != "none":
            continue
        h = f.get("height") or 0
        if h < min_height or h > max_height:
            continue
        fps = f.get("fps") or 0
        if fps < min_fps or fps > max_fps:
            continue
        out.append(f)

    def rank(f):
        return (
            f.get("height") or 0,
            0 if _is_hdr(f) else 1,
            f.get("tbr") or f.get("vbr") or 0,
        )

    out.sort(key=rank, reverse=True)
    return out


def _describe_available(formats):
    rows = []
    for f in formats or []:
        if (f.get("vcodec") or "none") == "none":
            continue
        if (f.get("acodec") or "none") != "none":
            continue
        rows.append((
            f.get("height") or 0,
            f.get("fps") or 0,
            (f.get("vcodec") or "?").split(".")[0],
            "HDR" if _is_hdr(f) else "SDR",
        ))
    rows.sort(reverse=True)
    if not rows:
        return "none"
    return ", ".join("%sp%g/%s/%s" % (h, fps, vc, hdr) for h, fps, vc, hdr in rows[:6])


def _estimate_size_bytes(chosen, info):
    v = chosen.get("filesize") or chosen.get("filesize_approx") or 0
    a = 0
    for f in info.get("formats") or []:
        if (f.get("acodec") or "none") != "none" and (f.get("vcodec") or "none") == "none":
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            if sz > a:
                a = sz
    if not a:
        a = _AUDIO_FALLBACK_BYTES
    return int((v + a) * _DISK_HEADROOM)


def have_free_space(target_dir, needed_bytes):
    try:
        free = shutil.disk_usage(target_dir).free
    except OSError:
        return (True, 0)
    return (free >= needed_bytes, free)


def _info_to_metadata(info, fallback_video_id):
    return {
        "video_id": info.get("id") or fallback_video_id,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration_seconds": float(info.get("duration") or 0.0),
    }


def probe_and_select(url, *, cookies_path=None, proxy_url=None, cookies_from_browser=None):
    """Probe once, gate-check 2160p50/60, return (info, chosen_format)."""
    video_id, normalized = parse_youtube_url(url)
    if not video_id or not normalized:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    opts = _common_opts(
        cookies_path=cookies_path,
        proxy_url=proxy_url,
        cookies_from_browser=cookies_from_browser,
    )
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_rate_limit(exc):
            raise UserError(_rate_limit_message(normalized, opts=opts)) from exc
        if _looks_like_bot_challenge(exc):
            _invalidate_cookie_cache()
            raise UserError(_bot_challenge_message(normalized, opts=opts)) from exc
        raise UserError(_("Could not read YouTube metadata: %s") % _strip_ansi(str(exc))) from exc
    if _is_playlist(info):
        raise UserError(_(
            "Playlists or channels are not supported \u2014 submit a single video URL."
        ))
    formats = info.get("formats") or []
    if not formats:
        raise UserError(_(
            "YouTube returned no playable streams for %s. This usually means a "
            "bot challenge, regional/age block, or membership requirement. "
            "Configure Settings \u2192 YouTube Ingest \u2192 Cookies File Path or "
            "Cookies From Browser, then retry."
        ) % normalized)
    candidates = _qualifying_formats(
        formats,
        min_height=_MIN_HEIGHT, max_height=_MAX_HEIGHT,
        min_fps=_MIN_FPS, max_fps=_MAX_FPS,
    )
    if not candidates:
        raise UserError(_(
            "This video doesnt meet the minimum req of 2160p50 or 2160p60 "
            "(available video-only streams: %s)."
        ) % _describe_available(formats))
    return (info, candidates[0])


def extract_metadata(url, *, cookies_path=None, proxy_url=None, cookies_from_browser=None):
    """Metadata fetch with no quality gate (used by the dedup-hit path)."""
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    opts = _common_opts(
        cookies_path=cookies_path,
        proxy_url=proxy_url,
        cookies_from_browser=cookies_from_browser,
    )
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_rate_limit(exc):
            raise UserError(_rate_limit_message(normalized, opts=opts)) from exc
        if _looks_like_bot_challenge(exc):
            _invalidate_cookie_cache()
            raise UserError(_bot_challenge_message(normalized, opts=opts)) from exc
        raise UserError(_("Could not read YouTube metadata: %s") % _strip_ansi(str(exc))) from exc
    return _info_to_metadata(info, video_id)


def _make_progress_hook(progress_cb, cancel_event, cancel_exc):
    def hook(d):
        status = d.get("status")
        if cancel_event is not None and cancel_event.is_set():
            raise cancel_exc("YouTube download cancelled.")
        if status == "downloading" and progress_cb is not None:
            try:
                progress_cb(
                    int(d.get("downloaded_bytes") or 0),
                    int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0),
                    status,
                )
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
    info=None,
    chosen_format=None,
    max_size_bytes=None,
    progress_cb=None,
    cancel_event=None,
    cancel_exception=None,
    cookies_path=None,
    proxy_url=None,
    cookies_from_browser=None,
):
    """Download gated video as .mkv. Returns (mkv_path, info, chosen_format).

    Pass ``info`` + ``chosen_format`` from a prior ``probe_and_select`` to
    skip the extra network probe.
    """
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    if info is None or chosen_format is None:
        info, chosen_format = probe_and_select(
            normalized,
            cookies_path=cookies_path,
            proxy_url=proxy_url,
            cookies_from_browser=cookies_from_browser,
        )
    fid = chosen_format.get("format_id")
    if not fid:
        raise UserError(_("Selected YouTube format has no format_id; cannot download."))

    estimated = _estimate_size_bytes(chosen_format, info)
    if max_size_bytes and estimated > int(max_size_bytes):
        raise UserError(_(
            "Estimated download size (%(est)s) exceeds the configured maximum "
            "(%(max)s) \u2014 aborting before any bytes are transferred."
        ) % {"est": _human_bytes(estimated), "max": _human_bytes(max_size_bytes)})
    ok, free = have_free_space(target_dir, estimated)
    if not ok:
        raise UserError(_(
            "Insufficient free disk space at %(dir)s: need ~%(est)s, have %(free)s."
        ) % {"dir": target_dir, "est": _human_bytes(estimated), "free": _human_bytes(free)})

    yt_dlp = _ensure_yt_dlp()
    cancel_exc = cancel_exception or InterruptedError
    outtmpl = "%s/%%(id)s.%%(ext)s" % target_dir.rstrip("/")
    # Pin the exact qualifying video stream + the best audio. No bare ``fid``
    # fallback: refusing to silently produce a video-only file is the whole
    # point of the gate.
    format_spec = "%s+bestaudio[ext=webm]/%s+bestaudio[ext=m4a]/%s+bestaudio" % (fid, fid, fid)
    opts = _common_opts(
        cookies_path=cookies_path,
        proxy_url=proxy_url,
        cookies_from_browser=cookies_from_browser,
    )
    opts.update({
        "format": format_spec,
        "outtmpl": outtmpl,
        "merge_output_format": "mkv",
        "restrictfilenames": True,
        "nooverwrites": True,
        "continuedl": True,
        "progress_hooks": [_make_progress_hook(progress_cb, cancel_event, cancel_exc)],
    })
    if max_size_bytes:
        opts["max_filesize"] = int(max_size_bytes)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=True)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_rate_limit(exc):
            raise UserError(_rate_limit_message(normalized, opts=opts)) from exc
        if _looks_like_bot_challenge(exc):
            _invalidate_cookie_cache()
            raise UserError(_bot_challenge_message(normalized, opts=opts)) from exc
        raise UserError(_("YouTube download failed: %s") % _strip_ansi(str(exc))) from exc

    final_path = None
    requested = info.get("requested_downloads") or []
    if requested:
        final_path = requested[0].get("filepath")
    if not final_path:
        final_path = info.get("filepath") or info.get("_filename")
    if not final_path:
        raise UserError(_("yt-dlp did not report a downloaded file path."))
    return (final_path, info, chosen_format)
