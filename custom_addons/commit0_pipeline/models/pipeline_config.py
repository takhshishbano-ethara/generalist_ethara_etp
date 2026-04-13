# -*- coding: utf-8 -*-
import logging
import os
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class Commit0PipelineConfig(models.TransientModel):
    _inherit = "res.config.settings"

    commit0_tools_path = fields.Char(
        string="Tools Path",
        config_parameter="commit0_pipeline.tools_path",
        help="Path to the commit0 pipeline tools directory.",
    )
    github_token = fields.Char(
        string="GitHub Token",
        config_parameter="commit0_pipeline.github_token",
        help="Personal access token for GitHub API operations.",
    )
    github_org = fields.Char(
        string="GitHub Organization",
        config_parameter="commit0_pipeline.github_org",
        default="Ethara-Ai",
        help="GitHub organization to fork repositories into.",
    )
    commit0_default_model = fields.Selection(
        selection=[
            ("opus", "Claude Opus 4.6"),
            ("kimi", "Kimi K2.5"),
            ("glm5", "GLM 5"),
            ("minimax", "MiniMax M2.5"),
            ("gpt54", "GPT 5.4"),
            ("custom", "Custom Model"),
        ],
        string="Default Model",
        config_parameter="commit0_pipeline.default_model",
        help="Default AI model preset for pipeline runs.",
    )
    commit0_default_stubbing_mode = fields.Selection(
        selection=[
            ("all", "All"),
            ("docstring", "Docstring Only"),
            ("combined", "Combined"),
        ],
        string="Default Stubbing Mode",
        default="combined",
        config_parameter="commit0_pipeline.default_stubbing_mode",
        help="Default mode for code stubbing in pipeline runs.",
    )
    max_active_tasks = fields.Integer(
        string="Max Active Tasks Per User",
        config_parameter="commit0_pipeline.max_active_tasks",
        default=1,
        help="Maximum number of concurrent active tasks a user can have.",
    )
    lambda_pdf_function_name = fields.Char(
        string="Lambda Function Name",
        config_parameter="commit0_pipeline.lambda_pdf_function_name",
        help="AWS Lambda function name or ARN for PDF scraping.",
    )
    lambda_pdf_region = fields.Char(
        string="Lambda Region",
        config_parameter="commit0_pipeline.lambda_pdf_region",
        default="ap-south-1",
    )
    lambda_pdf_access_key = fields.Char(
        string="AWS Access Key",
        config_parameter="commit0_pipeline.lambda_pdf_access_key",
    )
    lambda_pdf_secret_key = fields.Char(
        string="AWS Secret Key",
        config_parameter="commit0_pipeline.lambda_pdf_secret_key",
    )
    docker_available = fields.Boolean(
        string="Docker Available",
        compute="_compute_docker_available",
    )

    @api.depends_context("uid")
    def _compute_docker_available(self):
        """Check if Docker daemon is accessible."""
        for rec in self:
            try:
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=10,
                )
                rec.docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                rec.docker_available = False

    @api.model
    def get_default_tools_path(self):
        """Return default tools path (module's own tools/ directory)."""
        module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(module_path, "tools")
