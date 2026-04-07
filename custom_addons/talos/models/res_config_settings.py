# -*- coding: utf-8 -*-
import logging
import subprocess

from odoo import api, fields, models

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
    talos_docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_talos_docker_available",
    )
    talos_deployment_mode = fields.Selection(
        [("local", "Local (Docker Compose)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="talos.deployment_mode",
        default="local",
    )
    talos_sandbox_dir = fields.Char(
        string="Sandbox Directory",
        config_parameter="talos.sandbox_dir",
        help="Absolute path to the Telos Sandbox directory (contains docker/, personas/, scripts/).",
    )
    talos_openclaw_image = fields.Char(
        string="OpenClaw Image URI",
        config_parameter="talos.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
        help="Container image for OpenClaw in K8s mode.",
    )
    talos_litellm_image = fields.Char(
        string="LiteLLM Image URI",
        config_parameter="talos.litellm_image",
        default="ghcr.io/berriai/litellm:main-stable",
    )
    talos_k8s_storage_class = fields.Char(
        string="K8s Storage Class",
        config_parameter="talos.k8s_storage_class",
        default="gp3",
        help="StorageClassName for PersistentVolumeClaims in K8s mode.",
    )
    talos_aws_bearer_token = fields.Char(
        string="AWS Bearer Token (Bedrock)",
        config_parameter="talos.aws_bearer_token",
    )
    talos_aws_region = fields.Char(
        string="AWS Region",
        config_parameter="talos.aws_region",
        default="ap-south-1",
    )
    talos_bedrock_model_arn = fields.Char(
        string="Bedrock Model ARN",
        config_parameter="talos.bedrock_model_arn",
        help="ARN for the Bedrock model used inside OpenClaw containers.",
    )
    talos_litellm_master_key = fields.Char(
        string="LiteLLM Master Key",
        config_parameter="talos.litellm_master_key",
    )
    talos_litellm_db_password = fields.Char(
        string="LiteLLM DB Password",
        config_parameter="talos.litellm_db_password",
    )

    @api.depends_context("uid")
    def _compute_talos_docker_available(self):
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                rec.talos_docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.talos_docker_available = False
