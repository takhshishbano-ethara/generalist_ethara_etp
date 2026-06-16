import base64
import io
import logging
import mimetypes
import re

from odoo import _, api, fields, models


_logger = logging.getLogger(__name__)


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "file"


def _norm_filename(name):
    return _slug(name).lower()


class FenrirTaskAttachment(models.Model):
    _name = "fenrir.task.attachment"
    _description = "Fenrir Task Attachment"
    _order = "task_id, folder, sequence, id"
    _rec_name = "file_name"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    file_name = fields.Char(string="File Name", required=True)
    attachment = fields.Binary(string="Attachment", attachment=True)
    description = fields.Char(string="Description",
                              help="Optional short description / caption")
    folder = fields.Selection(
        selection=[
            ("root", "Root"),
            ("resources", "Resources"),
            ("tests", "Tests"),
            ("environment", "Environment"),
            ("data", "Data"),
        ],
        string="Export Folder",
        default="resources",
        required=True,
        help="Target subfolder in the strict task export. "
             "Use 'Root' for files that belong at the top of the task tree "
             "(e.g. task_metadata.json, license.json).",
    )
    is_generated = fields.Boolean(
        string="Auto-generated",
        default=False,
        help="True for files produced by the submit-time generator. "
             "These are wiped and rebuilt on every (re)submit.",
    )
    license = fields.Selection(
        selection=[
            ("self_created", "Self-created"),
            ("public_domain", "Public Domain"),
            ("cc0", "CC0"),
            ("cc_by", "CC-BY"),
            ("cc_by_sa", "CC-BY-SA"),
            ("mit", "MIT"),
            ("apache_2", "Apache 2.0"),
            ("proprietary", "Proprietary"),
            ("other", "Other"),
        ],
        string="License",
        default="self_created",
        required=True,
        help="License under which this asset is provided.",
    )
    source_url = fields.Char(
        string="Source URL",
        help="Where the asset originates from (leave blank if self-created).",
    )
    notes = fields.Text(
        string="Notes",
        help="Free-form notes about this asset; emitted as the 'notes' field in license.json.",
    )

    s3_key = fields.Char(
        string="S3 Key",
        readonly=True,
        copy=False,
        help="Object key in the configured S3 bucket. Set automatically when "
             "the file is pushed to S3 at attach time.",
    )
    s3_uploaded_at = fields.Datetime(
        string="S3 Uploaded At",
        readonly=True,
        copy=False,
    )
    file_size = fields.Integer(
        string="File Size (bytes)",
        readonly=True,
        copy=False,
    )

    _LICENSE_LABELS = {
        "self_created": "Self-created",
        "public_domain": "Public Domain",
        "cc0": "CC0",
        "cc_by": "CC-BY",
        "cc_by_sa": "CC-BY-SA",
        "mit": "MIT",
        "apache_2": "Apache 2.0",
        "proprietary": "Proprietary",
        "other": "Other",
    }

    def license_label(self):
        self.ensure_one()
        return self._LICENSE_LABELS.get(self.license or "self_created", "Self-created")

    # ── S3 push-on-create plumbing ───────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("fenrir_skip_s3_push"):
            for rec in records:
                rec._maybe_push_to_s3()
        return records

    def write(self, vals):
        result = super().write(vals)
        if vals.get("attachment") and not self.env.context.get("fenrir_skip_s3_push"):
            for rec in self:
                rec._maybe_push_to_s3()
        return result

    def _maybe_push_to_s3(self):
        """If this record holds bytes (and isn't generated), push them to
        S3 and drop the local binary copy so the Odoo filestore doesn't
        keep big payloads around."""
        self.ensure_one()
        if self.is_generated:
            return
        if not self.attachment:
            return
        if self.s3_key:
            return
        try:
            raw = base64.b64decode(self.attachment)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Fenrir: could not decode attachment %s for S3 push", self.id)
            return

        key = self._compute_s3_key()
        mime = mimetypes.guess_type(self.file_name or "")[0] \
            or "application/octet-stream"
        try:
            self.env["fenrir.s3.service"].upload_bytes(key, raw, mime)
        except Exception:  # noqa: BLE001
            # Leave the local copy in place so the file isn't lost. The
            # approve-time mirror will retry later.
            _logger.exception(
                "Fenrir: S3 push at attach-time failed for attachment %s "
                "(file %r); keeping local copy",
                self.id, self.file_name)
            return

        self.with_context(fenrir_skip_s3_push=True).write({
            "s3_key": key,
            "s3_uploaded_at": fields.Datetime.now(),
            "file_size": len(raw),
            "attachment": False,
        })
        _logger.info(
            "Fenrir: pushed attachment %s (%s) to S3 at %s — local copy cleared",
            self.id, self.file_name, key)

    def _compute_s3_key(self):
        """Derive the S3 object key matching the eventual task export layout."""
        self.ensure_one()
        raw_name = self.file_name or f"attachment_{self.id}"
        folder = self.folder or "resources"
        safe_name = (_slug(raw_name)
                     if folder in ("environment", "tests")
                     else _norm_filename(raw_name))
        rel_path = safe_name if folder == "root" else f"{folder}/{safe_name}"

        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        s3_folder = (config.s3_folder or "").strip().strip("/")
        task = self.task_id
        task_code = task.code or f"task_{task.id}"
        s3_prefix = f"{s3_folder}/{task_code}" if s3_folder else task_code
        return f"{s3_prefix}/{rel_path}"

    def _fetch_bytes(self):
        """Return raw bytes for this attachment: prefer the local binary,
        otherwise download from S3."""
        self.ensure_one()
        if self.attachment:
            return base64.b64decode(self.attachment)
        if self.s3_key:
            return self.env["fenrir.s3.service"].download_bytes(self.s3_key)
        return b""

    def has_content(self):
        self.ensure_one()
        return bool(self.attachment or self.s3_key)

    def unlink(self):
        """Best-effort delete of the S3 object alongside the DB row."""
        s3 = self.env["fenrir.s3.service"]
        to_delete = [(rec.id, rec.s3_key) for rec in self if rec.s3_key]
        result = super().unlink()
        for rec_id, key in to_delete:
            try:
                client, bucket, _f = s3._build_client()
                client.delete_object(Bucket=bucket, Key=key)
                _logger.info(
                    "Fenrir: deleted S3 object %s (task attachment %s)",
                    key, rec_id)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Fenrir: failed to delete S3 object %s for task "
                    "attachment %s: %s", key, rec_id, exc)
        return result

    def action_open_s3_download(self):
        """Open a short-lived presigned URL so the user can download the
        file directly from S3 without staging it back in Odoo."""
        self.ensure_one()
        if not self.s3_key:
            return False
        config = self.env["fenrir.drive.config"].sudo().get_singleton()
        days = config.s3_presigned_url_expiry_days or 7
        url = self.env["fenrir.s3.service"].presigned_get_url(
            self.s3_key, days * 24 * 3600)
        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "new",
        }
