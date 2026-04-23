# -*- coding: utf-8 -*-
import logging
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AtlasConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    atlas_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="atlas.bedrock_inference_arn",
        help="Full ARN of the AWS Bedrock application inference profile for Kimi K2.5.",
    )
    atlas_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="atlas.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint (e.g. ap-south-1).",
    )
    atlas_docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_atlas_docker_available",
    )
    atlas_deployment_mode = fields.Selection(
        [("local", "Local (Docker Compose)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="atlas.deployment_mode",
        default="local",
    )
    atlas_sandbox_dir = fields.Char(
        string="Sandbox Directory",
        config_parameter="atlas.sandbox_dir",
        help="Absolute path to the Sandbox directory (contains docker/, personas/, scripts/).",
    )
    atlas_openclaw_image = fields.Char(
        string="OpenClaw Image URI",
        config_parameter="atlas.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
        help="Container image for OpenClaw in K8s mode.",
    )
    atlas_litellm_image = fields.Char(
        string="LiteLLM Image URI",
        config_parameter="atlas.litellm_image",
        default="ghcr.io/berriai/litellm:main-stable",
    )
    atlas_ws_router_host = fields.Char(
        string="WS Router Host",
        config_parameter="atlas.ws_router_host",
        help="Public hostname for the WebSocket router Ingress "
        "(e.g. atlas-ws.yourdomain.com). Browser connects to "
        "wss://<host>/sandbox/<task_id>/. Leave empty to skip Ingress creation.",
    )
    atlas_aws_bearer_token = fields.Char(
        string="AWS Bearer Token (Bedrock)",
        config_parameter="atlas.aws_bearer_token",
    )
    atlas_aws_region = fields.Char(
        string="AWS Region",
        config_parameter="atlas.aws_region",
        default="ap-south-1",
    )
    atlas_bedrock_model_arn = fields.Char(
        string="Bedrock Model ARN",
        config_parameter="atlas.bedrock_model_arn",
        help="ARN for the Bedrock model used inside OpenClaw containers.",
    )
    atlas_litellm_master_key = fields.Char(
        string="LiteLLM Master Key",
        config_parameter="atlas.litellm_master_key",
    )
    atlas_litellm_db_password = fields.Char(
        string="LiteLLM DB Password",
        config_parameter="atlas.litellm_db_password",
    )
    atlas_gog_client_secret = fields.Char(
        string="Google OAuth Client Secret (JSON)",
        config_parameter="atlas.gog_client_secret",
        help="Paste the full contents of client_secret.json from Google Cloud Console.",
    )
    atlas_gog_keyring_password = fields.Char(
        string="Gog Keyring Password",
        config_parameter="atlas.gog_keyring_password",
        help="Password used to encrypt the gog file-based keyring.",
    )

    @api.depends_context("uid")
    def _compute_atlas_docker_available(self):
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                rec.atlas_docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.atlas_docker_available = False
