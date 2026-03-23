from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── Bedrock API Configuration ─────────────────────────────────────────
    ai_services_bedrock_api_key = fields.Char(
        string="Bedrock API Key",
        config_parameter="ai_services.bedrock_api_key",
        help="AWS Bedrock API Key (starts with ABSK...).",
    )
    ai_services_bedrock_inference_arn = fields.Char(
        string="Inference Profile ARN",
        config_parameter="ai_services.bedrock_inference_arn",
        help="Full ARN of the Bedrock application inference profile, e.g. "
        "arn:aws:bedrock:ap-south-1:ACCOUNT:application-inference-profile/ID",
    )
    ai_services_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="ai_services.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint.",
    )
