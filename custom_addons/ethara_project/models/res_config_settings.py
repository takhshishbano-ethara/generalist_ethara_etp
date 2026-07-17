from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ethara_aws_pricing_region = fields.Char(
        string="AWS Pricing Region",
        config_parameter="ethara_project.aws_pricing.region",
        default="us-east-1",
    )
    ethara_aws_pricing_cache_ttl_hours = fields.Integer(
        string="AWS Pricing Cache TTL (hours)",
        config_parameter="ethara_project.aws_pricing.cache_ttl_hours",
        default=24,
    )
    ethara_cost_fetch_months = fields.Integer(
        string="Default Cost Fetch Months",
        config_parameter="ethara_project.cost_fetch.months",
        default=6,
    )
    ethara_alert_warning_pct = fields.Float(
        string="Warning Threshold (%)",
        config_parameter="ethara_project.alert.warning_pct",
        default=80.0,
    )
    ethara_alert_over_budget_pct = fields.Float(
        string="Over-Budget Threshold (%)",
        config_parameter="ethara_project.alert.over_budget_pct",
        default=100.0,
    )
    ethara_provider_openrouter_enabled = fields.Boolean(
        string="Enable OpenRouter Cost Fetch",
        config_parameter="ethara_project.provider.openrouter_enabled",
    )
    ethara_provider_moonshot_enabled = fields.Boolean(
        string="Enable Moonshot Cost Fetch",
        config_parameter="ethara_project.provider.moonshot_enabled",
    )
    ethara_provider_openai_enabled = fields.Boolean(
        string="Enable OpenAI Cost Fetch",
        config_parameter="ethara_project.provider.openai_enabled",
    )
    ethara_provider_gcp_enabled = fields.Boolean(
        string="Enable GCP BigQuery Cost Fetch",
        config_parameter="ethara_project.provider.gcp_enabled",
    )

    ethara_aws_access_key_id = fields.Char(string="AWS Access Key ID")
    ethara_aws_secret_access_key = fields.Char(string="AWS Secret Access Key")
    ethara_aws_region_name = fields.Char(string="AWS Region")
    ethara_aws_synced_service_count = fields.Integer(
        string="AWS Services Synced", readonly=True,
    )
    ethara_aws_last_sync_summary = fields.Char(
        string="Last Sync", readonly=True,
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        creds = self.env["ethara.project.aws.credentials"].sudo().get_singleton()
        synced_count = self.env["ethara.project.infra.type"].sudo().search_count([
            ("is_aws_managed", "=", True),
        ])
        last_log = self.env["ethara.project.aws.pricing.sync.log"].sudo().search(
            [], limit=1, order="started_at desc",
        )
        if last_log:
            summary = "%s services synced — last run %s at %s" % (
                synced_count,
                last_log.status or "-",
                last_log.started_at
                and last_log.started_at.strftime("%Y-%m-%d %H:%M UTC")
                or "-",
            )
        else:
            summary = "%s services synced — never synced yet" % synced_count
        res.update({
            "ethara_aws_access_key_id": creds.access_key_id or "",
            "ethara_aws_secret_access_key": creds.secret_key or "",
            "ethara_aws_region_name": creds.region_name or "us-east-1",
            "ethara_aws_synced_service_count": synced_count,
            "ethara_aws_last_sync_summary": summary,
        })
        return res

    def set_values(self):
        super().set_values()
        creds = self.env["ethara.project.aws.credentials"].sudo().get_singleton()
        creds.write({
            "access_key_id": (self.ethara_aws_access_key_id or "").strip(),
            "secret_key": (self.ethara_aws_secret_access_key or "").strip(),
            "region_name": (self.ethara_aws_region_name or "us-east-1").strip(),
        })

    def action_trigger_aws_sync(self):
        self.ensure_one()
        creds = self.env["ethara.project.aws.credentials"].sudo().get_singleton()
        if not creds.access_key_id or not creds.secret_key:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "AWS sync unavailable",
                    "message": (
                        "Store both Access Key ID and Secret Access Key "
                        "before triggering a sync."
                    ),
                    "type": "warning",
                    "sticky": False,
                },
            }
        from odoo.addons.ethara_project.services import aws_pricing_service as ps
        log = ps.action_sync_all(
            self.env, triggered_by="manual", user_id=self.env.user.id,
        )
        self.env.cr.commit()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AWS sync %s" % (log.status or "?"),
                "message": "Services upserted: %s" % (log.services_upserted or 0),
                "type": "success" if log.status == "success" else "warning",
                "sticky": False,
            },
        }
