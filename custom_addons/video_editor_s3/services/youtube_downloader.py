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


def extract_metadata(url):
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
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=False)
    except yt_dlp.utils.DownloadError as exc:
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
):
    """Download the YouTube video to ``target_dir``.

    Returns ``(absolute_mp4_path, info_dict)``.

    ``cancel_exception`` is the exception class to raise on cooperative
    cancellation (typically ``JobCancelled`` from job_executor); defaults to
    ``InterruptedError`` to avoid importing the worker module here.
    """
    video_id, normalized = parse_youtube_url(url)
    if not video_id:
        raise UserError(_("Not a recognised YouTube URL: %s") % url)
    yt_dlp = _ensure_yt_dlp()
    cancel_exc = cancel_exception or InterruptedError
    outtmpl = "%s/%%(id)s.%%(ext)s" % target_dir.rstrip("/")
    opts = {
        # vcodec!=none excludes storyboards and audio-only formats; otherwise
        # /best can resolve to a thumbnail-strip image format and we end up
        # uploading a JPEG to S3.
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
        opts["max_filesize"] = int(max_size_bytes)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=True)
    except yt_dlp.utils.DownloadError as exc:
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
