# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SkollConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    skoll_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="skoll.bedrock_inference_arn",
        help="Full ARN of the AWS Bedrock application inference profile for Skoll LLM calls.",
    )
    skoll_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="skoll.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint (e.g. ap-south-1).",
    )
    skoll_qc_inference_arn = fields.Char(
        string="QC Model ARN (Kimi K2.5)",
        config_parameter="skoll.qc_inference_arn",
        help="Inference profile ARN for Kimi K2.5 used for QC reviews.",
    )
