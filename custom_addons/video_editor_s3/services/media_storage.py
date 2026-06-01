# -*- coding: utf-8 -*-
import logging
import os
import tempfile

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

CONFIG_PARAM_KEY = "video_editor_s3.media_root"


def _default_media_root():
    data_dir = config.get("data_dir")
    if data_dir:
        return os.path.join(data_dir, "video_editor_s3_media")
    return os.path.join(tempfile.gettempdir(), "odoo_video_editor_s3_media")


class MediaStorage(models.AbstractModel):
    _name = "video.editor.s3.media.storage"
    _description = "Crowley Sourcing On-Disk Media Storage"

    @api.model
    def get_media_root(self):
        ICP = self.env["ir.config_parameter"].sudo()
        configured = ICP.get_param(CONFIG_PARAM_KEY) or ""
        default = _default_media_root()
        candidates = []
        if configured:
            candidates.append(configured)
        if default not in candidates:
            candidates.append(default)
        tmp_fallback = os.path.join(tempfile.gettempdir(), "odoo_video_editor_s3_media")
        if tmp_fallback not in candidates:
            candidates.append(tmp_fallback)

        last_error = None
        for candidate in candidates:
            try:
                os.makedirs(candidate, exist_ok=True)
                probe = os.path.join(candidate, ".write_probe")
                with open(probe, "wb") as fh:
                    fh.write(b"ok")
                os.remove(probe)
            except OSError as exc:
                last_error = exc
                _logger.warning(
                    "video_editor_s3 media root %r is not writable (%s); trying next fallback.",
                    candidate, exc,
                )
                continue
            real = os.path.realpath(candidate)
            if configured and configured != candidate:
                _logger.warning(
                    "video_editor_s3.media_root was set to %r but is unwritable; "
                    "auto-switched to %r and saved to ir.config_parameter.",
                    configured, real,
                )
                ICP.set_param(CONFIG_PARAM_KEY, real)
            return real

        raise UserError(_(
            "No writable media root: tried %s. Last error: %s"
        ) % (", ".join(candidates), last_error))

    @api.model
    def project_dir(self, project):
        if not project or not project.id:
            raise UserError(_("Cannot resolve media directory for an unsaved project."))
        path = os.path.join(self.get_media_root(), str(int(project.id)))
        os.makedirs(path, exist_ok=True)
        return path

    @api.model
    def path_for(self, project, kind, version=1, slot=None):
        if not project or not project.id:
            raise UserError(_("Cannot resolve media path for an unsaved project."))
        if kind not in ("source", "edited", "preview"):
            raise UserError(_("Unknown media kind: %s") % kind)
        slot_suffix = f"_slot{int(slot)}" if slot else ""
        filename = f"v{int(version)}_{kind}{slot_suffix}.mp4"
        return os.path.join(self.project_dir(project), filename)

    @api.model
    def relative(self, path):
        if not path:
            return ""
        root = self.get_media_root()
        real = os.path.realpath(path)
        return os.path.relpath(real, root)

    @api.model
    def absolute(self, rel):
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
        if real_path != real_base and not real_path.startswith(real_base + os.sep):
            _logger.warning(
                "Rejected path-traversal attempt: rel=%r resolved=%r outside root=%r",
                rel, real_path, real_base,
            )
            raise UserError(_("Path escapes the media root."))
        return real_path
