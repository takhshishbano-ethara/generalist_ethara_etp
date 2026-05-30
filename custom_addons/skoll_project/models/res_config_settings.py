from odoo import fields, models


class SkollConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    skoll_deployment_mode = fields.Selection(
        [("local", "Local (Docker)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="skoll.deployment_mode",
        default="local",
    )
    skoll_sonnet_arn = fields.Char(
        string="Sonnet ARN",
        config_parameter="skoll.sonnet_arn",
    )
    skoll_kimi_arn = fields.Char(
        string="Kimi ARN",
        config_parameter="skoll.kimi_arn",
    )
    skoll_opus_arn = fields.Char(
        string="Opus ARN",
        config_parameter="skoll.opus_arn",
    )
    skoll_openclaw_image = fields.Char(
        string="OpenClaw Image",
        config_parameter="skoll.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
    )
    skoll_web_search_provider = fields.Selection(
        [
            ("brave", "Brave"),
            ("disabled", "Disabled"),
        ],
        string="Web Search Provider",
        config_parameter="skoll.web_search_provider",
        default="brave",
    )
    skoll_brave_api_key = fields.Char(
        string="Brave API Key",
        config_parameter="skoll.brave_api_key",
    )
