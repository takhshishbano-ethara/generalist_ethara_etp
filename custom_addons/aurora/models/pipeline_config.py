import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


LANGUAGE_SELECTION = [
    ("python", "python"),
    ("java", "java"),
    ("javascript", "javascript"),
    ("typescript", "typescript"),
    ("cpp", "cpp"),
    ("c", "c"),
    ("csharp", "csharp"),
    ("golang", "golang"),
    ("rust", "rust"),
    ("ruby", "ruby"),
    ("php", "php"),
    ("kotlin", "kotlin"),
    ("scala", "scala"),
    ("swift", "swift"),
    ("html", "html"),
]

LANG_DETECTION_MODE = [
    ("manual", "Manual"),
    ("automatic", "Automatic"),
]

# Mapping from GitHub's Linguist language names → our harness folder names.
# GitHub API returns these as the "language" field on a repository.
GITHUB_LANG_MAP = {
    "Python": "python",
    "Java": "java",
    "JavaScript": "javascript",
    "TypeScript": "typescript",
    "C++": "cpp",
    "C": "c",
    "C#": "csharp",
    "Go": "golang",
    "Rust": "rust",
    "Ruby": "ruby",
    "PHP": "php",
    "Kotlin": "kotlin",
    "Scala": "scala",
    "Swift": "swift",
    "HTML": "html",
}


class AuroraSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # -- Output / cache paths ------------------------------------------------
    aurora_output_dir = fields.Char(
        string="Output Directory",
        config_parameter="aurora.output_dir",
        default="/tmp/aurora_output",
        help="Filesystem directory for intermediate JSONL files.",
    )
    aurora_cache_dir = fields.Char(
        string="Clone Cache Directory",
        config_parameter="aurora.cache_dir",
        default="/tmp/repo_cache",
        help="Bare-clone cache for git operations.",
    )

    # -- Pipeline tunables ---------------------------------------------------
    aurora_delay_on_error = fields.Integer(
        string="Delay on Error (s)",
        config_parameter="aurora.delay_on_error",
        default=300,
    )
    aurora_retry_attempts = fields.Integer(
        string="Retry Attempts",
        config_parameter="aurora.retry_attempts",
        default=3,
    )
    aurora_max_tags = fields.Integer(
        string="Max Tags",
        config_parameter="aurora.max_tags",
        default=200,
    )
    aurora_window_days = fields.Integer(
        string="Window Days",
        config_parameter="aurora.window_days",
        default=30,
    )

    # -- Language detection ---------------------------------------------------
    aurora_lang_detection_mode = fields.Selection(
        selection=LANG_DETECTION_MODE,
        string="Language Detection Mode",
        config_parameter="aurora.lang_detection_mode",
        default="manual",
        help="Manual: use the dropdown below. Automatic: detect via GitHub API at pipeline start.",
    )
    aurora_lang = fields.Selection(
        selection=LANGUAGE_SELECTION,
        string="Language Label",
        config_parameter="aurora.lang",
        default="python",
        help="Language tag written into each output dataset record (used in Manual mode).",
    )

    # -- Concurrency ---------------------------------------------------------
    aurora_max_active_tasks = fields.Integer(
        string="Max Active Pipeline Runs",
        config_parameter="aurora.max_active_tasks",
        default=4,
    )

    # -- Webhook -------------------------------------------------------------
    aurora_webhook_url = fields.Char(
        string="Webhook Base URL",
        config_parameter="aurora.webhook_url",
        help="Base URL where k8s worker pods POST pipeline progress. "
             "Must be reachable from K8s pods. Falls back to web.base.url if not set. "
             "Can also be set via AURORA_WEBHOOK_URL env var (takes priority).",
    )

    # -- Harness Registry Git Sync -------------------------------------------
    aurora_harness_git_repo = fields.Char(
        string="Harness Git Repo",
        config_parameter="aurora.harness_git_repo",
        default="EtharaAI/multi-swe-bench",
        help="owner/repo slug for the multi_swe_bench harness. UI uploads are committed here and worker pods sync from here.",
    )
    aurora_harness_git_branch = fields.Char(
        string="Harness Git Branch",
        config_parameter="aurora.harness_git_branch",
        default="main",
        help="Branch of the harness repo to read and write. Use a fork/dev branch for testing before promoting to main.",
    )
    aurora_github_registry_write_token = fields.Char(
        string="GitHub Registry Write Token",
        config_parameter="aurora.github_registry_write_token",
    )

    aurora_discovery_min_stars = fields.Integer(
        string="Discovery: Min Stars",
        config_parameter="aurora.discovery_min_stars",
        default=1000,
    )
    aurora_discovery_min_language_pct = fields.Float(
        string="Discovery: Min Language %",
        config_parameter="aurora.discovery_min_language_pct",
        default=60.0,
    )
    aurora_discovery_auto_promote_threshold = fields.Integer(
        string="Discovery: Auto-Promote Threshold",
        config_parameter="aurora.discovery_auto_promote_threshold",
        default=80,
    )

    @api.onchange("aurora_lang_detection_mode")
    def _onchange_lang_detection_mode(self):
        running = self.env["aurora.pipeline"].sudo().search_count([
            ("stage", "not in", ["draft", "done", "failed"]),
        ])
        if running:
            self.aurora_lang_detection_mode = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("aurora.lang_detection_mode", "manual")
            )
            return {
                "warning": {
                    "title": "Cannot Change Mode",
                    "message": (
                        f"There are {running} pipeline(s) currently running. "
                        "You can only change the language detection mode when "
                        "no pipelines are in progress."
                    ),
                }
            }
