# -*- coding: utf-8 -*-
import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import youtube_downloader

_logger = logging.getLogger(__name__)

_MAX_BYTES = 512 * 1024
_ALLOWED_EXTS = (".txt",)
_USER_SLOT_TEMPLATE = "db/%s/user_cookies/user_%d"


class VideoEditorUserCookies(models.Model):
    _name = "video.editor.user.cookies"
    _description = "Per-user YouTube cookies for Crowley Sourcing"
    _rec_name = "user_id"

    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        ondelete="cascade",
        index=True,
        default=lambda self: self.env.user.id,
    )
    cookies_file = fields.Binary(
        string="YouTube Cookies File",
        attachment=True,
        help=(
            "Upload your own Netscape-format YouTube cookies.txt. Export it "
            "from a browser tab where you are signed in to YouTube using the "
            "'Get cookies.txt LOCALLY' (Chrome/Edge) or 'cookies.txt' (Firefox) "
            "extension. Each user uploads their own file so that ingest jobs "
            "you trigger use YOUR session — not another user's."
        ),
    )
    cookies_filename = fields.Char(string="Cookies Filename")
    uploaded_at = fields.Datetime(string="Uploaded At", readonly=True)
    has_cookies = fields.Boolean(
        string="Has Cookies",
        compute="_compute_has_cookies",
        store=False,
    )

    _sql_constraints = [
        (
            "user_uniq",
            "unique(user_id)",
            "Each user can have only one YouTube cookies entry.",
        ),
    ]

    @api.depends("cookies_file")
    def _compute_has_cookies(self):
        for rec in self:
            rec.has_cookies = bool(rec.cookies_file)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            blob = vals.get("cookies_file")
            if blob:
                self._validate_blob(blob, vals.get("cookies_filename"))
                vals["uploaded_at"] = fields.Datetime.now()
        return super().create(vals_list)

    def write(self, vals):
        if "cookies_file" in vals:
            blob = vals["cookies_file"]
            filename = vals.get("cookies_filename")
            if blob:
                self._validate_blob(blob, filename or (self[:1].cookies_filename if self else None))
                vals["uploaded_at"] = fields.Datetime.now()
            else:
                vals["uploaded_at"] = False
                for rec in self:
                    rec._unlink_materialized_file()
        return super().write(vals)

    def unlink(self):
        for rec in self:
            rec._unlink_materialized_file()
        return super().unlink()

    @api.model
    def _validate_blob(self, raw, filename):
        if not raw:
            return
        try:
            if isinstance(raw, str):
                raw_bytes = base64.b64decode(raw.encode("ascii"), validate=True)
            elif isinstance(raw, (bytes, bytearray)):
                raw_bytes = base64.b64decode(bytes(raw), validate=True)
            else:
                raise ValidationError(_("Cookies file payload is not bytes/str."))
        except (ValueError, TypeError) as exc:
            raise ValidationError(_(
                "Cookies file is not valid base64 (%s). Re-upload the file."
            ) % exc) from exc
        if not raw_bytes:
            raise ValidationError(_("Cookies file is empty."))
        if len(raw_bytes) > _MAX_BYTES:
            raise ValidationError(_(
                "Cookies file is too large (max %(max)d KB, got %(got)d KB)."
            ) % {
                "max": _MAX_BYTES // 1024,
                "got": len(raw_bytes) // 1024 + 1,
            })
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(_(
                "Cookies file must be UTF-8 plain text in Netscape format: %s"
            ) % exc) from exc
        lines = text.splitlines()
        first_line = (lines[0].strip().lower() if lines else "")
        if not any(m in first_line for m in youtube_downloader._NETSCAPE_HEADER_MARKERS):
            raise ValidationError(_(
                "Cookies file is not in Netscape format — the first line must "
                "start with '# Netscape HTTP Cookie File'.\n\n"
                "Re-export the file using the 'Get cookies.txt LOCALLY' "
                "extension for Chrome/Edge, or the 'cookies.txt' extension "
                "for Firefox, while signed in to YouTube."
            ))
        if filename:
            fn = filename.lower()
            if not fn.endswith(_ALLOWED_EXTS):
                raise ValidationError(_(
                    "Cookies file must be a .txt file (got %s)."
                ) % filename)

    def _slot_key(self):
        self.ensure_one()
        dbname = (self.env.cr.dbname or "default").strip() or "default"
        return _USER_SLOT_TEMPLATE % (dbname, self.user_id.id)

    def materialize(self):
        """Materialize this user's cookies to disk and return absolute path, or None."""
        self.ensure_one()
        if not self.cookies_file:
            return None
        uploaded_epoch = 0.0
        if self.uploaded_at:
            try:
                uploaded_epoch = self.uploaded_at.timestamp()
            except (AttributeError, ValueError):
                uploaded_epoch = 0.0
        try:
            path = youtube_downloader.materialize_cookies_blob(
                self._slot_key(), self.cookies_file, uploaded_epoch,
            )
        except Exception as exc:
            _logger.warning(
                "Materializing per-user cookies for user %d failed: %s",
                self.user_id.id, exc,
            )
            return None
        return path

    def _unlink_materialized_file(self):
        self.ensure_one()
        try:
            youtube_downloader.unlink_cookies_blob(self._slot_key())
        except Exception as exc:
            _logger.warning(
                "Cleanup of per-user cookies for user %d failed: %s",
                self.user_id.id, exc,
            )

    @api.model
    def get_or_create_for_user(self, user_id=None):
        user_id = user_id or self.env.uid
        rec = self.sudo().search([("user_id", "=", user_id)], limit=1)
        if not rec:
            rec = self.sudo().create({"user_id": user_id})
        return rec

    @api.model
    def get_materialized_path_for_user(self, user_id):
        if not user_id:
            return None
        rec = self.sudo().search([("user_id", "=", user_id)], limit=1)
        if not rec or not rec.cookies_file:
            return None
        return rec.materialize()

    @api.model
    def action_open_mine(self):
        rec = self.get_or_create_for_user(self.env.uid)
        view = self.env.ref("video_editor_s3.view_video_editor_user_cookies_form_self")
        return {
            "name": _("My YouTube Cookies"),
            "type": "ir.actions.act_window",
            "res_model": "video.editor.user.cookies",
            "view_mode": "form",
            "view_id": view.id,
            "res_id": rec.id,
            "target": "current",
        }

    def action_test_cookies(self):
        self.ensure_one()
        if not self.cookies_file:
            raise UserError(_(
                "No cookies file uploaded yet. Upload a Netscape-format "
                "cookies.txt first."
            ))
        path = self.sudo().materialize()
        if not path:
            raise UserError(_(
                "Could not materialize cookies file to disk. Check Odoo "
                "logs and disk permissions on the data directory."
            ))
        youtube_downloader.validate_cookies_file(path)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("YouTube cookies valid"),
                "message": _(
                    "Your cookies file looks good and will be used for "
                    "YouTube jobs you trigger."
                ),
                "sticky": False,
            },
        }

    def action_clear_cookies(self):
        for rec in self:
            rec.write({"cookies_file": False, "cookies_filename": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Cookies cleared"),
                "message": _("Your YouTube cookies file has been removed."),
                "sticky": False,
            },
        }
