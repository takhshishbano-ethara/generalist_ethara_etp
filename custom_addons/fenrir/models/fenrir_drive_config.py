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

    # ── Computed display helpers ─────────────────────────────────────────
    @api.depends("parent_folder_id")
    def _compute_parent_folder_url(self):
        for rec in self:
            rec.parent_folder_url = (
                f"https://drive.google.com/drive/folders/{rec.parent_folder_id}"
                if rec.parent_folder_id else False)

    @api.depends("auth_method", "parent_folder_id",
                 "oauth_client_id", "oauth_client_secret", "oauth_refresh_token",
                 "service_account_json")
    def _compute_is_configured(self):
        for rec in self:
            if not rec.parent_folder_id:
                rec.is_configured = False
            elif rec.auth_method == "oauth":
                rec.is_configured = bool(
                    rec.oauth_client_id and rec.oauth_client_secret
                    and rec.oauth_refresh_token)
            else:
                rec.is_configured = bool(rec.service_account_json)

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
        """Verify saved credentials work by fetching parent folder metadata."""
        self.ensure_one()
        try:
            service, parent_id = self.env["fenrir.drive.service"]._build_client()
            info = service.files().get(
                fileId=parent_id, fields="id, name, mimeType",
                supportsAllDrives=True).execute()
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_(
                "Drive API call failed: %s\n\n"
                "Common causes:\n"
                "  • For Service Account: parent folder isn't in a Shared Drive.\n"
                "  • For OAuth: refresh token expired or revoked.\n"
                "  • Folder ID is wrong (copy from drive.google.com/drive/folders/<id>).\n"
                "  • Drive API not enabled in the GCP project.") % exc
            ) from exc
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection OK"),
                "message": _("Reached folder: %s") % info.get("name", parent_id),
                "sticky": False,
                "type": "success",
            },
        }
