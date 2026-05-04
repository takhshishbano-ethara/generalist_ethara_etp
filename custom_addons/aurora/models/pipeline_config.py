import logging

from odoo import api, fields, models

from .credential_manager import (
    encrypt_value,
    decrypt_value,
)

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
        default="/data/repo_cache",
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

    # -- S3 Storage (encrypted at rest via Fernet) ---------------------------
    aurora_s3_bucket = fields.Char(
        string="S3 Bucket",
        config_parameter="aurora.s3_bucket",
        help="AWS S3 bucket name for storing pipeline outputs.",
    )
    aurora_s3_access_key = fields.Char(
        string="S3 Access Key",
    )
    aurora_s3_secret_key = fields.Char(
        string="S3 Secret Key",
    )
    aurora_s3_region = fields.Char(
        string="S3 Region",
        config_parameter="aurora.s3_region",
        default="ap-south-1",
    )
    aurora_s3_folder = fields.Char(
        string="S3 Folder",
        config_parameter="aurora.s3_folder",
        help="Optional folder prefix inside the bucket. e.g. 'production/pipelines'. "
             "Final path: {bucket}/{folder}/aurora_phase1/{org}__{repo}/run_N/",
    )

    # -- Concurrency ---------------------------------------------------------
    aurora_max_active_tasks = fields.Integer(
        string="Max Active Pipeline Runs",
        config_parameter="aurora.max_active_tasks",
        default=50,
    )

    # -- Kubernetes ----------------------------------------------------------
    aurora_k8s_namespace = fields.Char(
        string="K8s Namespace",
        config_parameter="aurora.k8s_namespace",
        default="aurora",
    )
    aurora_k8s_image = fields.Char(
        string="K8s Docker Image",
        config_parameter="aurora.k8s_image",
        help="Docker image for pipeline worker pods (same Odoo image, different entrypoint).",
    )
    aurora_k8s_service_account = fields.Char(
        string="K8s Service Account",
        config_parameter="aurora.k8s_service_account",
        default="aurora-worker",
    )
    aurora_k8s_node_pool = fields.Char(
        string="K8s Node Pool",
        config_parameter="aurora.k8s_node_pool",
        default="",
        help="Node pool label for pod scheduling. Leave empty for local/minikube. Set to 'general-purpose' for EKS.",
    )
    aurora_k8s_kueue_queue = fields.Char(
        string="Kueue Queue Name",
        config_parameter="aurora.k8s_kueue_queue",
        default="aurora-pipelines",
    )
    aurora_k8s_efs_pvc = fields.Char(
        string="EFS PVC Name",
        config_parameter="aurora.k8s_efs_pvc",
        default="aurora-repo-cache",
        help="PersistentVolumeClaim for shared git repo cache (ReadWriteMany).",
    )
    aurora_k8s_cpu_request = fields.Char(
        string="CPU Request",
        config_parameter="aurora.k8s_cpu_request",
        default="1",
    )
    aurora_k8s_memory_request = fields.Char(
        string="Memory Request",
        config_parameter="aurora.k8s_memory_request",
        default="2Gi",
    )
    aurora_k8s_memory_limit = fields.Char(
        string="Memory Limit",
        config_parameter="aurora.k8s_memory_limit",
        default="4Gi",
    )
    aurora_k8s_deadline_seconds = fields.Integer(
        string="Job Deadline (seconds)",
        config_parameter="aurora.k8s_deadline_seconds",
        default=14400,
        help="Hard kill for pipeline pods after this many seconds (default: 4 hours).",
    )
    aurora_k8s_worker_script = fields.Char(
        string="Worker Script Path",
        config_parameter="aurora.k8s_worker_script",
        default="/opt/ethara/app/custom_addons/aurora/worker/run_pipeline.py",
        help="Path to run_pipeline.py inside the container.",
    )
    aurora_k8s_odoo_conf = fields.Char(
        string="Odoo Config Path (in container)",
        config_parameter="aurora.k8s_odoo_conf",
        default="/etc/odoo/odoo.conf",
        help="Path to odoo.conf inside the worker pod.",
    )
    aurora_k8s_configmap = fields.Char(
        string="Worker ConfigMap Name",
        config_parameter="aurora.k8s_configmap",
        default="",
        help="Optional ConfigMap for extra config. Leave empty if odoo.conf is provided via the Secret.",
    )
    aurora_k8s_secret = fields.Char(
        string="Worker Secret Name",
        config_parameter="aurora.k8s_secret",
        default="aurora-odoo-config",
        help="K8s Secret containing odoo.conf (DB credentials, addons path, etc.). Mounted at /etc/odoo/.",
    )

    # -- Webhook (worker -> Odoo progress notifications) ---------------------
    aurora_webhook_secret = fields.Char(
        string="Webhook Secret",
    )
    aurora_webhook_url = fields.Char(
        string="Webhook URL",
        config_parameter="aurora.webhook_url",
        help="Base URL where k8s worker pods POST pipeline progress. Typically the public Odoo URL (e.g. https://odoo.your-domain.com). Leave empty to disable webhook notifications from k8s workers.",
    )

    # -- Harness Registry Git Sync (Option B: UI uploads pushed to GitHub) ---
    aurora_github_registry_write_token = fields.Char(
        string="GitHub Registry Write Token",
    )
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

    _ENCRYPTED_FIELD_MAP = {
        "aurora_s3_access_key": "aurora.s3_access_key",
        "aurora_s3_secret_key": "aurora.s3_secret_key",
        "aurora_webhook_secret": "aurora.webhook_secret",
        "aurora_github_registry_write_token": "aurora.github_registry_write_token",
    }

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        for field_name, param_key in self._ENCRYPTED_FIELD_MAP.items():
            stored = ICP.get_param(param_key, "")
            if not stored:
                res[field_name] = ""
            elif stored.startswith("fernet:1:"):
                res[field_name] = decrypt_value(ICP, stored)
            else:
                res[field_name] = stored
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        for field_name, param_key in self._ENCRYPTED_FIELD_MAP.items():
            plaintext = self[field_name] or ""
            try:
                ICP.set_param(param_key, encrypt_value(ICP, plaintext))
            except Exception:
                _logger.warning(
                    "Aurora: encryption failed for %s, storing plaintext.", param_key
                )
                ICP.set_param(param_key, plaintext)
