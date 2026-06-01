import json
import logging
import os
import subprocess
import time

import boto3
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import config
from modules import cookies

_logger = logging.getLogger(__name__)
_DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def _qualifying_formats(formats, *, min_height, max_height, min_fps, max_fps):
    out = []
    for f in formats or []:
        if (f.get("vcodec") or "none") == "none":
            continue
        if (f.get("acodec") or "none") != "none":
            continue
        h = f.get("height") or 0
        if h < min_height or h > max_height:
            continue
        fps = f.get("fps") or 0
        if fps < min_fps or fps > max_fps:
            continue
        out.append(f)
    out.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
    return out


def _ydl_opts(cookies_path):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "http_headers": {"User-Agent": _DEFAULT_UA},
        "geo_bypass": True,
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    return opts


def _ffprobe(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-of", "json", path,
        ], timeout=60)
        meta = json.loads(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        _logger.warning("ffprobe failed on %s: %s", path, exc)
        return {}
    fmt = meta.get("format") or {}
    streams = meta.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    try:
        size_bytes = int(fmt.get("size") or os.path.getsize(path))
    except OSError:
        size_bytes = 0
    fps = 0.0
    rate = vstream.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration": float(fmt.get("duration") or 0.0),
        "size_bytes": size_bytes,
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": fps,
        "codec": vstream.get("codec_name") or "",
    }


def run(event, context):
    started = time.time()
    youtube_url = (event.get("youtube_url") or "").strip()
    tier = (event.get("tier") or "").strip()
    s3_bucket = event.get("s3_bucket") or config.S3_BUCKET
    s3_key = event.get("s3_key") or ""
    if not youtube_url:
        return {"status": "error", "error": "youtube_url is required"}
    if not s3_key:
        return {"status": "error", "error": "s3_key is required"}
    if tier and tier not in config.YOUTUBE_TIERS:
        return {"status": "error", "error": "unknown tier: %s" % tier}

    cookies_path = cookies.cookies_file_path()
    spec = config.YOUTUBE_TIERS[tier] if tier else None
    opts = _ydl_opts(cookies_path)
    opts["skip_download"] = True

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
    except DownloadError as exc:
        return {"status": "error", "error": "probe failed: %s" % str(exc)[:500]}

    formats = info.get("formats") or []
    if spec:
        candidates = _qualifying_formats(
            formats,
            min_height=spec["min_height"], max_height=spec["max_height"],
            min_fps=spec["min_fps"], max_fps=spec["max_fps"],
        )
    else:
        candidates = [f for f in formats if (f.get("vcodec") or "none") != "none" and (f.get("acodec") or "none") == "none"]
        candidates.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
    if not candidates:
        return {"status": "error", "error": "no qualifying stream for tier=%s" % tier}
    chosen = candidates[0]
    fid = chosen.get("format_id")
    format_spec = "%s+bestaudio[ext=webm]/%s+bestaudio[ext=m4a]/%s+bestaudio" % (fid, fid, fid)

    out_path = "/tmp/%s_%s.mkv" % (info.get("id") or "video", tier or "best")
    dl_opts = _ydl_opts(cookies_path)
    dl_opts.update({
        "format": format_spec,
        "merge_output_format": "mkv",
        "outtmpl": out_path,
        "noprogress": True,
    })
    try:
        with YoutubeDL(dl_opts) as ydl:
            ydl.download([youtube_url])
    except DownloadError as exc:
        return {"status": "error", "error": "download failed: %s" % str(exc)[:500]}
    if not os.path.exists(out_path):
        return {"status": "error", "error": "downloaded file missing: %s" % out_path}

    probe = _ffprobe(out_path)
    if spec and int(probe.get("height") or 0) < spec["min_height"]:
        return {"status": "error", "error": "downloaded height %s below tier floor %s" % (probe.get("height"), spec["min_height"])}

    s3 = boto3.client("s3", region_name=config.S3_REGION)
    try:
        s3.upload_file(out_path, s3_bucket, s3_key)
    except Exception as exc:
        return {"status": "error", "error": "s3 upload failed: %s" % str(exc)[:500]}
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    s3_url = "https://%s.s3.%s.amazonaws.com/%s" % (s3_bucket, config.S3_REGION, s3_key)
    return {
        "status": "ok",
        "op": "youtube_ingest",
        "s3_url": s3_url,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "tier": tier,
        "video_id": info.get("id") or "",
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration_seconds": probe.get("duration") or float(info.get("duration") or 0.0),
        "width": probe.get("width") or chosen.get("width") or 0,
        "height": probe.get("height") or chosen.get("height") or 0,
        "fps": probe.get("fps") or float(chosen.get("fps") or 0.0),
        "vcodec": (chosen.get("vcodec") or "").split(".")[0],
        "size_bytes": probe.get("size_bytes") or 0,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
