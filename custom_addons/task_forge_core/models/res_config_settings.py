from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    kimi_aws_region = fields.Char(
        string='AWS Region',
        config_parameter='task_forge_core.kimi_aws_region',
    )
    kimi_api_key = fields.Char(
        string='Bedrock API Key',
        config_parameter='task_forge_core.kimi_api_key',
    )
    kimi_model_arn = fields.Char(
        string='Bedrock Inference ARN',
        config_parameter='task_forge_core.kimi_model_arn',
    )
