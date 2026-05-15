# -*- coding: utf-8 -*-
"""On-disk media storage for trimmed/cropped video renders.

This service is the single source of truth for *where* the FFmpeg
processor writes its outputs and *which* paths the HTTP streaming
controller is allowed to read back.

Directory layout::

    <media_root>/
      <task.id>/                          # integer, NOT task.name (which has "/")
        v<version_no>_edited_slot1.mp4    # per-slot trim
        v<version_no>_edited_slot2.mp4
        v<version_no>_edited.mp4          # legacy single-slot render
        v<version_no>_preview.mp4         # low-bitrate preview

``<media_root>`` is configurable via the ``ir.config_parameter`` key
``video_qc.media_root``.  The default is ``<odoo data_dir>/video_qc_media``
so that on a typical dev install (macOS / Linux user-mode Odoo) the
process can already write to it — Odoo creates and owns ``data_dir``
on startup.  On a hardened production install where the admin moves
``data_dir`` somewhere else, the default follows automatically.

Why a separate service
----------------------
Two callers need the exact same path arithmetic:

* :class:`~odoo.addons.instagram_video_qc_manager.services.ffmpeg_processor.FFmpegProcessor`
  needs to know *where* to write its output.
* :class:`~odoo.addons.instagram_video_qc_manager.controllers.main.VideoQCController`
  needs to validate that a stored relative path resolves under
  ``<media_root>`` (and not, say, ``../../etc/passwd``) before opening
  the file for streaming.

Centralising the logic guarantees both sides agree on the security
posture and the layout.  This mirrors the style of
``custom_addons/aurora/controllers/file_viewer.py`` (lines 28-43) and
``custom_addons/aurora/models/dataset_resolver.py`` (lines 46-61),
which are the canonical precedents for filesystem-backed assets in
this codebase.
"""

import logging
import os
import tempfile

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)


CONFIG_PARAM_KEY = "video_qc.media_root"


def _default_media_root():
    """Best-effort default that the Odoo process can actually write to.

    Tries, in order:

    1. ``<config['data_dir']>/video_qc_media`` — the canonical Odoo
       writable area (auto-created by the server on startup; owned by
       the user that runs Odoo, so no chown drama on dev installs).
    2. ``<tempfile.gettempdir()>/odoo_video_qc_media`` — fallback if
       config['data_dir'] is unset or unwritable for some reason.

    The previous default ``/var/lib/odoo/video_qc_media`` failed in
    practice because on a typical macOS / Linux dev box Odoo runs as
    the developer's user, which cannot create directories under
    ``/var/lib``.  Renders then fell into the silent error branch and
    the trimmed file never reached disk.
    """
    data_dir = config.get("data_dir")
    if data_dir:
        return os.path.join(data_dir, "video_qc_media")
    return os.path.join(tempfile.gettempdir(), "odoo_video_qc_media")


class MediaStorage(models.AbstractModel):
    _name = "video.qc.media.storage"
    _description = "Video QC On-Disk Media Storage"

    # ------------------------------------------------------------------
    # Roots & directories
    # ------------------------------------------------------------------
    @api.model
    def get_media_root(self):
        """Return the absolute, real-path media root.

        Reads ``ir.config_parameter.video_qc.media_root`` if the admin
        set one, otherwise falls back to :func:`_default_media_root`.
        Self-heals: creates the directory on first call.  If the
        configured path is unwritable (e.g. a fresh install left the
        ``/var/lib/odoo/...`` default in place) we **log a clear
        warning and silently fall back to the data_dir-based default**
        so renders don't fail with an opaque PermissionError at the
        end of a post-commit chain.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        configured = ICP.get_param(CONFIG_PARAM_KEY) or ""
        default = _default_media_root()
        candidates = []
        if configured:
            candidates.append(configured)
        # Always include the default as a fallback, even if configured.
        if default not in candidates:
            candidates.append(default)
        # Last-ditch: tmp dir.
        tmp_fallback = os.path.join(tempfile.gettempdir(), "odoo_video_qc_media")
        if tmp_fallback not in candidates:
            candidates.append(tmp_fallback)

        last_error = None
        for candidate in candidates:
            try:
                os.makedirs(candidate, exist_ok=True)
                # Confirm we can actually write a file (catches
                # cases where the dir exists but is read-only).
                probe = os.path.join(candidate, ".video_qc_write_probe")
                with open(probe, "wb") as fh:
                    fh.write(b"ok")
                os.remove(probe)
            except OSError as exc:
                last_error = exc
                _logger.warning(
                    "video_qc media root %r is not writable (%s); trying next fallback.",
                    candidate, exc,
                )
                continue
            real = os.path.realpath(candidate)
            # If the admin's configured root was unwritable, persist
            # the working fallback so subsequent renders skip the
            # probe loop and the UI shows where files actually go.
            if configured and configured != candidate:
                _logger.warning(
                    "video_qc.media_root was set to %r but is unwritable; "
                    "auto-switched to %r and saved to ir.config_parameter.",
                    configured, real,
                )
                ICP.set_param(CONFIG_PARAM_KEY, real)
            return real

        # Every candidate failed — raise so the caller's error path
        # surfaces clearly rather than retrying indefinitely.
        raise UserError(_(
            "No writable media root: tried %s. Last error: %s"
        ) % (", ".join(candidates), last_error))

    @api.model
    def task_dir(self, task):
        """Return ``<media_root>/<task.id>/`` (created on demand).

        The directory uses ``task.id`` (integer), NOT ``task.name``,
        because the sequence prefix ``VQC/%(year)s/`` contains ``/``
        which is filesystem-unfriendly.
        """
        if not task or not task.id:
            raise UserError(_("Cannot resolve media directory for an unsaved task."))
        path = os.path.join(self.get_media_root(), str(int(task.id)))
        os.makedirs(path, exist_ok=True)
        return path

    @api.model
    def path_for(self, version, kind, slot=None):
        """Canonical absolute path for an FFmpeg output.

        :param version: a ``video.task.version`` recordset (single).
        :param str kind: ``"edited"`` or ``"preview"``.
        :param int|None slot: ``1`` or ``2`` for per-slot trims;
            ``None`` for the legacy single-slot edited output.
        """
        if not version or not version.id:
            raise UserError(_("Cannot resolve media path for an unsaved version."))
        if kind not in ("edited", "preview"):
            raise UserError(_("Unknown media kind: %s") % kind)
        slot_suffix = f"_slot{int(slot)}" if slot else ""
        filename = f"v{int(version.version_no)}_{kind}{slot_suffix}.mp4"
        return os.path.join(self.task_dir(version.task_id), filename)

    # ------------------------------------------------------------------
    # Path <-> relative conversions (for storage in Char columns)
    # ------------------------------------------------------------------
    @api.model
    def relative(self, path):
        """Convert an absolute path to a stable rel-path under <media_root>.

        Stored in the Char columns so the database stays portable
        across hosts that may relocate the root.  Returns ``""`` when
        ``path`` is falsy.
        """
        if not path:
            return ""
        root = self.get_media_root()
        real = os.path.realpath(path)
        # We INTENTIONALLY do not raise here: the caller is the
        # ffmpeg_processor and we trust it.  The traversal guard lives
        # in :meth:`absolute`, which is what the HTTP layer calls back.
        return os.path.relpath(real, root)

    @api.model
    def absolute(self, rel):
        """Resolve a relative path back to an absolute path, with guard.

        Implements the canonical realpath + ``startswith(allowed_base + os.sep)``
        traversal guard borrowed verbatim from
        ``custom_addons/aurora/controllers/file_viewer.py`` lines 28-43.

        Raises :class:`~odoo.exceptions.UserError` (NOT ``AccessError``)
        so the HTTP controller can convert the rejection to a clean
        404 without leaking the reason.

        :param str rel: relative path as stored on the version row.
        :return: absolute, real path safe to ``open(..., 'rb')``.
        :raises UserError: if the path escapes ``<media_root>``.
        """
        if not rel:
            raise UserError(_("Empty media path."))
        root = self.get_media_root()
        candidate = os.path.join(root, rel)
        try:
            real_path = os.path.realpath(candidate)
            real_base = os.path.realpath(root)
        except (OSError, ValueError) as exc:
            raise UserError(_("Invalid media path.")) from exc
        if not real_base:
            raise UserError(_("Media root is not configured."))
        # Exact match OR under-the-base — matches aurora's _is_path_under_base.
        if real_path != real_base and not real_path.startswith(real_base + os.sep):
            _logger.warning(
                "Rejected path-traversal attempt: rel=%r resolved=%r outside root=%r",
                rel, real_path, real_base,
            )
            raise UserError(_("Path escapes the media root."))
        return real_path
