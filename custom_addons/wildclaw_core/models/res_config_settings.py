import subprocess

from odoo import api, fields, models


class WildclawConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wildclaw_bedrock_inference_arn = fields.Char(
        string="Bedrock Inference ARN",
        config_parameter="wildclaw.bedrock_inference_arn",
    )
    wildclaw_bedrock_region = fields.Char(
        string="Bedrock Region",
        config_parameter="wildclaw.bedrock_region",
        default="ap-south-1",
    )
    wildclaw_aws_bearer_token = fields.Char(
        string="AWS Bearer Token (Bedrock)",
        config_parameter="wildclaw.aws_bearer_token",
    )

    wildclaw_deployment_mode = fields.Selection(
        [("local", "Local (Docker Compose)"), ("k8s", "Kubernetes")],
        string="Deployment Mode",
        config_parameter="wildclaw.deployment_mode",
        default="local",
    )
    wildclaw_sandbox_dir = fields.Char(
        string="Sandbox Directory",
        config_parameter="wildclaw.sandbox_dir",
    )
    wildclaw_openclaw_image = fields.Char(
        string="OpenClaw Image URI",
        config_parameter="wildclaw.openclaw_image",
        default="ghcr.io/openclaw/openclaw:latest",
    )
    wildclaw_litellm_image = fields.Char(
        string="LiteLLM Image URI",
        config_parameter="wildclaw.litellm_image",
        default="ghcr.io/berriai/litellm:main-stable",
    )
    wildclaw_ws_router_host = fields.Char(
        string="WS Router Host",
        config_parameter="wildclaw.ws_router_host",
    )
    wildclaw_k8s_namespace = fields.Char(
        string="K8s Namespace",
        config_parameter="wildclaw.k8s_namespace",
        default="wildclaw",
    )

    wildclaw_batch_size = fields.Integer(
        string="Batch Size (Pods Per Model)",
        config_parameter="wildclaw.batch_size",
        default=8,
    )
    wildclaw_disable_prompt_qc = fields.Boolean(
        string="Disable Prompt QC",
        config_parameter="wildclaw.disable_prompt_qc",
        default=False,
    )
    wildclaw_disable_trajectory_qc = fields.Boolean(
        string="Disable Trajectory QC",
        config_parameter="wildclaw.disable_trajectory_qc",
        default=False,
    )
    wildclaw_disable_auto_hint = fields.Boolean(
        string="Disable Auto-Hint",
        config_parameter="wildclaw.disable_auto_hint",
        default=False,
    )

    wildclaw_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="wildclaw.s3_bucket",
        default="production-grtlabs-tag",
    )
    wildclaw_s3_prefix = fields.Char(
        string="S3 Key Prefix",
        config_parameter="wildclaw.s3_prefix",
        default="WildClaw",
    )
    wildclaw_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="wildclaw.s3_region",
        default="us-east-1",
    )

    wildclaw_ecr_registry = fields.Char(
        string="ECR Registry",
        config_parameter="wildclaw.ecr_registry",
    )
    wildclaw_mock_image_prefix = fields.Char(
        string="Mock Image Name Prefix",
        config_parameter="wildclaw.mock_image_prefix",
        default="wildclaw-mock-",
    )
    wildclaw_mock_image_tag = fields.Char(
        string="Mock Image Tag",
        config_parameter="wildclaw.mock_image_tag",
        default="latest",
    )

    wildclaw_media_video_frame_count = fields.Integer(
        string="Video Frame Extract Count",
        config_parameter="wildclaw.media_video_frame_count",
        default=8,
        help="Number of frames to extract per video for multimodal analysis.",
    )
    wildclaw_media_max_upload_mb = fields.Integer(
        string="Max Upload Size (MB)",
        config_parameter="wildclaw.media_max_upload_mb",
        default=50,
    )

    wildclaw_wildclawbench_enabled = fields.Boolean(
        string="Use Vendored WildClawBench Runner",
        config_parameter="wildclaw.wildclawbench_enabled",
        default=True,
        help="When enabled, sandbox execution routes through the vendored WildClawBench programmatic API.",
    )

    wildclaw_prep_dir = fields.Char(
        string="Data Prep Directory",
        config_parameter="wildclaw.prep_dir",
        default="/tmp/wildclaw_prep",
        help="Directory for yt-dlp downloads, model weights, and archive extracts.",
    )
    wildclaw_audio_transcription_provider = fields.Selection(
        [("", "Disabled"), ("openai_whisper", "OpenAI Whisper"), ("bedrock", "Bedrock (TBD)")],
        string="Audio Transcription Provider",
        config_parameter="wildclaw.audio_transcription_provider",
        default="",
    )
    wildclaw_openai_api_key = fields.Char(
        string="OpenAI API Key",
        config_parameter="wildclaw.openai_api_key",
    )
    wildclaw_sam3_weights_path = fields.Char(
        string="SAM3 Weights Path",
        config_parameter="wildclaw.sam3_weights_path",
        help="Filesystem path to sam3.pt. Auto-downloaded via prep_runner.download_hf_hub if not present.",
    )
    wildclaw_hf_token = fields.Char(
        string="HuggingFace Hub Token",
        config_parameter="wildclaw.hf_token",
    )

    wildclaw_docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_wildclaw_docker_available",
    )

    @api.depends_context("uid")
    def _compute_wildclaw_docker_available(self):
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                rec.wildclaw_docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.wildclaw_docker_available = False
