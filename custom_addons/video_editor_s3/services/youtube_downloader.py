# -*- coding: utf-8 -*-
import logging
import re
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

_PLAYER_CLIENT_FALLBACKS = (None, "tv", "ios", "android", "web_safari", "mweb")

_BOT_CHALLENGE_RE = re.compile(
    r"(sign in to confirm|confirm you'?re not a bot|"
    r"requires.*cookies|use --cookies)",
    re.IGNORECASE,
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


def _looks_like_bot_challenge(exc):
    return bool(_BOT_CHALLENGE_RE.search(str(exc) or ""))


def _bot_challenge_message(url, attempted_clients):
    tried = ", ".join(c or "default" for c in attempted_clients)
    return _(
        "YouTube blocked the download with a bot-protection challenge for %(url)s.\n"
        "\n"
        "yt-dlp was tried with these player clients: %(tried)s.\n"
        "\n"
        "Open Settings \u2192 Video Editor S3 \u2192 YouTube Ingest and configure one of:\n"
        "  \u2022 Cookies From Browser \u2014 e.g. chrome (the browser must be installed on "
        "the Odoo host and signed in to YouTube). Works only on hosts where Odoo "
        "can read that browser's cookie store.\n"
        "  \u2022 Cookies File Path \u2014 absolute path to a Netscape-format cookies.txt "
        "exported from a logged-in YouTube session (use the 'Get cookies.txt "
        "LOCALLY' extension). Suitable for server deployments.\n"
        "  \u2022 YouTube Proxy URL \u2014 optional residential proxy if your server's IP "
        "is blocked by YouTube.",
        url=url, tried=tried,
    )


def _apply_yt_auth_opts(opts, *, cookies_file=None, cookies_from_browser=None, proxy=None):
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if proxy:
        opts["proxy"] = proxy
    return opts


def _set_player_client(opts, player_client):
    if not player_client:
        return opts
    args = dict(opts.get("extractor_args") or {})
    args["youtube"] = dict(args.get("youtube") or {})
    args["youtube"]["player_client"] = [player_client]
    opts["extractor_args"] = args
    return opts


def extract_metadata(url, *, cookies_file=None, cookies_from_browser=None, proxy=None):
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    _apply_yt_auth_opts(
        base_opts,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
    last_exc = None
    attempted = []
    info = None
    for player_client in _PLAYER_CLIENT_FALLBACKS:
        attempted.append(player_client)
        opts = dict(base_opts)
        _set_player_client(opts, player_client)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalized, download=False)
            break
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            if _looks_like_bot_challenge(exc):
                _logger.warning(
                    "yt-dlp metadata fetch hit bot challenge with player_client=%s; trying next",
                    player_client or "default",
                )
                continue
            raise UserError(_("Could not read YouTube metadata: %s") % exc) from exc
    else:
        raise UserError(_bot_challenge_message(normalized, attempted)) from last_exc

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
    cookies_file=None,
    cookies_from_browser=None,
    proxy=None,
):
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    cancel_exc = cancel_exception or InterruptedError
    outtmpl = "%s/%%(id)s.%%(ext)s" % target_dir.rstrip("/")
    base_opts = {
        "format": (
            "bestvideo[ext=mp4][vcodec!=none]+bestaudio[ext=m4a]"
            "/best[ext=mp4][vcodec!=none]"
            "/bestvideo[vcodec!=none]+bestaudio"
            "/best[vcodec!=none]"
        ),
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
    if max_size_bytes:
        base_opts["max_filesize"] = int(max_size_bytes)
    _apply_yt_auth_opts(
        base_opts,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        proxy=proxy,
    )
    last_exc = None
    attempted = []
    info = None
    for player_client in _PLAYER_CLIENT_FALLBACKS:
        if cancel_event is not None and cancel_event.is_set():
            raise cancel_exc("YouTube download cancelled.")
        attempted.append(player_client)
        opts = dict(base_opts)
        _set_player_client(opts, player_client)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalized, download=True)
            break
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            if _looks_like_bot_challenge(exc):
                _logger.warning(
                    "yt-dlp hit bot challenge with player_client=%s; trying next",
                    player_client or "default",
                )
                continue
            raise UserError(_("YouTube download failed: %s") % exc) from exc
    else:
        raise UserError(_bot_challenge_message(normalized, attempted)) from last_exc

    final_path = None
    requested = info.get("requested_downloads") or []
    if requested:
        final_path = requested[0].get("filepath")
    if not final_path:
        final_path = info.get("filepath") or info.get("_filename")
    if not final_path:
        raise UserError(_("yt-dlp did not report a downloaded file path."))
    return (final_path, info)
