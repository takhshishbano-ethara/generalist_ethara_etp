# -*- coding: utf-8 -*-
import logging
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AtlasConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

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
    atlas_gog_client_secret = fields.Char(
        string="Google OAuth Client Secret (JSON)",
        config_parameter="atlas.gog_client_secret",
        help="Paste the full contents of client_secret.json from Google Cloud Console.",
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
