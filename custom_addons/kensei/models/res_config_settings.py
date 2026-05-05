# -*- coding: utf-8 -*-
import logging
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class KenseiConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kensei_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="kensei.bedrock_inference_arn",
        help="Full ARN of the AWS Bedrock application inference profile for server-side LLM calls (QC, hints).",
    )
    kensei_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="kensei.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint (e.g. ap-south-1).",
    )
    kensei_moonshot_api_key = fields.Char(
        string="Moonshot API Key",
        config_parameter="kensei.moonshot_api_key",
        help="API key for Moonshot (Kimi K2.5) used in sandbox trajectory generation.",
    )
    kensei_docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_kensei_docker_available",
    )
    kensei_deployment_mode = fields.Selection(
        [("local", "Local (Docker Compose)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="kensei.deployment_mode",
        default="local",
    )
    kensei_sandbox_dir = fields.Char(
        string="Sandbox Directory",
        config_parameter="kensei.sandbox_dir",
        help="Absolute path to the Telos Sandbox directory (contains docker/, personas/, scripts/).",
    )
    kensei_openclaw_image = fields.Char(
        string="OpenClaw Image URI",
        config_parameter="kensei.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
        help="Container image for OpenClaw in K8s mode.",
    )
    kensei_litellm_image = fields.Char(
        string="LiteLLM Image URI",
        config_parameter="kensei.litellm_image",
        default="ghcr.io/berriai/litellm:main-stable",
    )
    kensei_ws_router_host = fields.Char(
        string="WS Router Host",
        config_parameter="kensei.ws_router_host",
        help="Public hostname for the WebSocket router Ingress "
        "(e.g. kensei-ws.yourdomain.com). Browser connects to "
        "wss://<host>/sandbox/<task_id>/. Leave empty to skip Ingress creation.",
    )
    kensei_aws_bearer_token = fields.Char(
        string="AWS Bearer Token (Bedrock)",
        config_parameter="kensei.aws_bearer_token",
    )
    kensei_aws_region = fields.Char(
        string="AWS Region",
        config_parameter="kensei.aws_region",
        default="ap-south-1",
    )
    kensei_bedrock_model_arn = fields.Char(
        string="Bedrock Model ARN",
        config_parameter="kensei.bedrock_model_arn",
        help="ARN for the Bedrock model used inside OpenClaw containers.",
    )
    kensei_litellm_master_key = fields.Char(
        string="LiteLLM Master Key",
        config_parameter="kensei.litellm_master_key",
    )
    kensei_litellm_db_password = fields.Char(
        string="LiteLLM DB Password",
        config_parameter="kensei.litellm_db_password",
    )
    kensei_gog_client_secret = fields.Char(
        string="Google OAuth Client Secret (JSON)",
        config_parameter="kensei.gog_client_secret",
        help="Paste the full contents of client_secret.json from Google Cloud Console.",
    )
    kensei_gog_keyring_password = fields.Char(
        string="Gog Keyring Password",
        config_parameter="kensei.gog_keyring_password",
        help="Password used to encrypt the gog file-based keyring.",
    )
    kensei_disable_prompt_qc = fields.Boolean(
        string="Disable Prompt QC",
        config_parameter="kensei.disable_prompt_qc",
        default=False,
        help="Skip LLM-powered prompt quality checks. Useful for testing and debugging.",
    )
    kensei_disable_trajectory_qc = fields.Boolean(
        string="Disable Trajectory QC",
        config_parameter="kensei.disable_trajectory_qc",
        default=False,
        help="Skip trajectory validation and LLM-powered trajectory QC. Useful for testing and debugging.",
    )
    kensei_disable_auto_hint = fields.Boolean(
        string="Disable Auto-Hint",
        config_parameter="kensei.disable_auto_hint",
        default=False,
        help="Skip automated hint evaluation and generation. Useful for testing and debugging.",
    )
    kensei_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="kensei.s3_bucket",
    )
    kensei_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="kensei.s3_prefix",
        default="kensei",
    )
    kensei_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="kensei.s3_region",
        default="ap-south-1",
    )

    @api.depends_context("uid")
    def _compute_kensei_docker_available(self):
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                rec.kensei_docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.kensei_docker_available = False
