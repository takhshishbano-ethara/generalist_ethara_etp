from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import credential_manager

_API_KEY_PARAM = "i2i.openrouter_api_key"
_MASK = "********"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    i2i_openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        help="API key for OpenRouter. Stored encrypted; the form shows a "
             "mask once set \u2014 submit a new value to rotate.",
    )
    i2i_api_key_is_set = fields.Boolean(
        string="API Key Configured",
        compute="_compute_i2i_api_key_is_set",
    )

    i2i_default_model = fields.Char(
        string="LLM QC Model",
        config_parameter="i2i.default_model",
        default="google/gemini-3.5-flash",
        help="OpenRouter model slug used for vision-based QC review.",
    )
    i2i_http_referer = fields.Char(
        string="HTTP Referer",
        config_parameter="i2i.http_referer",
    )
    i2i_app_title = fields.Char(
        string="App Title",
        config_parameter="i2i.app_title",
        default="Ethara I2I",
    )
    i2i_openrouter_max_retries = fields.Integer(
        string="OpenRouter Max Retries",
        config_parameter="i2i.openrouter_max_retries",
        default=3,
    )
    i2i_usd_per_mtoken = fields.Float(
        string="Cost (USD per 1M tokens)",
        config_parameter="i2i.usd_per_mtoken",
        default=0.20,
        digits=(12, 6),
        help="Used to compute approximate cost from token usage.",
    )

    def _compute_i2i_api_key_is_set(self):
        for rec in self:
            rec.i2i_api_key_is_set = bool(
                self.env["ir.config_parameter"].sudo().get_param(_API_KEY_PARAM)
            )

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        existing = ICP.get_param(_API_KEY_PARAM, "")
        res["i2i_openrouter_api_key"] = _MASK if existing else ""
        return res

    def set_values(self):
        super().set_values()
        submitted = (self.i2i_openrouter_api_key or "").strip()
        if submitted and submitted != _MASK:
            credential_manager.set_encrypted_param(
                self.env, _API_KEY_PARAM, submitted,
            )

    @api.constrains("i2i_openrouter_max_retries", "i2i_usd_per_mtoken")
    def _check_i2i_limits(self):
        for rec in self:
            if not (1 <= rec.i2i_openrouter_max_retries <= 10):
                raise ValidationError(_(
                    "OpenRouter Max Retries must be between 1 and 10."
                ))
            if rec.i2i_usd_per_mtoken < 0:
                raise ValidationError(_(
                    "Cost per 1M tokens must be non-negative."
                ))
