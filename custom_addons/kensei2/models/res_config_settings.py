# -*- coding: utf-8 -*-
import logging
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class Kensei2ConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kensei2_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="kensei2.bedrock_inference_arn",
        help="Full ARN of the AWS Bedrock application inference profile for server-side LLM calls (QC, hints).",
    )
    kensei2_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="kensei2.bedrock_region",
        default="ap-south-1",
        help="AWS region for the Bedrock endpoint (e.g. ap-south-1).",
    )
    kensei2_docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_kensei2_docker_available",
    )
    kensei2_deployment_mode = fields.Selection(
        [("local", "Local (Docker Compose)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="kensei2.deployment_mode",
        default="local",
    )
    kensei2_sandbox_dir = fields.Char(
        string="Sandbox Directory",
        config_parameter="kensei2.sandbox_dir",
        help="Absolute path to the Telos Sandbox directory (contains docker/, personas/, scripts/).",
    )
    kensei2_openclaw_image = fields.Char(
        string="OpenClaw Image URI",
        config_parameter="kensei2.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
        help="Container image for OpenClaw in K8s mode.",
    )
    kensei2_litellm_image = fields.Char(
        string="LiteLLM Image URI",
        config_parameter="kensei2.litellm_image",
        default="ghcr.io/berriai/litellm:main-stable",
    )
    kensei2_ws_router_host = fields.Char(
        string="WS Router Host",
        config_parameter="kensei2.ws_router_host",
        help="Public hostname for the WebSocket router Ingress "
        "(e.g. kensei2-ws.yourdomain.com). Browser connects to "
        "wss://<host>/sandbox/<task_id>/. Leave empty to skip Ingress creation.",
    )
    kensei2_aws_bearer_token = fields.Char(
        string="AWS Bearer Token (Bedrock)",
        config_parameter="kensei2.aws_bearer_token",
    )
    kensei2_batch_size = fields.Integer(
        string="Batch Size (Pods Per Model)",
        config_parameter="kensei2.batch_size",
        default=8,
        help="Number of sandbox pods to launch per model type (e.g. 8 = 8 Claude + 8 GPT = 16 total).",
    )

    kensei2_disable_prompt_qc = fields.Boolean(
        string="Disable Prompt QC",
        config_parameter="kensei2.disable_prompt_qc",
        default=False,
        help="Skip LLM-powered prompt quality checks. Useful for testing and debugging.",
    )
    kensei2_disable_trajectory_qc = fields.Boolean(
        string="Disable Trajectory QC",
        config_parameter="kensei2.disable_trajectory_qc",
        default=False,
        help="Skip trajectory validation and LLM-powered trajectory QC. Useful for testing and debugging.",
    )
    kensei2_disable_auto_hint = fields.Boolean(
        string="Disable Auto-Hint",
        config_parameter="kensei2.disable_auto_hint",
        default=False,
        help="Skip automated hint evaluation and generation. Useful for testing and debugging.",
    )
    kensei2_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="kensei2.s3_bucket",
        default="production-grtlabs-tag",
    )
    kensei2_s3_prefix = fields.Char(
        string="S3 Key Prefix (Folder)",
        config_parameter="kensei2.s3_prefix",
        default="Kensei2",
    )
    kensei2_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="kensei2.s3_region",
        default="us-east-1",
    )
    kensei2_ecr_registry = fields.Char(
        string="ECR Registry",
        config_parameter="kensei2.ecr_registry",
        default="426628337772.dkr.ecr.ap-south-1.amazonaws.com",
        help="AWS ECR registry prefix for mock API container images (K8s mode).",
    )
    kensei2_mock_image_tag = fields.Char(
        string="Mock Image Tag",
        config_parameter="kensei2.mock_image_tag",
        default="latest",
        help="Docker image tag for mock API containers (e.g. 'latest', git SHA).",
    )
    kensei2_test_gen_enabled = fields.Boolean(
        string="Enable Test Generation",
        config_parameter="kensei2.test_gen_enabled",
        default=True,
        help="Auto-generate pytest test cases from mock API audit logs on sandbox stop.",
    )
    kensei2_test_gen_inference_arn = fields.Char(
        string="Test Gen Inference ARN",
        config_parameter="kensei2.test_gen_inference_arn",
        help="Bedrock inference ARN for test generation (Sonnet 4.6). Falls back to main ARN if empty.",
    )
    kensei2_mock_image_prefix = fields.Char(
        string="Mock Image Name Prefix",
        config_parameter="kensei2.mock_image_prefix",
        default="kensei2-mock-",
        help="Image-name prefix for mock API containers (e.g. 'kensei2-mock-'). "
             "Set to 'kensei-mock-' to reuse the existing kensei ECR repositories.",
    )

    @api.depends_context("uid")
    def _compute_kensei2_docker_available(self):
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                rec.kensei2_docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.kensei2_docker_available = False
