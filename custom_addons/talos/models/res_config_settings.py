# -*- coding: utf-8 -*-
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class TalosConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    talos_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="talos.bedrock_inference_arn",
        help="Full ARN of the AWS Bedrock application inference profile for Kimi K2.5.",
    )
    talos_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="talos.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint (e.g. ap-south-1).",
    )
