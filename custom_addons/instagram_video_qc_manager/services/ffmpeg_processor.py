# -*- coding: utf-8 -*-
"""FFmpeg-backed render pipeline.

Given a *video.task.version* and an *editing configuration dictionary*, the
processor:

1. extracts the source attachment to a temp file,
2. builds an FFmpeg command from the configuration,
3. runs FFmpeg to produce the edited render,
4. probes the result with ffprobe,
5. (optionally) generates a low-bitrate preview,
6. stores everything back as ir.attachment records and returns them.

The configuration schema (loosely):

    {
        "trim":       {"start": 0.0, "end": 12.5},
        "crop":       {"x": 100, "y": 50, "w": 720, "h": 1280, "aspect": "9:16"},
        "rotate":     90,
        "resize":     {"w": 1080, "h": 1920},
        "mute":       false,
        "brightness": 0.05,
        "contrast":   1.1,
        "saturation": 1.0,
    }

Only keys that are present contribute to the FFmpeg filter chain.
"""

import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@contextmanager
def _tempdir():
    path = tempfile.mkdtemp(prefix="ffmpeg_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FFmpegProcessor(models.AbstractModel):
    _name = "ffmpeg.processor"
    _description = "FFmpeg Video Processor Service"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def render_version(self, version, config):
        """Render *version* using *config* (a dict).

        Legacy single-slot entry point.  Returns ``(edited_attachment,
        preview_attachment, command, probe)``.
        """
        if not version.original_attachment_id:
            raise UserError(_("Version has no source attachment."))

        with _tempdir() as workdir:
            src_path = self._dump_attachment(version.original_attachment_id, workdir, "src")
            out_path = os.path.join(workdir, "edited.mp4")
            preview_path = os.path.join(workdir, "preview.mp4")

            command = self._build_command(src_path, out_path, config)
            self._run(command, workdir, task=version.task_id, version=version, op="render")

            preview_cmd = self._build_preview_command(out_path, preview_path)
            try:
                self._run(preview_cmd, workdir, task=version.task_id, version=version, op="preview")
                preview_attachment = self._store(version, preview_path, kind="preview")
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Preview generation failed, continuing: %s", exc)
                preview_attachment = False

            probe = self._probe(out_path)
            edited_attachment = self._store(version, out_path, kind="edited")
            self._record_history(version, config)
            return edited_attachment, preview_attachment, " ".join(command), probe

    @api.model
    def render_for_attachment(self, version, attachment, config, slot):
        """Render a single ``attachment`` using ``config`` and store the
        result against ``version`` tagged with the slot number.

        Used by the two-slot rendering path so a single version owns a
        trimmed clip for each source slot.  Returns ``(edited,
        ffmpeg_command, probe)``.
        """
        if not attachment:
            raise UserError(_("No source attachment provided for slot #%s.") % slot)
        with _tempdir() as workdir:
            src_path = self._dump_attachment(attachment, workdir, f"src_{slot}")
            out_path = os.path.join(workdir, f"edited_slot_{slot}.mp4")
            command = self._build_command(src_path, out_path, config)
            self._run(
                command, workdir,
                task=version.task_id, version=version, op=f"render_slot_{slot}",
            )
            probe = self._probe(out_path)
            edited = self._store(version, out_path, kind="edited", slot=slot)
            self._record_history(version, config, slot=slot)
            return edited, " ".join(command), probe

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------
    def _build_command(self, src, dst, config):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

        trim = config.get("trim") or {}
        # ``-ss`` is placed AFTER ``-i`` (slow but keyframe-accurate
        # seek) rather than before, so the rendered clip starts at
        # exactly ``trim.start`` instead of the previous keyframe.
        # This trades a small amount of CPU for an exact trim — fine
        # for the short Instagram clips this module is built for and
        # eliminates the "saved video starts a beat too early" bug.
        cmd += ["-i", src]
        has_start = trim.get("start") is not None
        has_end = trim.get("end") is not None
        if has_start:
            cmd += ["-ss", f"{float(trim['start']):.3f}"]
        if has_end and has_start:
            duration = max(0.0, float(trim["end"]) - float(trim["start"]))
            if duration > 0:
                cmd += ["-t", f"{duration:.3f}"]
        elif has_end:
            cmd += ["-to", f"{float(trim['end']):.3f}"]

        filters = []
        crop = config.get("crop")
        if crop and all(k in crop for k in ("w", "h", "x", "y")):
            filters.append(f"crop={int(crop['w'])}:{int(crop['h'])}:{int(crop['x'])}:{int(crop['y'])}")

        resize = config.get("resize")
        if resize and resize.get("w") and resize.get("h"):
            filters.append(f"scale={int(resize['w'])}:{int(resize['h'])}")

        rotate = config.get("rotate")
        if rotate:
            # transpose=1 -> 90 CW, 2 -> 90 CCW, plus we can chain for 180
            mapping = {90: "transpose=1", -90: "transpose=2", 270: "transpose=2",
                       180: "transpose=1,transpose=1", -180: "transpose=2,transpose=2"}
            filters.append(mapping.get(int(rotate), f"rotate={float(rotate)}*PI/180"))

        eq_parts = []
        if "brightness" in config:
            eq_parts.append(f"brightness={float(config['brightness']):.3f}")
        if "contrast" in config:
            eq_parts.append(f"contrast={float(config['contrast']):.3f}")
        if "saturation" in config:
            eq_parts.append(f"saturation={float(config['saturation']):.3f}")
        if eq_parts:
            filters.append("eq=" + ":".join(eq_parts))

        if filters:
            cmd += ["-vf", ",".join(filters)]

        if config.get("mute"):
            cmd += ["-an"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "128k"]

        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            dst,
        ]
        return cmd

    def _build_preview_command(self, src, dst):
        return [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-vf", "scale=480:-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            dst,
        ]

    # ------------------------------------------------------------------
    # Execution + probing
    # ------------------------------------------------------------------
    def _run(self, command, workdir, task=None, version=None, op="render"):
        _logger.info("FFmpeg run: %s", " ".join(command))
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=workdir,
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except FileNotFoundError as exc:
            raise UserError(_("ffmpeg is not installed on the server.")) from exc
        except subprocess.TimeoutExpired as exc:
            raise UserError(_("FFmpeg timed out after 30 minutes.")) from exc
        except subprocess.CalledProcessError as exc:
            raise UserError(_("FFmpeg failed: %s") % (exc.stderr or exc.stdout or str(exc))) from exc
        elapsed_ms = int((time.time() - started) * 1000)
        if task:
            self.env["video.task.processing.log"].sudo().create(
                {
                    "task_id": task.id,
                    "version_id": version.id if version else False,
                    "level": "info",
                    "operation": op,
                    "message": (proc.stdout or proc.stderr or "ok")[:8000],
                    "duration_ms": elapsed_ms,
                    "ffmpeg_command": " ".join(command),
                }
            )

    def _probe(self, path):
        try:
            cmd = ["ffprobe", "-v", "error", "-print_format", "json",
                   "-show_format", "-show_streams", path]
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
            data = json.loads(proc.stdout or "{}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ffprobe failed: %s", exc)
            return {}
        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            {},
        )
        return {
            "duration": float(data.get("format", {}).get("duration", 0) or 0),
            "resolution": f"{video_stream.get('width', 0)}x{video_stream.get('height', 0)}",
            "codec": video_stream.get("codec_name"),
        }

    # ------------------------------------------------------------------
    # Attachment plumbing
    # ------------------------------------------------------------------
    def _dump_attachment(self, attachment, workdir, base):
        path = os.path.join(workdir, f"{base}.mp4")
        raw = attachment.raw or (base64.b64decode(attachment.datas) if attachment.datas else b"")
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def _store(self, version, path, kind, slot=None):
        with open(path, "rb") as fh:
            data = fh.read()
        mime, _enc = mimetypes.guess_type(path)
        # Slot suffix makes the attachment names easy to distinguish in
        # ir.attachment lists ("v3_edited_slot1.mp4" vs "v3_edited_slot2.mp4").
        slot_suffix = f"_slot{slot}" if slot else ""
        return self.env["ir.attachment"].sudo().create(
            {
                "name": f"{version.task_id.name}_v{version.version_no}_{kind}{slot_suffix}.mp4",
                "datas": base64.b64encode(data),
                "res_model": version._name,
                "res_id": version.id,
                "mimetype": mime or "video/mp4",
                "video_task_id": version.task_id.id,
                "video_version_id": version.id,
                "video_asset_kind": kind,
            }
        )

    def _record_history(self, version, config, slot=None):
        History = self.env["video.task.edit.history"].sudo()
        order = ["trim", "crop", "rotate", "resize", "mute",
                 "brightness", "contrast", "saturation"]
        suffix = f" (slot {slot})" if slot else ""
        for key in order:
            if key not in config:
                continue
            value = config[key]
            History.create(
                {
                    "version_id": version.id,
                    "action_type": key,
                    "action_data": json.dumps(value) if isinstance(value, (dict, list)) else str(value),
                    "notes": suffix or False,
                }
            )
        History.create(
            {
                "version_id": version.id,
                "action_type": "export",
                "action_data": json.dumps(config),
                "notes": suffix or False,
            }
        )
