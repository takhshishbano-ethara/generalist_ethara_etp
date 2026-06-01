"""Crowley Sourcing local YouTube extractor.

Tiny HTTP server that downloads a (optionally trimmed) YouTube video
to MP4 with yt-dlp and streams the bytes back as the HTTP response
body. Run this on a residential network (laptop / home box) and expose
it to the Odoo server via Tailscale or ``cloudflared tunnel`` so
YouTube does not block the cloud-IP backend.

Endpoints:
    POST /download   body {"url": "...", "start_seconds": 0, "end_seconds": 0, "tier": "2160p"}
                     -> body: MP4 bytes
                        headers: X-Video-Id, X-Video-Title, X-Video-Channel,
                                 X-Video-Duration-Seconds, X-Video-Filename
    GET  /health      -> {"ok": true, "yt_dlp": "<version>" or false}

Run::

    pip install -U yt-dlp
    python local_youtube_extractor.py --host 127.0.0.1 --port 8081

Then expose via Tailscale (use ``http://<tailnet>:8081``) or::

    cloudflared tunnel --url http://localhost:8081

Point Odoo at the resulting URL via
``Settings > Crowley Sourcing > YouTube Ingest > Local Extractor URL``
(ICP key ``video_editor_s3.local_extractor_url``).
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yt_dlp  # type: ignore

    _HAS_YT_DLP = True
    _YT_DLP_VERSION = getattr(yt_dlp.version, "__version__", "unknown")
except ImportError:
    yt_dlp = None  # type: ignore
    _HAS_YT_DLP = False
    _YT_DLP_VERSION = ""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_logger = logging.getLogger("video_editor_s3.local_extractor")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIER_HEIGHTS = {
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
}
_DEFAULT_TIER = "2160p"
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _parse_youtube_url(url):
    if not url:
        return None, None
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError):
        return None, None
    host = (parsed.netloc or "").lower()
    if not host:
        return None, None
    youtube_hosts = (
        "youtube.com", "www.youtube.com",
        "m.youtube.com", "music.youtube.com",
        "youtu.be",
    )
    if host not in youtube_hosts:
        return None, None
    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
    else:
        path = parsed.path or ""
        if path == "/watch":
            qs = urllib.parse.parse_qs(parsed.query or "")
            video_id = (qs.get("v") or [""])[0]
        else:
            for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
                if path.startswith(prefix):
                    video_id = path[len(prefix):].split("/", 1)[0]
                    break
    if not video_id or not _VIDEO_ID_RE.match(video_id):
        return None, None
    normalized = "https://www.youtube.com/watch?v=%s" % video_id
    return video_id, normalized


def _download(url, start_seconds, end_seconds, tier):
    if not _HAS_YT_DLP:
        return None, None, "yt-dlp is not installed on the extractor host"
    video_id, normalized = _parse_youtube_url(url)
    if not video_id:
        return None, None, "Invalid YouTube URL"
    height = _TIER_HEIGHTS.get(tier or _DEFAULT_TIER)
    if not height:
        return None, None, "Unknown tier: %s" % tier

    tempdir = tempfile.mkdtemp(prefix="video_editor_s3_local_yt_")
    outtmpl = "%s/%%(id)s.%%(ext)s" % tempdir.rstrip("/")
    format_spec = "bv*[height=%d]+ba/b[height=%d]" % (height, height)

    opts = {
        "format": format_spec,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "nooverwrites": True,
        "continuedl": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "geo_bypass": True,
        "http_headers": {"User-Agent": _DEFAULT_USER_AGENT},
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ],
    }
    if start_seconds > 0.0 or end_seconds > 0.0:
        from yt_dlp.utils import download_range_func
        end_val = end_seconds if end_seconds > 0.0 else float("inf")
        opts["download_ranges"] = download_range_func(None, [(start_seconds, end_val)])
        opts["force_keyframes_at_cuts"] = True

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized, download=True)
    except Exception as exc:
        shutil.rmtree(tempdir, ignore_errors=True)
        _logger.warning("yt-dlp failed on %s: %s", normalized, exc)
        return None, tempdir, "yt-dlp download failed: %s" % exc

    final_path = None
    requested = info.get("requested_downloads") or []
    if requested:
        final_path = requested[0].get("filepath")
    if not final_path:
        final_path = info.get("filepath") or info.get("_filename")
    if not final_path or not os.path.isfile(final_path):
        shutil.rmtree(tempdir, ignore_errors=True)
        return None, tempdir, "yt-dlp did not produce a downloaded file"
    metadata = {
        "video_id": info.get("id") or video_id,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_seconds": float(info.get("duration") or 0.0),
        "filename": os.path.basename(final_path),
    }
    return (final_path, metadata), tempdir, None


def _encode_header(value):
    """ASCII-safe encoding for HTTP header values (RFC 5987 percent-escape)."""
    if value is None:
        return ""
    text = str(value)
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return urllib.parse.quote(text, safe="")


class Handler(BaseHTTPRequestHandler):

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {
                "ok": True,
                "yt_dlp": _YT_DLP_VERSION if _HAS_YT_DLP else False,
            })
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/download":
            self._json(404, {"ok": False, "error": "Not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "Body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "Body must be a JSON object"})
            return
        url = (body.get("url") or "").strip()
        try:
            start_seconds = max(float(body.get("start_seconds") or 0.0), 0.0)
        except (TypeError, ValueError):
            start_seconds = 0.0
        try:
            end_seconds = max(float(body.get("end_seconds") or 0.0), 0.0)
        except (TypeError, ValueError):
            end_seconds = 0.0
        tier = (body.get("tier") or _DEFAULT_TIER).strip() or _DEFAULT_TIER

        _logger.info(
            "download request: url=%s tier=%s start=%s end=%s",
            url, tier, start_seconds, end_seconds,
        )
        result, tempdir, error = _download(url, start_seconds, end_seconds, tier)
        if error:
            try:
                self._json(400 if "Invalid" in error or "Unknown" in error else 500, {
                    "ok": False, "error": error,
                })
            finally:
                if tempdir:
                    shutil.rmtree(tempdir, ignore_errors=True)
            return

        final_path, metadata = result
        try:
            size = os.path.getsize(final_path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Video-Id", _encode_header(metadata["video_id"]))
            self.send_header("X-Video-Title", _encode_header(metadata["title"]))
            self.send_header("X-Video-Channel", _encode_header(metadata["channel"]))
            self.send_header("X-Video-Duration-Seconds", "%.3f" % metadata["duration_seconds"])
            self.send_header("X-Video-Filename", _encode_header(metadata["filename"]))
            self.end_headers()
            with open(final_path, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)
            _logger.info(
                "download done: video_id=%s size=%d title=%s",
                metadata["video_id"], size, metadata["title"][:80],
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            _logger.warning("client disconnected mid-transfer: %s", exc)
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    def log_message(self, format, *args):
        _logger.info("%s - %s", self.address_string(), format % args)


def main():
    parser = argparse.ArgumentParser(
        description="Crowley Sourcing local YouTube extractor",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8081,
        help="bind port (default 8081)",
    )
    args = parser.parse_args()

    if not _HAS_YT_DLP:
        print("yt-dlp is not installed. Run: pip install -U yt-dlp")
        sys.exit(1)

    _logger.info(
        "Crowley Sourcing local YouTube extractor listening on %s:%d (yt_dlp=%s)",
        args.host, args.port, _YT_DLP_VERSION,
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
