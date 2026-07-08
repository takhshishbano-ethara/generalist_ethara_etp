from odoo import api, fields, models

CONFIG_PARAM_DEFAULT_APPROVERS = "etp_projects.default_approver_user_ids"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    etp_default_approver_user_ids = fields.Many2many(
        "res.users",
        "etp_projects_default_approver_settings_rel",
        "config_id",
        "user_id",
        string="Default Project Budget Approvers",
        help="Users automatically added as approvers when a new Project Budget is created "
             "(both via the Odoo form view and the /api/v1/etp_projects/project_budget/create API). "
             "API callers can supply additional approver_ids — they are merged with this list.",
    )

    etp_aws_is_enabled = fields.Boolean(string="Enable AWS Integration")
    etp_aws_access_key_id = fields.Char(string="AWS Access Key ID")
    etp_aws_secret_access_key = fields.Char(string="AWS Secret Access Key")
    etp_aws_region_name = fields.Char(string="AWS Region")
    etp_aws_secret_is_set = fields.Boolean(string="Secret Stored", readonly=True)
    etp_aws_synced_service_count = fields.Integer(string="AWS Services Synced", readonly=True)
    etp_aws_last_sync_summary = fields.Char(string="Last Sync", readonly=True)

    @api.model
    def get_values(self):
        res = super().get_values()
        ids = _parse_default_approver_ids(
            self.env["ir.config_parameter"].sudo().get_param(
                CONFIG_PARAM_DEFAULT_APPROVERS, "",
            )
        )
        existing = self.env["res.users"].sudo().browse(ids).exists().ids if ids else []
        res["etp_default_approver_user_ids"] = [(6, 0, existing)]

        creds = self.env["etp.aws.credentials"].sudo().get_singleton()
        synced_count = self.env["etp.infra.type"].sudo().search_count([
            ("is_aws_managed", "=", True),
        ])
        last_log = self.env["etp.aws.pricing.sync.log"].sudo().search(
            [], limit=1, order="started_at desc",
        )
        if last_log:
            summary = "%s — %s (services: %s, items: %s)" % (
                last_log.started_at and last_log.started_at.strftime("%Y-%m-%d %H:%M UTC") or "-",
                last_log.status or "-",
                last_log.services_upserted or 0,
                last_log.items_upserted or 0,
            )
        else:
            summary = "Never synced"
        res.update({
            "etp_aws_is_enabled": creds.is_enabled,
            "etp_aws_access_key_id": _mask_access_key(creds.access_key_id),
            "etp_aws_secret_access_key": "•" * 16 if creds.secret_key_is_set else "",
            "etp_aws_region_name": creds.region_name or "us-east-1",
            "etp_aws_secret_is_set": creds.secret_key_is_set,
            "etp_aws_synced_service_count": synced_count,
            "etp_aws_last_sync_summary": summary,
        })
        return res

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            CONFIG_PARAM_DEFAULT_APPROVERS,
            ",".join(str(i) for i in self.etp_default_approver_user_ids.ids),
        )

        creds = self.env["etp.aws.credentials"].sudo().get_singleton()
        write_vals = {
            "is_enabled": self.etp_aws_is_enabled,
            "region_name": self.etp_aws_region_name or "us-east-1",
        }
        # Mask-only submission = user did not touch the field; keep stored value.
        new_access = (self.etp_aws_access_key_id or "").strip()
        if "•" not in new_access:
            write_vals["access_key_id"] = new_access
        new_secret = (self.etp_aws_secret_access_key or "").strip()
        if new_secret and "•" not in new_secret:
            write_vals["secret_key"] = new_secret
        creds.write(write_vals)

    def action_trigger_aws_sync(self):
        self.ensure_one()
        creds = self.env["etp.aws.credentials"].sudo().get_singleton()
        if not creds.is_enabled or not creds.access_key_id or not creds.secret_key_is_set:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "AWS sync unavailable",
                    "message": "Enable the integration and store both Access Key ID and Secret Access Key before triggering a sync.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        from odoo.addons.etp_projects.services import aws_pricing_service as ps
        log = ps.action_sync_all(
            self.env, triggered_by="manual", user_id=self.env.user.id,
        )
        self.env.cr.commit()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AWS sync %s" % (log.status or "?"),
                "message": "Services: %s upserted / Items: %s upserted / API calls: %s" % (
                    log.services_upserted or 0,
                    log.items_upserted or 0,
                    log.api_call_count or 0,
                ),
                "type": "success" if log.status == "success" else "warning",
                "sticky": False,
            },
        }


def _parse_default_approver_ids(raw):
    if not raw:
        return []
    ids = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            ids.append(int(token))
    return ids


def _mask_access_key(key):
    if not key:
        return ""
    return "•" * len(key)
