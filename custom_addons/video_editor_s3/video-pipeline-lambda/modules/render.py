import json
import logging
import os
import subprocess
import time

import boto3

import config

_logger = logging.getLogger(__name__)
_TRANSPOSE_MAP = {90: "transpose=1", 180: "transpose=2,transpose=2", 270: "transpose=2"}


def _build_vf_chain(cfg):
    parts = []
    crop = cfg.get("crop") or {}
    if crop:
        w, h, x, y = int(crop.get("w") or 0), int(crop.get("h") or 0), int(crop.get("x") or 0), int(crop.get("y") or 0)
        if w > 0 and h > 0:
            parts.append("crop=%d:%d:%d:%d" % (w, h, x, y))
    rotate = cfg.get("rotate")
    if rotate:
        mapped = _TRANSPOSE_MAP.get(int(rotate), None)
        if mapped:
            parts.append(mapped)
    resize = cfg.get("resize") or {}
    if resize:
        rw, rh = int(resize.get("w") or 0), int(resize.get("h") or 0)
        if rw > 0 and rh > 0:
            parts.append("scale=%d:%d:flags=lanczos" % (rw, rh))
    filt = cfg.get("filter") or {}
    eq_parts = []
    for key, default in (("brightness", 0.0), ("contrast", 1.0), ("saturation", 1.0)):
        val = filt.get(key)
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if abs(f - default) > 1e-6:
            eq_parts.append("%s=%.3f" % (key, f))
    if eq_parts:
        parts.append("eq=" + ":".join(eq_parts))
    return ",".join(parts)


def _build_cmd(src_input, dst_path, cfg):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if isinstance(src_input, str) and src_input.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", src_input]
    trim = cfg.get("trim") or {}
    try:
        start = float(trim.get("start") or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(trim.get("end") or 0.0)
    except (TypeError, ValueError):
        end = 0.0
    if start > 0:
        cmd += ["-ss", "%.3f" % start]
    if end > start:
        cmd += ["-t", "%.3f" % (end - start)]
    vf = _build_vf_chain(cfg)
    if vf:
        cmd += ["-vf", vf]
    if cfg.get("mute"):
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += [dst_path]
    return cmd


def _ffprobe(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path,
        ], timeout=60)
        meta = json.loads(out)
    except Exception as exc:
        _logger.warning("ffprobe failed: %s", exc)
        return {}
    fmt = meta.get("format") or {}
    vstream = next((s for s in (meta.get("streams") or []) if s.get("codec_type") == "video"), {})
    fps = 0.0
    rate = vstream.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        size_bytes = int(fmt.get("size") or os.path.getsize(path))
    except OSError:
        size_bytes = 0
    return {
        "duration": float(fmt.get("duration") or 0.0),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": fps,
        "size_bytes": size_bytes,
        "codec": vstream.get("codec_name") or "",
    }


def run(event, context):
    started = time.time()
    src_input = (event.get("source_url") or "").strip()
    if not src_input:
        return {"status": "error", "error": "source_url is required"}
    s3_bucket = event.get("s3_bucket") or config.S3_BUCKET
    s3_key = (event.get("s3_key") or "").lstrip("/")
    if not s3_key:
        return {"status": "error", "error": "s3_key is required"}
    cfg = event.get("config") or {}
    dst_path = "/tmp/render_%s.mp4" % (event.get("job_id") or "out")

    cmd = _build_cmd(src_input, dst_path, cfg)
    _logger.info("ffmpeg cmd: %s", " ".join(cmd))
    try:
        subprocess.check_call(cmd, timeout=850)
    except subprocess.CalledProcessError as exc:
        return {"status": "error", "error": "ffmpeg exit %s" % exc.returncode}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "ffmpeg timed out (lambda cap)"}
    if not os.path.exists(dst_path):
        return {"status": "error", "error": "ffmpeg produced no file at %s" % dst_path}

    probe = _ffprobe(dst_path)
    s3 = boto3.client("s3", region_name=config.S3_REGION)
    try:
        s3.upload_file(dst_path, s3_bucket, s3_key)
    except Exception as exc:
        return {"status": "error", "error": "s3 upload failed: %s" % str(exc)[:500]}
    finally:
        try:
            os.unlink(dst_path)
        except OSError:
            pass

    s3_url = "https://%s.s3.%s.amazonaws.com/%s" % (s3_bucket, config.S3_REGION, s3_key)
    width, height = probe.get("width") or 0, probe.get("height") or 0
    resolution = "%dx%d" % (width, height) if width and height else ""
    return {
        "status": "ok",
        "op": "render",
        "s3_url": s3_url,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "duration_seconds": probe.get("duration") or 0.0,
        "width": width,
        "height": height,
        "fps": probe.get("fps") or 0.0,
        "size_bytes": probe.get("size_bytes") or 0,
        "resolution": resolution,
        "codec": probe.get("codec") or "",
        "ffmpeg_command": " ".join(cmd),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
