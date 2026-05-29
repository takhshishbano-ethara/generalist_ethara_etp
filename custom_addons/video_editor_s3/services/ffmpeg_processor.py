# -*- coding: utf-8 -*-
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_BINARY_SEARCH_PATHS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
)

_TRANSPOSE_MAP = {
    90: "transpose=1",
    -90: "transpose=2",
    270: "transpose=2",
    180: "transpose=1,transpose=1",
    -180: "transpose=1,transpose=1",
}

_MAX_LOG_MESSAGE = 8000
_FFMPEG_TIMEOUT = 1800
_FFPROBE_TIMEOUT = 60
_CONFIG_NAMESPACE = "video_editor_s3"


def _parse_fps(value):
    if not value or value == "0/0":
        return 0.0
    try:
        if "/" in value:
            num, denom = value.split("/", 1)
            denom_f = float(denom)
            if denom_f <= 0:
                return 0.0
            return float(num) / denom_f
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class FfmpegProcessor(models.AbstractModel):
    _name = "video.editor.s3.ffmpeg.processor"
    _description = "FFmpeg/FFprobe processor for video.editor.s3"

    def _resolve_binary(self, name):
        icp = self.env["ir.config_parameter"].sudo()
        override = icp.get_param(f"{_CONFIG_NAMESPACE}.{name}_path", "").strip()
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        which = shutil.which(name)
        if which:
            return which
        for base in _BINARY_SEARCH_PATHS:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _require_binary(self, name):
        resolved = self._resolve_binary(name)
        if resolved:
            return resolved
        paths = ", ".join(_BINARY_SEARCH_PATHS)
        raise UserError(_(
            "%(name)s binary not found.\n\n"
            "Install:\n"
            "  macOS:   brew install ffmpeg\n"
            "  Debian:  sudo apt-get install ffmpeg\n"
            "  RHEL:    sudo dnf install ffmpeg\n\n"
            "Searched: %(paths)s\n\n"
            "Override via the System Parameter:\n"
            "  Key:   %(ns)s.%(name)s_path\n"
            "  Value: /absolute/path/to/%(name)s"
        ) % {"name": name, "paths": paths, "ns": _CONFIG_NAMESPACE})

    @contextmanager
    def _tempdir(self):
        path = tempfile.mkdtemp(prefix="video_editor_s3_")
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def _probe(self, path):
        out = {"duration": 0.0, "width": 0, "height": 0, "resolution": "", "codec": "", "size_bytes": 0, "fps": 0.0}
        try:
            out["size_bytes"] = os.path.getsize(path)
        except OSError:
            pass
        ffprobe = self._resolve_binary("ffprobe")
        if not ffprobe:
            return out
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT, check=True,
            )
            data = json.loads(result.stdout or "{}")
            fmt = data.get("format") or {}
            if fmt.get("duration"):
                try:
                    out["duration"] = float(fmt["duration"])
                except (TypeError, ValueError):
                    pass
            if not out["size_bytes"] and fmt.get("size"):
                try:
                    out["size_bytes"] = int(fmt["size"])
                except (TypeError, ValueError):
                    pass
            for stream in data.get("streams") or []:
                if stream.get("codec_type") == "video":
                    out["width"] = int(stream.get("width") or 0)
                    out["height"] = int(stream.get("height") or 0)
                    if out["width"] and out["height"]:
                        out["resolution"] = "%dx%d" % (out["width"], out["height"])
                    out["codec"] = stream.get("codec_name") or ""
                    out["fps"] = _parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "")
                    break
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as err:
            _logger.warning("ffprobe failed for %s: %s", path, err)
        return out

    def _filter_chain(self, config):
        filters = []
        crop = (config or {}).get("crop") or {}
        if crop:
            try:
                w = int(crop.get("w") or 0)
                h = int(crop.get("h") or 0)
                x = int(crop.get("x") or 0)
                y = int(crop.get("y") or 0)
            except (TypeError, ValueError):
                w = h = x = y = 0
            if w > 0 and h > 0:
                filters.append("crop=%d:%d:%d:%d" % (w, h, x, y))

        rotate = (config or {}).get("rotate")
        if rotate:
            try:
                deg = int(rotate)
            except (TypeError, ValueError):
                deg = 0
            mapped = _TRANSPOSE_MAP.get(deg)
            if mapped:
                filters.append(mapped)

        resize = (config or {}).get("resize") or {}
        if resize:
            try:
                rw = int(resize.get("w") or 0)
                rh = int(resize.get("h") or 0)
            except (TypeError, ValueError):
                rw = rh = 0
            if rw > 0 and rh > 0:
                filters.append("scale=%d:%d:flags=lanczos" % (rw, rh))

        eq_parts = []
        for key, scale in (("brightness", 1.0), ("contrast", 1.0), ("saturation", 1.0)):
            val = (config or {}).get(key)
            if val is None:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if abs(f) > 1e-6 if key == "brightness" else abs(f - 1.0) > 1e-6:
                eq_parts.append("%s=%.3f" % (key, f * scale))
        if eq_parts:
            filters.append("eq=" + ":".join(eq_parts))

        return ",".join(filters)

    def _build_command(self, ffmpeg, src, dst, config, *, preview=False):
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if isinstance(src, str) and src.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        cmd += ["-i", src]
        trim = (config or {}).get("trim") or {}
        start = trim.get("start")
        end = trim.get("end")
        if start is not None:
            try:
                start_f = float(start)
            except (TypeError, ValueError):
                start_f = 0.0
            if start_f > 0:
                cmd += ["-ss", "%.3f" % start_f]
        if end is not None:
            try:
                end_f = float(end)
                start_f = float(start) if start is not None else 0.0
            except (TypeError, ValueError):
                end_f = start_f = 0.0
            if end_f > start_f:
                cmd += ["-t", "%.3f" % (end_f - start_f)]

        vf = self._filter_chain(config)
        if preview:
            preview_filter = "scale=480:-2"
            vf = vf + "," + preview_filter if vf else preview_filter
        if vf:
            cmd += ["-vf", vf]

        if (config or {}).get("mute"):
            cmd += ["-an"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "96k" if preview else "128k"]

        if preview:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30"]
        else:
            # x264 defaults (ncpu threads, ref=3, bframes=3, rc-lookahead=40)
            # OOM on 2160p sources because each thread keeps its own reference
            # and lookahead frame buffers. Cap to a memory budget that survives
            # 2 concurrent jobs on a small VPS while leaving quality nearly
            # unchanged (~5-8 % bitrate cost at the same CRF).
            cmd += [
                "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                "-threads", "2",
                "-x264-params", "ref=2:bframes=2:rc-lookahead=10",
            ]

        cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
        return cmd

    def _run(self, cmd, *, job=None, operation="ffmpeg"):
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT, check=True,
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log(job, "info", operation, (stdout or stderr or "ok")[:_MAX_LOG_MESSAGE],
                      duration_ms=duration_ms, command=cmd)
            return stdout, stderr
        except FileNotFoundError as err:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log(job, "error", operation, "binary missing: %s" % err,
                      duration_ms=duration_ms, command=cmd)
            raise UserError(_("FFmpeg binary missing on this host: %s") % err)
        except subprocess.TimeoutExpired as err:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log(job, "error", operation,
                      "timeout after %ss: %s" % (_FFMPEG_TIMEOUT, err),
                      duration_ms=duration_ms, command=cmd)
            raise UserError(_("FFmpeg timed out after %s seconds.") % _FFMPEG_TIMEOUT)
        except subprocess.CalledProcessError as err:
            duration_ms = int((time.monotonic() - started) * 1000)
            stderr = (err.stderr or "")[:_MAX_LOG_MESSAGE]
            self._log(job, "error", operation, stderr or str(err),
                      duration_ms=duration_ms, command=cmd)
            raise UserError(_("FFmpeg failed:\n%s") % (stderr or err))

    def _log(self, job, level, operation, message, *, duration_ms=0, command=None):
        if not job:
            return
        cmd_text = ""
        if command:
            try:
                cmd_text = " ".join(str(c) for c in command)
            except Exception:
                cmd_text = ""
        try:
            self.env["video.editor.processing.log"].sudo().create({
                "project_id": job.project_id.id,
                "job_id": job.id,
                "level": level,
                "operation": operation,
                "message": (message or "")[:_MAX_LOG_MESSAGE],
                "duration_ms": duration_ms,
                "ffmpeg_command": cmd_text[:_MAX_LOG_MESSAGE],
            })
        except Exception as err:
            _logger.warning("video.editor.processing.log write failed: %s", err)

    @api.model
    def render(self, job, src_abs, dst_abs, config, *, preview=False):
        ffmpeg = self._require_binary("ffmpeg")
        cmd = self._build_command(ffmpeg, src_abs, dst_abs, config, preview=preview)
        operation = "preview" if preview else "render"
        self._run(cmd, job=job, operation=operation)
        meta = self._probe(dst_abs)
        meta["ffmpeg_command"] = " ".join(cmd)
        return meta

    @api.model
    def probe(self, src_abs):
        return self._probe(src_abs)

    @api.model
    def move_into_storage(self, abs_src, abs_dst):
        dst_dir = os.path.dirname(abs_dst)
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.exists(abs_dst):
            try:
                os.remove(abs_dst)
            except OSError as err:
                _logger.warning("could not remove stale %s: %s", abs_dst, err)
        shutil.move(abs_src, abs_dst)
        return abs_dst
