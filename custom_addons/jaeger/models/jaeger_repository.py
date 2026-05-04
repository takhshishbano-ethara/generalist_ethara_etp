import logging
import os
import re
import threading
from datetime import datetime

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_SAFE_GITHUB_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")

_CRON_LOCK_WATCHDOG_SCRAPES = 83927461
_CRON_LOCK_WATCHDOG_BUILDS = 83927462
_CRON_LOCK_RECONCILE_SCRAPES = 83927463
_CRON_LOCK_AUTO_ADVANCE = 83927467
_CRON_LOCK_POLL_EKS = 83927468
_CRON_LOCK_WATCHDOG_TESTS = 83927469
_CRON_LOCK_WATCHDOG_DATASET = 83927470

STAGE_SELECTION = [
    ("stage1", "1 - Repo Validation"),
    ("stage2", "2 - PR Collection"),
    ("stage3", "3 - Docker Build"),
    ("stage4", "4 - Test Execution"),
    ("stage5", "5 - Dataset Finalization"),
    ("stage6", "6 - Trajectory Generation"),
    ("stage7", "7 - Delivery & Export"),
    ("done", "Complete"),
    ("failed", "Failed"),
]

TERMINAL_STATE_SELECTION = [
    ("none", "None"),
    ("repo_not_suitable", "Repository Not Suitable"),
    ("no_valid_prs", "No Valid PRs Found"),
    ("build_failed", "Docker Build Failed"),
    ("no_valid_instances", "No Valid Instances"),
    ("complete", "Complete"),
]

STATUS_SELECTION = [
    ("pending", "Pending"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]

LANGUAGE_SELECTION = [
    ("python", "Python"),
    ("java", "Java"),
    ("typescript", "TypeScript"),
    ("javascript", "JavaScript"),
    ("go", "Go"),
    ("rust", "Rust"),
    ("c", "C"),
    ("cpp", "C++"),
]

PIPELINE_MODE_SELECTION = [
    ("swe", "SWE (Single-PR Tasks)"),
    ("rct", "RCT (Real Coder — Bounty PRs)"),
    ("lht", "LHT (Long-Horizon Tasks)"),
]

TRAJECTORY_STATUS_SELECTION = [
    ("pending", "Pending"),
    ("dispatched", "Dispatched"),
    ("running", "Running"),
    ("evaluating", "Evaluating"),
    ("summarizing", "Summarizing"),
    ("done", "Done"),
    ("failed", "Failed"),
]

TASK_CATEGORY_SELECTION = [
    ("swe", "SWE"),
    ("long_horizon", "Long Horizon"),
    ("real_coder", "Real Coder"),
]


MAX_CONCURRENT_SCRAPES = 500  # Cluster-wide cap (kaiju_build uses 1500 for builds)

LANGUAGE_BASE_IMAGES = {
    "python": "python:3.11-slim",
    "javascript": "node:20-slim",
    "typescript": "node:20-slim",
    "java": "eclipse-temurin:17-jdk",
    "go": "golang:1.22",
    "rust": "rust:1.85",
    "c": "ubuntu:22.04",
    "cpp": "ubuntu:22.04",
}


class JaegerRepository(models.Model):
    _name = "jaeger.repository"
    _description = "Jaeger Repository"
    _inherit = ["mail.thread"]
    _order = "create_date desc"
    _repo_url_unique = models.Constraint(
        "unique(repo_url)",
        "Repository URL must be unique.",
    )

    # ── Identity ─────────────────────────────────────────────────────────
    name = fields.Char(
        string="Reference",
        readonly=True,
        copy=False,
        default="New",
    )
    repo_url = fields.Char(string="GitHub URL", required=True, tracking=True)
    org = fields.Char(string="Organization", compute="_compute_org_repo", store=True)
    repo_name = fields.Char(
        string="Repository Name", compute="_compute_org_repo", store=True,
    )
    language = fields.Selection(
        LANGUAGE_SELECTION, string="Language", default="python", required=True,
    )
    pipeline_mode = fields.Selection(
        PIPELINE_MODE_SELECTION,
        string="Pipeline Mode",
        default="swe",
        required=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.uid,
        tracking=True,
    )
    is_admin = fields.Boolean(compute="_compute_is_admin")

    # ── Stage Tracking ───────────────────────────────────────────────────
    current_stage = fields.Selection(
        STAGE_SELECTION,
        string="Current Stage",
        default="stage1",
        tracking=True,
        index=True,
    )
    terminal_state = fields.Selection(
        TERMINAL_STATE_SELECTION,
        string="Terminal State",
        default="none",
    )

    # ── Stage 1: Repo Discovery & Validation ─────────────────────────────
    stars = fields.Integer(string="Stars")
    forks = fields.Integer(string="Forks")
    is_fork = fields.Boolean(string="Is Fork")
    repo_description = fields.Text(string="Description")
    crawl_status = fields.Selection(
        STATUS_SELECTION, string="Crawl Status", default="pending",
    )

    # ── Stage 2: PR Collection & Filtering ────────────────────────────────
    pr_collection_status = fields.Selection(
        STATUS_SELECTION,
        string="PR Collection Status",
        default="pending",
        index=True,
    )
    pr_collection_progress = fields.Float(string="PR Collection Progress")
    pr_collection_step = fields.Char(string="Current Step", readonly=True)
    pr_collection_log = fields.Text(string="Collection Log", readonly=True)
    s3_folder_url = fields.Char(
        string="S3 Folder",
        compute="_compute_s3_folder_url",
    )
    total_prs_fetched = fields.Integer(string="Total PRs Fetched")
    filtered_prs_count = fields.Integer(string="Filtered PRs")
    issues_fetched_count = fields.Integer(string="Issues Fetched")
    raw_dataset_count = fields.Integer(string="Raw Dataset Count")
    raw_dataset_jsonl_path = fields.Char(string="Raw Dataset JSONL Path")

    # ── Stage 2 (LHT-specific) ───────────────────────────────────────────
    tags_jsonl_path = fields.Char(string="Tags JSONL Path")
    tag_groups_jsonl_path = fields.Char(string="Tag Groups JSONL Path")
    max_tags = fields.Integer(string="Max Tags", default=200)
    window_days = fields.Integer(string="Window Days", default=30)
    tags_fetched_count = fields.Integer(string="Tags Fetched")
    tag_groups_count = fields.Integer(string="Tag Groups")

    # ── Stage 3: Docker Image Building ────────────────────────────────────
    docker_build_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("queued", "Queued"),
            ("building", "Building"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Docker Build Status",
        default="pending",
        index=True,
    )
    docker_build_progress = fields.Float(string="Docker Build Progress")
    docker_image_prefix = fields.Char(string="Image Prefix")
    ecr_url = fields.Char(string="ECR URL")
    ecr_prefix = fields.Char(string="ECR Prefix")
    # kaiju_build_id = fields.Many2one("kaiju.build", string="Kaiju Build")  # Phase 2-7
    images_built_count = fields.Integer(string="Images Built")
    images_failed_count = fields.Integer(string="Images Failed")
    force_build = fields.Boolean(string="Force Build")
    docker_platform = fields.Char(
        string="Docker Platform",
        help="e.g. linux/amd64. Leave empty for native platform (recommended for local testing).",
    )
    base_image_name = fields.Char(
        string="Base Image Name",
        readonly=True,
        help="Auto-built repo base image tag. Empty = will auto-build on first Docker build.",
    )
    base_image_status = fields.Selection(
        [
            ("none", "Not Built"),
            ("building", "Building"),
            ("built", "Built"),
            ("failed", "Failed"),
        ],
        string="Base Image Status",
        default="none",
    )

    # ── Stage 4: Test Execution ───────────────────────────────────────────
    test_execution_status = fields.Selection(
        STATUS_SELECTION,
        string="Test Execution Status",
        default="pending",
        index=True,
    )
    test_execution_progress = fields.Float(string="Test Execution Progress")
    instances_tested_count = fields.Integer(string="Instances Tested")
    instances_valid_count = fields.Integer(string="Instances Valid")
    instances_invalid_count = fields.Integer(string="Instances Invalid")
    instances_error_count = fields.Integer(string="Instances Error")
    human_mode = fields.Boolean(string="Human Mode (Sequential)", default=True)
    agent_timeout = fields.Integer(string="Agent Timeout (s)", default=1800)

    # ── Test Config (human-in-the-loop overrides) ────────────────────────
    test_config_json = fields.Text(
        string="Test Configuration (JSON)",
        help="Optional JSON overrides for auto-detected settings. "
             "Keys: base_image, system_deps, install_cmd, test_cmd, "
             "prepare_cmd, parser, memory_limit, network, env",
    )
    test_config_effective = fields.Text(
        string="Effective Config",
        compute="_compute_test_config_effective",
    )

    # ── Stage 5: Dataset Finalization ─────────────────────────────────────
    dataset_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Dataset Status",
        default="pending",
    )
    final_dataset_jsonl_path = fields.Char(string="Final Dataset JSONL Path")
    final_dataset_count = fields.Integer(string="Final Dataset Count")
    final_report_json = fields.Text(string="Final Report JSON")
    total_instances = fields.Integer(string="Total Instances")
    resolved_instances = fields.Integer(string="Valid Instances")
    unresolved_instances = fields.Integer(string="Invalid Instances")
    empty_patch_instances = fields.Integer(string="Empty Patch Instances")
    error_instances = fields.Integer(string="Error Instances")

    # ── Stage 6: Trajectory Generation ────────────────────────────────────
    trajectory_status = fields.Selection(
        TRAJECTORY_STATUS_SELECTION,
        string="Trajectory Status",
        default="pending",
    )
    eks_job_id = fields.Char(string="EKS Job ID", index=True)
    model_name = fields.Char(string="LLM Model", default="claude")
    model_canonical_name = fields.Char(string="Model Canonical Name")
    k_runs = fields.Integer(string="K Runs (pass@k)", default=8)
    num_workers = fields.Integer(string="Inference Workers", default=1)
    max_iterations = fields.Integer(string="Max Iterations", default=300)
    max_retries = fields.Integer(string="Max Retries", default=3)
    conversation_timeout = fields.Integer(
        string="Conversation Timeout (s)", default=3600,
    )
    temperature = fields.Float(string="Temperature", default=1.0)
    llm_config_json = fields.Text(string="LLM Config JSON")
    pass_at_k = fields.Float(string="pass@k")
    pass_at_k_summary_json = fields.Text(string="pass@k Summary JSON")
    total_api_cost = fields.Float(string="Total API Cost (USD)")
    total_api_calls = fields.Integer(string="Total API Calls")
    total_prompt_tokens = fields.Integer(string="Total Prompt Tokens")
    total_completion_tokens = fields.Integer(string="Total Completion Tokens")

    # ── Stage 7: Delivery & Export ────────────────────────────────────────
    delivery_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("converting", "Converting"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Delivery Status",
        default="pending",
    )
    meta_delivery_jsonl_path = fields.Char(string="Meta Delivery JSONL Path")
    delivered_count = fields.Integer(string="Delivered Count")
    task_category = fields.Selection(
        TASK_CATEGORY_SELECTION, string="Task Category",
    )

    # ── Logging ───────────────────────────────────────────────────────────
    log_output = fields.Text(string="Log Output")
    error_message = fields.Text(string="Error Message")
    cancel_requested = fields.Boolean(default=False)
    last_heartbeat = fields.Datetime(string="Last Heartbeat", readonly=True)
    scrape_queued_at = fields.Datetime(string="Scrape Queued At", readonly=True)

    # ── Relations ─────────────────────────────────────────────────────────
    instance_ids = fields.One2many(
        "jaeger.instance", "repository_id", string="Instances",
    )
    run_ids = fields.One2many(
        "jaeger.trajectory.run", "repository_id", string="Trajectory Runs",
    )

    # ── JSONL Preview Fields ─────────────────────────────────────────────
    raw_dataset_preview = fields.Text(
        string="Raw Dataset Preview", compute="_compute_jsonl_previews",
    )
    final_dataset_preview = fields.Text(
        string="Final Dataset Preview", compute="_compute_jsonl_previews",
    )

    # ── Computed Fields ───────────────────────────────────────────────────

    @api.depends("repo_url")
    def _compute_org_repo(self):
        for rec in self:
            if rec.repo_url:
                parts = rec.repo_url.strip().rstrip("/").removesuffix(".git").split("/")
                if len(parts) >= 2:
                    rec.org = parts[-2]
                    rec.repo_name = parts[-1]
                else:
                    rec.org = ""
                    rec.repo_name = ""
            else:
                rec.org = ""
                rec.repo_name = ""

    @api.constrains("repo_url")
    def _check_repo_url(self):
        for rec in self:
            url = (rec.repo_url or "").strip().rstrip("/").removesuffix(".git")
            if not url.startswith("https://github.com/"):
                raise ValidationError(
                    "Only GitHub URLs are supported (https://github.com/org/repo).",
                )
            parts = url.replace("https://github.com/", "").split("/")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValidationError(
                    "URL must be in format: https://github.com/org/repo",
                )
            if not _SAFE_GITHUB_NAME.match(parts[0]):
                raise ValidationError(
                    f"Invalid GitHub org name: {parts[0]!r}. "
                    "Only alphanumeric, dots, hyphens, and underscores allowed.",
                )
            if not _SAFE_GITHUB_NAME.match(parts[1]):
                raise ValidationError(
                    f"Invalid GitHub repo name: {parts[1]!r}. "
                    "Only alphanumeric, dots, hyphens, and underscores allowed.",
                )

    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("jaeger.group_jaeger_admin")
        for rec in self:
            rec.is_admin = is_admin

    @api.depends("pipeline_mode")
    def _compute_s3_folder_url(self):
        bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        for rec in self:
            if not bucket:
                rec.s3_folder_url = ""
                continue
            mode = rec.pipeline_mode or "swe"
            folder = f"{prefix}/{mode}/{rec.id}/"
            rec.s3_folder_url = (
                f"https://s3.console.aws.amazon.com/s3/buckets/{bucket}"
                f"?region={region}&prefix={folder}"
            )

    @api.depends("raw_dataset_count", "final_dataset_jsonl_path")
    def _compute_jsonl_previews(self):
        for rec in self:
            if rec.raw_dataset_count:
                filename = f"{rec.org}__{rec.repo_name}_raw_dataset.jsonl"
                raw_lines = rec._fetch_lines_from_s3_by_name(filename)
                rec.raw_dataset_preview = rec._format_preview_lines(raw_lines) if raw_lines else ""
            else:
                rec.raw_dataset_preview = ""
            rec.final_dataset_preview = rec._read_jsonl_preview(
                rec.final_dataset_jsonl_path,
            )

    def _read_jsonl_preview(self, file_path, max_lines=20):
        """Read the first N lines of a JSONL file for UI preview.

        If the local file doesn't exist, attempt to stream the first
        ``max_lines`` from S3 using the configured bucket/prefix.
        """
        import json as json_mod
        from pathlib import Path

        if not file_path:
            return ""
        p = Path(file_path)

        raw_lines = None

        # Try local file first
        if p.exists():
            with open(p, encoding="utf-8") as f:
                raw_lines = f.readlines()
        else:
            # Fallback: fetch from S3
            raw_lines = self._fetch_lines_from_s3(file_path)

        if raw_lines is None:
            return f"File not available locally or on S3: {file_path}"

        lines = []
        total_lines = len(raw_lines)
        for i, line in enumerate(raw_lines):
            if i >= max_lines:
                break
            try:
                obj = json_mod.loads(line)
                lines.append(json_mod.dumps(obj, indent=2, ensure_ascii=False))
            except json_mod.JSONDecodeError:
                lines.append(line.rstrip())
        if total_lines > max_lines:
            lines.append(f"\n... ({total_lines - max_lines} more lines)")
        return "\n".join(lines)

    def _fetch_lines_from_s3(self, file_path):
        """Try to read a JSONL file from S3 given its local path.

        Extracts the filename from *file_path*, builds the S3 key using
        the repo id, and streams the object content.  Returns a list of
        lines on success, or ``None`` on failure.
        """
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            return None

        s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        s3_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        if not s3_bucket:
            return None

        mode = self.pipeline_mode or "swe"
        filename = os.path.basename(file_path)
        s3_key = f"{s3_prefix}/{mode}/{self.id}/{filename}"
        try:
            config_kwargs = {"connect_timeout": 10, "read_timeout": 30}
            if os.environ.get("JAEGER_S3_ENDPOINT"):
                config_kwargs["s3"] = {"addressing_style": "path"}
            client = boto3.client(
                "s3",
                region_name=s3_region,
                endpoint_url=os.environ.get(
                    "JAEGER_S3_ENDPOINT",
                    f"https://s3.{s3_region}.amazonaws.com",
                ),
                config=BotoConfig(**config_kwargs),
            )
            resp = client.get_object(Bucket=s3_bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8", errors="replace")
            return body.splitlines(keepends=True)
        except Exception:
            _logger.debug("S3 preview fallback failed for %s", s3_key, exc_info=True)
            return None

    def _fetch_lines_from_s3_by_name(self, filename):
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            return None
        s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        s3_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        if not s3_bucket:
            return None
        mode = self.pipeline_mode or "swe"
        s3_key = f"{s3_prefix}/{mode}/{self.id}/{filename}"
        try:
            config_kwargs = {"connect_timeout": 10, "read_timeout": 30}
            if os.environ.get("JAEGER_S3_ENDPOINT"):
                config_kwargs["s3"] = {"addressing_style": "path"}
            client = boto3.client(
                "s3",
                region_name=s3_region,
                endpoint_url=os.environ.get("JAEGER_S3_ENDPOINT", f"https://s3.{s3_region}.amazonaws.com"),
                config=BotoConfig(**config_kwargs),
            )
            resp = client.get_object(Bucket=s3_bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8", errors="replace")
            return body.splitlines(keepends=True)
        except Exception:
            _logger.debug("S3 fetch by name failed for %s", s3_key, exc_info=True)
            return None

    def _format_preview_lines(self, raw_lines, max_lines=20):
        import json as json_mod
        lines = []
        total_lines = len(raw_lines)
        for i, line in enumerate(raw_lines):
            if i >= max_lines:
                break
            try:
                obj = json_mod.loads(line)
                lines.append(json_mod.dumps(obj, indent=2, ensure_ascii=False))
            except json_mod.JSONDecodeError:
                lines.append(line.rstrip())
        if total_lines > max_lines:
            lines.append(f"\n... ({total_lines - max_lines} more lines)")
        return "\n".join(lines)

    # ── Sequence ──────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "jaeger.repository",
                ) or "New"
        return super().create(vals_list)

    def write(self, vals):
        if "test_config_json" in vals:
            for rec in self:
                if rec.base_image_status == "built":
                    vals.setdefault("base_image_status", "none")
                    vals.setdefault("base_image_name", False)
                    _logger.info(
                        "Auto-reset base image for %s (test_config_json changed)",
                        rec.name,
                    )
        res = super().write(vals)
        step_text = vals.get("pr_collection_step")
        if step_text:
            for rec in self:
                rec._append_collection_log(step_text)
        return res

    # ── Helpers ───────────────────────────────────────────────────────────

    def _append_log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        self.env.cr.execute(
            "UPDATE jaeger_repository SET log_output = "
            "CASE WHEN LENGTH(COALESCE(log_output, '')) > 200000 "
            "THEN RIGHT(log_output, 150000) || %s "
            "ELSE COALESCE(log_output, '') || %s END "
            "WHERE id = %s",
            [line, line, self.id],
        )
        self.invalidate_recordset(["log_output"])

    def _append_collection_log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        self.env.cr.execute(
            "UPDATE jaeger_repository SET pr_collection_log = "
            "CASE WHEN LENGTH(COALESCE(pr_collection_log, '')) > 50000 "
            "THEN RIGHT(pr_collection_log, 40000) || %s "
            "ELSE COALESCE(pr_collection_log, '') || %s END "
            "WHERE id = %s",
            [line, line, self.id],
        )
        self.invalidate_recordset(["pr_collection_log"])

    def _count_jsonl_lines(self, path):
        try:
            with open(path) as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0

    def _run_pipeline_async(self, method_name, status_field, label):
        from odoo.orm.registry import Registry

        repo_id = self.id
        dbname = self.env.cr.dbname

        def _bg_run():
            try:
                registry = Registry(dbname)
                with registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    repo = env["jaeger.repository"].browse(repo_id)
                    try:
                        getattr(repo, method_name)()
                    except Exception as e:
                        _logger.exception(
                            "Jaeger background %s failed for repo %s",
                            label, repo_id,
                        )
                        try:
                            cr.rollback()
                            repo.write({
                                status_field: "failed",
                                "error_message": str(e)[:2000],
                            })
                            cr.commit()
                        except Exception:
                            _logger.exception("Failed to write error status")
            except Exception:
                _logger.exception(
                    "Jaeger background %s failed for repo %s", label, repo_id,
                )

        thread = threading.Thread(
            target=_bg_run, daemon=True,
            name=f"jaeger_{method_name}_{repo_id}",
        )
        thread.start()

    # ── Downloads ──────────────────────────────────────────────────────────

    def action_download_raw_dataset(self):
        self.ensure_one()
        if not self.raw_dataset_count:
            raise UserError("No raw dataset available for download.")
        return {
            "type": "ir.actions.act_url",
            "url": f"/jaeger/download/{self.id}/raw_dataset",
            "target": "new",
        }

    # ── Stage Advancement ─────────────────────────────────────────────────

    def action_advance_stage(self):
        self.ensure_one()
        if self.current_stage in ("done", "failed"):
            raise UserError("Repository is already in a terminal state.")
        from psycopg2 import OperationalError as Psycopg2OpError
        try:
            self.env.cr.execute(
                "SELECT current_stage FROM jaeger_repository"
                " WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Psycopg2OpError:
            self.env.cr.rollback()
            raise UserError("Stage advancement is already in progress by another user.")
        gate_ok, msg = self._check_current_gate()
        if not gate_ok:
            raise UserError(f"Cannot advance: {msg}")
        next_stage = self._next_stage()
        if next_stage:
            self.write({"current_stage": next_stage})

    def _check_current_gate(self):
        stage = self.current_stage
        if self.terminal_state != "none":
            return False, f"Terminal state: {self.terminal_state}"

        if stage == "stage1":
            if self.crawl_status != "done":
                return False, "Repository validation not complete"
        elif stage == "stage2":
            if self.pr_collection_status != "done":
                return False, "PR collection not complete"
            if not self.instance_ids:
                return False, "No instances created"
        elif stage == "stage3":
            if self.docker_build_status != "done":
                return False, "Docker build not complete"
            if self.images_built_count == 0:
                return False, "No images built"
        elif stage == "stage4":
            if self.test_execution_status != "done":
                return False, "Test execution not complete"
            if self.instances_valid_count == 0:
                return False, "No valid instances after test execution"
        elif stage == "stage5":
            if self.dataset_status != "done":
                return False, "Dataset finalization not complete"
            if self.final_dataset_count == 0:
                return False, "No valid instances in final dataset"
        elif stage == "stage6":
            if self.trajectory_status != "done":
                return False, "Trajectory generation not complete"
        elif stage == "stage7":
            if self.delivery_status != "done":
                return False, "Delivery export not complete"

        return True, ""

    def _next_stage(self):
        mapping = {
            "stage1": "stage2",
            "stage2": "stage3",
            "stage3": "stage4",
            "stage4": "stage5",
            "stage5": "stage6",
            "stage6": "stage7",
            "stage7": "done",
        }
        return mapping.get(self.current_stage)

