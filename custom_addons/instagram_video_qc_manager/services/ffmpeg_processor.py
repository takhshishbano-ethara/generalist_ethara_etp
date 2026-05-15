# -*- coding: utf-8 -*-
"""FFmpeg-backed render pipeline.

Given a *video.task.version* and an *editing configuration dictionary*, the
processor:

1. extracts the source attachment to a temp file,
2. builds an FFmpeg command from the configuration,
3. runs FFmpeg into a temp directory,
4. probes the result with ffprobe,
5. **moves the rendered file to its canonical on-disk location**
   ``<media_root>/<task.id>/v<n>_<kind>[_slot<N>].mp4``,
6. returns the *relative path* (under ``<media_root>``) so the caller
   can stash it in the version's ``edited_file_*_path`` Char column.

The HTTP controller streams those files back to the browser directly
via :func:`werkzeug.utils.send_file` (true HTTP-Range, no in-memory
base64 round-trip).

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
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Filesystem locations we'll try if ``shutil.which`` doesn't find the
# binary on the worker's PATH.  This is the single most common reason
# renders fail on a dev install: Homebrew puts ``ffmpeg`` under
# ``/opt/homebrew/bin`` (Apple Silicon) or ``/usr/local/bin`` (Intel),
# but Odoo started inside a Python venv inherits a stripped PATH and
# can't see them.  We probe each candidate up-front and resolve to an
# absolute path so ``subprocess.run`` doesn't have to.
_BINARY_SEARCH_PATHS = (
    "/opt/homebrew/bin",   # macOS Apple Silicon Homebrew
    "/usr/local/bin",      # macOS Intel Homebrew / generic Linux
    "/opt/local/bin",      # MacPorts
    "/usr/bin",            # Linux system packages
    "/bin",                # Linux fallback
)


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
    # Binary resolution
    #
    # We intentionally never trust the worker process's PATH.  On macOS
    # Homebrew installs ffmpeg under /opt/homebrew/bin (Apple Silicon)
    # or /usr/local/bin (Intel), but a venv-launched Odoo gets a
    # stripped PATH and ``subprocess.run(["ffmpeg", ...])`` raises
    # FileNotFoundError on the FIRST call — the only signal the user
    # sees today is a "status=error" stuck on every version with no
    # trimmed file written to disk.
    # ------------------------------------------------------------------
    @api.model
    def _resolve_binary(self, name):
        """Return the absolute path to ``name`` (``ffmpeg`` or ``ffprobe``).

        Resolution order:

        1. ``ir.config_parameter video_qc.<name>_path`` — operator
           override, e.g. when the binary lives in a non-standard
           location.
        2. ``shutil.which(name)`` — honors the worker's PATH if it has
           one we can use.
        3. A fixed list of standard install prefixes (see
           ``_BINARY_SEARCH_PATHS``) — catches the common
           Homebrew/MacPorts/apt cases when Odoo was started without a
           shell that exports PATH.

        Returns ``None`` when not found — the caller decides whether to
        raise a UserError with helpful install instructions.
        """
        icp = self.env["ir.config_parameter"].sudo()
        override = icp.get_param(f"video_qc.{name}_path")
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        resolved = shutil.which(name)
        if resolved:
            return resolved
        for prefix in _BINARY_SEARCH_PATHS:
            candidate = os.path.join(prefix, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    @api.model
    def _require_binary(self, name):
        """Resolve ``name`` or raise a CLEAR UserError with install hints."""
        path = self._resolve_binary(name)
        if path:
            return path
        # Build a helpful message — operators upgrading from a Docker
        # image that doesn't include ffmpeg need to know what to do.
        searched = ", ".join(_BINARY_SEARCH_PATHS)
        raise UserError(_(
            "%(name)s binary not found.\n\n"
            "Install it:\n"
            "  macOS:  brew install ffmpeg\n"
            "  Debian/Ubuntu:  sudo apt-get install -y ffmpeg\n"
            "  RHEL/Fedora:  sudo dnf install -y ffmpeg\n\n"
            "Or set an absolute path via the system parameter "
            "video_qc.%(name)s_path (Settings → Technical → System "
            "Parameters).\n\n"
            "Searched: %(searched)s, plus shutil.which()."
        ) % {"name": name, "searched": searched})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def render_version(self, version, config):
        """Render *version* using *config* (a dict).

        Legacy single-slot entry point.  Returns ``(edited_rel_path,
        preview_rel_path, command, probe)`` — the path strings are
        relative to ``<media_root>`` and are intended to be stashed
        in ``edited_file_path`` / ``preview_file_path`` on the version
        row.  ``preview_rel_path`` is ``""`` when preview generation
        fails (non-fatal).
        """
        if not version.original_attachment_id:
            raise UserError(_("Version has no source attachment."))

        with _tempdir() as workdir:
            src_path = self._dump_attachment(version.original_attachment_id, workdir, "src")
            tmp_out = os.path.join(workdir, "edited.mp4")
            tmp_preview = os.path.join(workdir, "preview.mp4")

            command = self._build_command(src_path, tmp_out, config)
            self._run(command, workdir, task=version.task_id, version=version, op="render")

            preview_rel = ""
            preview_cmd = self._build_preview_command(tmp_out, tmp_preview)
            try:
                self._run(preview_cmd, workdir, task=version.task_id, version=version, op="preview")
                # IMPORTANT: shutil.move BEFORE the ``with _tempdir`` block
                # exits, otherwise the temp tree (and our render) gets
                # wiped at context close.  ``_store`` handles this.
                preview_rel = self._store(version, tmp_preview, kind="preview")
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Preview generation failed, continuing: %s", exc)

            probe = self._probe(tmp_out)
            edited_rel = self._store(version, tmp_out, kind="edited")
            self._record_history(version, config)
            return edited_rel, preview_rel, " ".join(command), probe

    @api.model
    def render_for_attachment(self, version, attachment, config, slot):
        """Render a single ``attachment`` using ``config`` and store the
        result on disk against ``version`` tagged with the slot number.

        Used by the two-slot rendering path so a single version owns a
        trimmed clip for each source slot.  Returns ``(edited_rel_path,
        ffmpeg_command, probe)`` — ``edited_rel_path`` is the
        ``<media_root>``-relative path, to be written into the
        ``edited_file_<slot>_path`` Char column on the version.
        """
        if not attachment:
            raise UserError(_("No source attachment provided for slot #%s.") % slot)
        with _tempdir() as workdir:
            src_path = self._dump_attachment(attachment, workdir, f"src_{slot}")
            tmp_out = os.path.join(workdir, f"edited_slot_{slot}.mp4")
            command = self._build_command(src_path, tmp_out, config)
            self._run(
                command, workdir,
                task=version.task_id, version=version, op=f"render_slot_{slot}",
            )
            probe = self._probe(tmp_out)
            edited_rel = self._store(version, tmp_out, kind="edited", slot=slot)
            self._record_history(version, config, slot=slot)
            return edited_rel, " ".join(command), probe

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------
    def _build_command(self, src, dst, config):
        # Resolve to an absolute path so subprocess.run never has to
        # touch PATH.  ``_require_binary`` raises a CLEAR UserError
        # listing the locations we tried and the install commands.
        ffmpeg = self._require_binary("ffmpeg")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]

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
        ffmpeg = self._require_binary("ffmpeg")
        return [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
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
            # _build_command already resolves to an absolute path via
            # _require_binary, so this shouldn't fire normally — but
            # the binary could have been moved between resolution and
            # invocation.  Surface enough context to debug.
            raise UserError(_(
                "Could not execute %(cmd)s.  Either the binary is missing "
                "or the resolved path is stale.  Install ffmpeg "
                "(brew install ffmpeg / apt-get install -y ffmpeg) or "
                "set ir.config_parameter video_qc.ffmpeg_path to an "
                "absolute path."
            ) % {"cmd": command[0] if command else "ffmpeg"}) from exc
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
            ffprobe = self._require_binary("ffprobe")
            cmd = [ffprobe, "-v", "error", "-print_format", "json",
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
        """Materialise the SOURCE attachment to a file FFmpeg can read.

        Source clips are still stored as ir.attachment rows (that's the
        Instagram downloader's domain) so we have to base64-decode them
        here.  This is the only base64 round-trip remaining on the
        render hot path; the output-side write goes straight to disk
        via :meth:`_store`.
        """
        path = os.path.join(workdir, f"{base}.mp4")
        raw = attachment.raw or (base64.b64decode(attachment.datas) if attachment.datas else b"")
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def _store(self, version, src_path, kind, slot=None):
        """Move the FFmpeg output from its tempdir to its final on-disk home.

        ``src_path`` lives inside the ``_tempdir()`` context manager —
        if we don't move it before the ``with`` block exits, the temp
        tree (and the file) get wiped.  We use :func:`shutil.move` so
        the operation is atomic on the same filesystem and falls back
        to copy+remove across filesystem boundaries.

        Returns the ``<media_root>``-relative path, ready to be stored
        in the version's ``edited_file_*_path`` / ``preview_file_path``
        Char column.

        Decision (per task §MUST NOT DO §1): we do NOT create an
        ir.attachment row here.  The HTTP controller serves the file
        directly from disk through ``werkzeug.utils.send_file``, so
        the attachment indirection is no longer load-bearing.  Legacy
        attachment rows from before this refactor stay in place
        (controller falls back to them when the path field is empty).
        """
        if not os.path.isfile(src_path):
            raise UserError(_("Render output not found at %s") % src_path)
        storage = self.env["video.qc.media.storage"].sudo()
        final_abs = storage.path_for(version, kind, slot=slot)
        os.makedirs(os.path.dirname(final_abs), exist_ok=True)
        # If a previous render of the same (version, kind, slot) tuple
        # left a file at ``final_abs``, ``shutil.move`` would fall back
        # to copy-then-unlink with the destination overwritten.  That's
        # the intended last-writer-wins semantic.  On the *same*
        # filesystem we hit a fast ``os.rename`` instead.
        if os.path.isfile(final_abs):
            try:
                os.remove(final_abs)
            except OSError as exc:
                _logger.warning(
                    "Could not remove stale render at %s: %s", final_abs, exc,
                )
        shutil.move(src_path, final_abs)
        rel = storage.relative(final_abs)
        _logger.info(
            "Stored render: version=%s kind=%s slot=%s -> %s (%d bytes)",
            version.id, kind, slot, rel, os.path.getsize(final_abs),
        )
        return rel

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
