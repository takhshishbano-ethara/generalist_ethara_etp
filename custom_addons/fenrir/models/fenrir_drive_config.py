"""Google Drive configuration — persistent singleton record.

Stored in its own table (not TransientModel) so it has a real DB row that
shows up in a list view and survives sessions. A singleton guard prevents
multiple rows.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_logger = logging.getLogger(__name__)

# Legacy ir.config_parameter keys — migrated into the model record on first access.
PARAM_SERVICE_ACCOUNT = "fenrir.drive.service_account_json"
PARAM_PARENT_FOLDER = "fenrir.drive.parent_folder_id"
PARAM_OAUTH_CLIENT_ID = "fenrir.drive.oauth_client_id"
PARAM_OAUTH_CLIENT_SECRET = "fenrir.drive.oauth_client_secret"
PARAM_OAUTH_REFRESH_TOKEN = "fenrir.drive.oauth_refresh_token"
PARAM_S3_BUCKET = "fenrir.s3.bucket"
PARAM_S3_REGION = "fenrir.s3.region"
PARAM_S3_FOLDER = "fenrir.s3.folder"
PARAM_S3_KEY = "fenrir.s3.access_key_id"
PARAM_S3_SECRET = "fenrir.s3.secret_access_key"
PARAM_S3_ENDPOINT = "fenrir.s3.endpoint_url"
PARAM_S3_EXPIRY = "fenrir.s3.presigned_url_expiry_days"


class FenrirDriveConfig(models.Model):
    _name = "fenrir.drive.config"
    _description = "Fenrir — Google Drive Configuration"
    _rec_name = "name"

    name = fields.Char(default="Google Drive", required=True)

    auth_method = fields.Selection(
        selection=[
            ("oauth", "OAuth User (works on personal / free Gmail)"),
            ("service_account", "Service Account (Shared Drives only)"),
        ],
        string="Auth Method",
        default="oauth",
        required=True,
        help="OAuth = uploads happen as a real Google user (free Gmail OK).\n"
             "Service Account = only works for Google Workspace Shared Drives.")

    parent_folder_id = fields.Char(
        string="Parent Folder ID",
        help="Drive folder ID where each <TASK_ID>/ folder will be created. "
             "Copy it from drive.google.com/drive/folders/<this part>.")
    parent_folder_url = fields.Char(
        string="Open in Drive",
        compute="_compute_parent_folder_url")
    is_configured = fields.Boolean(
        string="Configured",
        compute="_compute_is_configured",
        help="True when all required fields for the chosen auth method are set.")

    service_account_json = fields.Text(
        string="Service Account JSON",
        help="Full JSON contents of a GCP service account key. "
             "Only works when the parent folder is inside a Shared Drive.")

    oauth_client_id = fields.Char(string="OAuth Client ID")
    oauth_client_secret = fields.Char(string="OAuth Client Secret")
    oauth_refresh_token = fields.Char(
        string="OAuth Refresh Token",
        help="Obtained by running scripts/authorize_drive.py once.")

    # ── S3 storage for uploaded binaries ─────────────────────────────────
    s3_bucket = fields.Char(string="S3 Bucket",
                            help="Bucket name, e.g. production-grtlabs-tag.")
    s3_region = fields.Char(string="S3 Region", default="us-east-1")
    s3_folder = fields.Char(
        string="S3 Folder Prefix",
        help="Prefix under which task folders are created. "
             "Empty = uploads land at bucket root.")
    s3_access_key_id = fields.Char(string="S3 Access Key ID")
    s3_secret_access_key = fields.Char(
        string="S3 Secret Access Key", help="Stored as-is in ir.config_parameter.")
    s3_endpoint_url = fields.Char(
        string="S3 Endpoint URL",
        help="Leave empty for AWS S3. Set for S3-compatible services "
             "(Cloudflare R2, MinIO, Backblaze B2, etc.).")
    s3_presigned_url_expiry_days = fields.Integer(
        string="Presigned URL Expiry (days)",
        default=7,
        help="How long the Drive-side download links stay valid. "
             "AWS caps at 7 days for SigV4.")

    # ── Computed display helpers ─────────────────────────────────────────
    @api.depends("parent_folder_id")
    def _compute_parent_folder_url(self):
        for rec in self:
            rec.parent_folder_url = (
                f"https://drive.google.com/drive/folders/{rec.parent_folder_id}"
                if rec.parent_folder_id else False)

    @api.depends("auth_method", "parent_folder_id",
                 "oauth_client_id", "oauth_client_secret", "oauth_refresh_token",
                 "service_account_json",
                 "s3_bucket", "s3_access_key_id", "s3_secret_access_key")
    def _compute_is_configured(self):
        for rec in self:
            drive_ok = bool(rec.parent_folder_id) and (
                (rec.auth_method == "oauth"
                 and rec.oauth_client_id and rec.oauth_client_secret
                 and rec.oauth_refresh_token)
                or (rec.auth_method == "service_account"
                    and rec.service_account_json))
            s3_ok = bool(rec.s3_bucket and rec.s3_access_key_id
                         and rec.s3_secret_access_key)
            rec.is_configured = drive_ok and s3_ok

    # ── Singleton enforcement ────────────────────────────────────────────
    @api.constrains("name")
    def _check_singleton(self):
        others = self.search_count([("id", "not in", self.ids)])
        if self and others > 0:
            raise ValidationError(_(
                "Only one Google Drive configuration is allowed."))

    @api.model
    def get_singleton(self):
        """Return the one config record, creating + migrating from
        ir.config_parameter on first access."""
        rec = self.search([], limit=1)
        if rec:
            return rec
        ICP = self.env["ir.config_parameter"].sudo()
        vals = {
            "name": "Google Drive",
            "parent_folder_id": ICP.get_param(PARAM_PARENT_FOLDER, ""),
            "service_account_json": ICP.get_param(PARAM_SERVICE_ACCOUNT, ""),
            "oauth_client_id": ICP.get_param(PARAM_OAUTH_CLIENT_ID, ""),
            "oauth_client_secret": ICP.get_param(PARAM_OAUTH_CLIENT_SECRET, ""),
            "oauth_refresh_token": ICP.get_param(PARAM_OAUTH_REFRESH_TOKEN, ""),
        }
        vals["auth_method"] = (
            "oauth" if vals["oauth_refresh_token"] else "service_account"
            if vals["service_account_json"] else "oauth")
        return self.create(vals)

    # ── Actions ──────────────────────────────────────────────────────────
    def action_open_singleton(self):
        """Entry point from the menu — auto-create row if missing, open form."""
        rec = self.get_singleton()
        return {
            "type": "ir.actions.act_window",
            "name": "Google Drive",
            "res_model": self._name,
            "view_mode": "form",
            "res_id": rec.id,
            "target": "current",
        }

    @api.constrains("service_account_json")
    def _check_service_account_json(self):
        for rec in self:
            raw = (rec.service_account_json or "").strip()
            if not raw:
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError(_(
                    "Service Account JSON is not valid JSON: %s") % exc)

    def action_test_connection(self):
        """Verify both Drive and S3 credentials by hitting each service once."""
        self.ensure_one()
        results = []

        # Drive
        try:
            service, parent_id = self.env["fenrir.drive.service"]._build_client()
            info = service.files().get(
                fileId=parent_id, fields="id, name, mimeType",
                supportsAllDrives=True).execute()
            results.append(_("Drive OK — folder: %s") % info.get("name", parent_id))
        except UserError as exc:
            results.append(_("Drive FAILED — %s") % exc.args[0])
        except Exception as exc:  # noqa: BLE001
            results.append(_("Drive FAILED — %s") % exc)

        # S3
        try:
            s3 = self.env["fenrir.s3.service"]
            client, bucket, _folder = s3._build_client()
            client.head_bucket(Bucket=bucket)
            results.append(_("S3 OK — bucket: %s reachable") % bucket)
        except UserError as exc:
            results.append(_("S3 FAILED — %s") % exc.args[0])
        except Exception as exc:  # noqa: BLE001
            results.append(_("S3 FAILED — %s") % exc)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection check"),
                "message": "\n".join(results),
                "sticky": True,
                "type": "info",
            },
        }
