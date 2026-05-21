"""Argus local IG video extractor.

Tiny HTTP server that returns the direct CDN ``.mp4`` URL for an
Instagram reel/post/tv link.  Run it on a residential network
(laptop/home box) and expose it to the Odoo server via Tailscale
or ``cloudflared tunnel`` to bypass Instagram's cloud-IP blocking.

Endpoints:
    POST /extract    body {"url": "<instagram link>"} -> {"ok": true, "video_url": "..."}
    GET  /extract?url=<instagram link>                -> same
    GET  /health                                       -> {"ok": true, "yt_dlp": ..., "instaloader": ...}

Run::

    pip install -U yt-dlp instaloader
    python local_extractor.py --host 127.0.0.1 --port 8080

Then expose via Tailscale (use ``http://<tailnet>:8080``) or::

    cloudflared tunnel --url http://localhost:8080

Point Argus at the resulting URL via the system parameter
``argus.remote_extractor_url`` in Odoo.
"""

import argparse
import json
import logging
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yt_dlp  # type: ignore

    _HAS_YT_DLP = True
except ImportError:
    yt_dlp = None  # type: ignore
    _HAS_YT_DLP = False

try:
    import instaloader  # type: ignore

    _HAS_INSTALOADER = True
except ImportError:
    instaloader = None  # type: ignore
    _HAS_INSTALOADER = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_logger = logging.getLogger("argus.extractor")

_IG_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/([\w\-]{1,32})/?",
    re.IGNORECASE,
)


def _via_yt_dlp(url):
    if not _HAS_YT_DLP:
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
        "socket_timeout": 12,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        _logger.warning("yt-dlp failed on %s: %s", url, exc)
        return None
    if not info:
        return None
    direct = info.get("url")
    if direct:
        return direct
    for entry in info.get("entries") or []:
        if entry and entry.get("url"):
            return entry["url"]
    return None


def _via_instaloader(shortcode):
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
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        if not post.is_video:
            return None
        return post.video_url or None
    except Exception as exc:
        _logger.warning("instaloader failed on %s: %s", shortcode, exc)
        return None


def _extract(url):
    url = (url or "").strip()
    if not url:
        return None, "Invalid Instagram URL"
    match = _IG_URL_RE.match(url)
    if not match:
        return None, "Invalid Instagram URL"
    shortcode = match.group(1)
    video_url = _via_yt_dlp(url) or _via_instaloader(shortcode)
    if not video_url:
        return None, "Both extractors failed (yt-dlp + instaloader)"
    return video_url, None


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
                "yt_dlp": _HAS_YT_DLP,
                "instaloader": _HAS_INSTALOADER,
            })
            return
        if parsed.path == "/extract":
            qs = urllib.parse.parse_qs(parsed.query or "")
            url = (qs.get("url") or qs.get("link") or [""])[0]
            video_url, error = _extract(url)
            if error:
                self._json(400 if "Invalid" in error else 404, {
                    "ok": False,
                    "error": error,
                    "source_url": url,
                })
                return
            self._json(200, {
                "ok": True,
                "video_url": video_url,
                "source_url": url,
            })
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/extract":
            self._json(404, {"ok": False, "error": "Not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        url = ""
        if isinstance(body, dict):
            url = body.get("url") or body.get("link") or ""
        video_url, error = _extract(url)
        if error:
            self._json(400, {
                "ok": False,
                "error": error,
                "source_url": url,
            })
            return
        self._json(200, {
            "ok": True,
            "video_url": video_url,
            "source_url": url,
        })

    def log_message(self, format, *args):
        _logger.info("%s - %s", self.address_string(), format % args)


def main():
    parser = argparse.ArgumentParser(
        description="Argus local Instagram video extractor",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="bind port (default 8080)",
    )
    args = parser.parse_args()

    if not _HAS_YT_DLP and not _HAS_INSTALOADER:
        print(
            "Neither yt-dlp nor instaloader is installed. "
            "Run: pip install -U yt-dlp instaloader"
        )
        sys.exit(1)

    _logger.info(
        "Argus extractor listening on %s:%d (yt_dlp=%s instaloader=%s)",
        args.host, args.port, _HAS_YT_DLP, _HAS_INSTALOADER,
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
