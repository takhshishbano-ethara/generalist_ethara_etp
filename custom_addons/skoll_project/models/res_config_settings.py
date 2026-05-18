from odoo import fields, models


class SkollConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

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
