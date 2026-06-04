# -*- coding: utf-8 -*-
import re
from urllib.parse import parse_qs, urlparse

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
