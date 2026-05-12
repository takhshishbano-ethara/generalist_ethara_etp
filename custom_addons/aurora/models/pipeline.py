import logging
import os
import re
import threading
import time
import uuid

from github import Auth, Github, GithubException

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config as odoo_config

from .pipeline_config import GITHUB_LANG_MAP, LANGUAGE_SELECTION

_logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException as K8sApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


_k8s_config_lock = threading.Lock()
_k8s_config_loaded = False
_k8s_config_loaded_at = 0.0
_K8S_CONFIG_MAX_AGE = 3000

# Track locally-spawned pipeline threads for lifecycle management.
_local_threads: dict[int, threading.Thread] = {}
_local_threads_lock = threading.Lock()

# Maximum file size (bytes) allowed for in-memory download via ir.attachment.
_MAX_DOWNLOAD_FILE_SIZE = 200 * 1024 * 1024  # 200 MB

# ---------------------------------------------------------------------------
# DevOps-managed infrastructure constants (Talos pattern).
# These values are managed by the DevOps team at the cluster level.
# Do NOT expose them as UI-configurable settings.
# ---------------------------------------------------------------------------
NAMESPACE = "aurora"

NODE_SELECTOR = (
    {} if os.environ.get("AURORA_LOCAL_MODE") else {"ethara.ai/node-pool": "general-purpose"}
)

SERVICE_ACCOUNT = "aurora-worker"

S3_BUCKET = "production-grtlabs-tag"
S3_REGION = "us-east-1"
S3_AURORA_PREFIX = "aurora"

KUEUE_QUEUE = "" if os.environ.get("AURORA_LOCAL_MODE") else "aurora-pipelines"

DOCKER_IMAGE = "426628337772.dkr.ecr.ap-south-1.amazonaws.com/aurora-worker:latest"

IMAGE_PULL_POLICY = "IfNotPresent" if os.environ.get("AURORA_LOCAL_MODE") else "Always"

CPU_REQUEST = "1"
MEMORY_REQUEST = "2Gi"
MEMORY_LIMIT = "4Gi"

DEADLINE_SECONDS = 14400  # 4 hours

WORKER_SCRIPT = "/opt/odoo/custom_addons/aurora/worker/run_pipeline.py"
ODOO_CONF_PATH = "/etc/odoo/odoo.conf"


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _load_k8s_config():
    global _k8s_config_loaded, _k8s_config_loaded_at
    if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
        return
    with _k8s_config_lock:
        if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        _k8s_config_loaded = True
        _k8s_config_loaded_at = time.time()

_SAFE_GITHUB_NAME = re.compile(r'^[a-zA-Z0-9._-]+$')


def _validate_file_path(file_url: str, allowed_base: str) -> str:
    """Resolve *file_url* and ensure it lives under *allowed_base*.

    Prevents path-traversal attacks where a tampered DB field like
    ``/etc/shadow`` could be served to the user.

    Returns the resolved absolute path on success; raises UserError otherwise.
    """
    real_path = os.path.realpath(file_url)
    real_base = os.path.realpath(allowed_base)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise UserError("File path is outside the allowed output directory.")
    return real_path

STEP_SELECTION = [
    ("draft", "Draft"),
    # Phase 1 — Data Collection (sub-steps kept for internal tracking)
    ("fetch_prs", "1 – Fetch PRs"),
    ("filter_prs", "2 – Filter PRs"),
    ("discover_tags", "3 – Discover Tags"),
    ("group_prs", "4 – Group PRs by Tags"),
    ("fetch_issues", "5 – Fetch Issues"),
    ("build_dataset", "6 – Build Dataset"),
    # Phase 2 — Docker Build & Test Execution
    ("phase2_build", "Phase 2 – Docker Build"),
    ("phase2_test", "Phase 2 – Test Execution"),
    ("phase2_report", "Phase 2 – Report Generation"),
    # Phase 3 — Trajectory Generation
    ("phase3_infer", "Phase 3 – Inference"),
    ("phase3_eval", "Phase 3 – Evaluation"),
    ("phase3_summary", "Phase 3 – Summary"),
    ("done", "Done"),
    ("failed", "Failed"),
]

TERMINAL_STATES = {"done", "failed"}

_RECONCILER_ADVISORY_LOCK_ID = 73927462
_WATCHDOG_ADVISORY_LOCK_ID = 73927463

AUTOMATION_STATUS = [
    ("idle", "Idle"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class AuroraPipeline(models.Model):
    _name = "aurora.pipeline"
    _description = "Aurora Pipeline Run"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(
        string="Reference",
        readonly=True,
        copy=False,
        default="New",
    )
    user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.user,
        tracking=True,
    )
    stage = fields.Selection(
        STEP_SELECTION,
        default="draft",
        tracking=True,
        required=True,
    )
    active = fields.Boolean(default=True)

    github_org = fields.Char(string="GitHub Org", required=True)
    github_repo = fields.Char(string="GitHub Repo", required=True)
    skip_pr_fetch = fields.Boolean(
        string="Skip PR Fetch",
        help="Re-use previously fetched PR data.",
    )
    detected_lang = fields.Selection(
        selection=LANGUAGE_SELECTION,
        string="Language",
        readonly=True,
        tracking=True,
        help="Language assigned to this pipeline run. "
             "Set automatically (GitHub API) or manually from Settings.",
    )

    job_name = fields.Char(
        string="K8s Job Name",
        readonly=True,
        index=True,
        copy=False,
    )

    step1_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Fetch PRs")
    step2_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Filter PRs")
    step3_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Discover Tags")
    step4_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Group PRs")
    step5_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Fetch Issues")
    step6_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Build Dataset")

    output_dir = fields.Char(string="Output Directory", readonly=True)
    log = fields.Text(string="Execution Log", readonly=True)

    step1_file = fields.Char(readonly=True)
    step2_file = fields.Char(readonly=True)
    step3_file = fields.Char(readonly=True)
    step4_file = fields.Char(readonly=True)
    step5_file = fields.Char(readonly=True)
    step6_file = fields.Char(readonly=True)

    step1_log = fields.Text(string="Step 1 Log", readonly=True)
    step2_log = fields.Text(string="Step 2 Log", readonly=True)
    step3_log = fields.Text(string="Step 3 Log", readonly=True)
    step4_log = fields.Text(string="Step 4 Log", readonly=True)
    step5_log = fields.Text(string="Step 5 Log", readonly=True)
    step6_log = fields.Text(string="Step 6 Log", readonly=True)

    dataset_url = fields.Char(string="Dataset Download URL", readonly=True)
    dataset_filename = fields.Char(readonly=True)

    last_heartbeat = fields.Datetime(string="Last Heartbeat", readonly=True)
    progress_text = fields.Char(string="Current Progress", readonly=True)

    is_admin = fields.Boolean(compute="_compute_is_admin")
    use_s3 = fields.Boolean(compute="_compute_use_s3")

    pr_count = fields.Integer(string="PRs Fetched", readonly=True)
    filtered_pr_count = fields.Integer(string="PRs After Filter", readonly=True)
    tag_count = fields.Integer(string="Tags Found", readonly=True)
    group_count = fields.Integer(string="Tag Groups", readonly=True)
    issue_count = fields.Integer(string="Issues Fetched", readonly=True)
    dataset_count = fields.Integer(string="Dataset Records", readonly=True)

    phase1_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Phase 1 Status")
    phase1_file = fields.Char(string="Phase 1 JSONL", readonly=True)

    phase2_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Phase 2 Status")
    phase2_file = fields.Char(string="Phase 2 Report", readonly=True)
    phase2_dataset_file = fields.Char(string="Phase 2 Dataset JSONL", readonly=True)
    phase2_final_report_file = fields.Char(string="Phase 2 Final Report", readonly=True)
    phase2_dataset_count = fields.Integer(string="Phase 2 Dataset Records", readonly=True)
    phase2_image_count = fields.Integer(string="Docker Images Built", readonly=True)
    phase2_instance_count = fields.Integer(string="Instances Tested", readonly=True)
    phase2_resolved_count = fields.Integer(string="Resolved Instances", readonly=True)
    phase2_log = fields.Text(string="Phase 2 Log", readonly=True)
    phase2_has_registry = fields.Boolean(string="Registry Available", readonly=True)
    phase2_result_ids = fields.One2many(
        "aurora.pipeline.result",
        "pipeline_id",
        string="Phase 2 Results",
        readonly=True,
    )

    phase3_status = fields.Selection(AUTOMATION_STATUS, default="idle", string="Phase 3 Status")
    phase3_file = fields.Char(string="Phase 3 Output", readonly=True)
    phase3_inference_count = fields.Integer(string="Inferences Run", readonly=True)
    phase3_pass_at_k = fields.Float(string="Pass@K Score", readonly=True, digits=(5, 4))
    phase3_log = fields.Text(string="Phase 3 Log", readonly=True)

    @api.depends_context("uid")
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("aurora.group_aurora_admin")
        for rec in self:
            rec.is_admin = is_admin

    @api.depends("output_dir")
    def _compute_use_s3(self):
        for rec in self:
            rec.use_s3 = bool(rec.output_dir and rec.output_dir.startswith("s3://"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "aurora.pipeline"
                ) or "New"
        return super().create(vals_list)

    def _check_max_active(self):
        max_active = int(
            self.env["ir.config_parameter"].sudo().get_param(
                "aurora.max_active_tasks", "50"
            )
        )
        running = self.sudo().search_count([
            ("stage", "not in", list(TERMINAL_STATES) + ["draft"]),
        ])
        if running >= max_active:
            raise UserError(
                f"Maximum active pipeline runs ({max_active}) reached. "
                "Wait for a run to finish or increase the limit in Settings."
            )

    def _get_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "output_dir": ICP.get_param("aurora.output_dir", "/tmp/aurora_output"),
            "cache_dir": ICP.get_param("aurora.cache_dir", "/tmp/repo_cache"),
            "delay_on_error": int(ICP.get_param("aurora.delay_on_error", "300")),
            "retry_attempts": int(ICP.get_param("aurora.retry_attempts", "3")),
            "max_tags": int(ICP.get_param("aurora.max_tags", "200")),
            "window_days": int(ICP.get_param("aurora.window_days", "30")),
            "lang": ICP.get_param("aurora.lang", "python"),
            "s3_bucket": S3_BUCKET,
            "s3_access_key": _get_env("AURORA_S3_ACCESS_KEY"),
            "s3_secret_key": _get_env("AURORA_S3_SECRET_KEY"),
            "s3_region": S3_REGION,
            "s3_folder": S3_AURORA_PREFIX,
        }

    def _detect_language_from_github(self):
        pool_token = self.env["aurora.github.token"].search(
            [("state", "=", "active"), ("leased_by_run_id", "=", False)], limit=1,
        )
        if not pool_token:
            raise UserError(
                "No GitHub tokens available in the token pool. "
                "Import tokens via Configuration → Import Tokens."
            )
        token_raw = pool_token._decrypt_token(pool_token.token)
        if not token_raw:
            raise UserError(
                "Failed to decrypt token from the pool. "
                "Check your encryption key configuration."
            )

        try:
            g = Github(auth=Auth.Token(token_raw), timeout=15)
            repo = g.get_repo(f"{self.github_org}/{self.github_repo}")
            github_lang = repo.language
        except GithubException as exc:
            raise UserError(
                f"Failed to fetch repo info from GitHub: {exc.data.get('message', exc)}"
            ) from exc
        except Exception as exc:
            raise UserError(
                f"Failed to fetch repo info from GitHub: {exc}"
            ) from exc

        if not github_lang:
            raise UserError(
                f"GitHub reports no primary language for "
                f"{self.github_org}/{self.github_repo}. "
                f"Please switch to Manual mode and select a language."
            )

        mapped = GITHUB_LANG_MAP.get(github_lang)
        if not mapped:
            raise UserError(
                f"GitHub detected language '{github_lang}' for "
                f"{self.github_org}/{self.github_repo}, "
                f"but it is not in the supported language list. "
                f"Supported GitHub languages: {', '.join(sorted(GITHUB_LANG_MAP.keys()))}. "
                f"Please switch to Manual mode and select a language."
            )
        return mapped

    def _resolve_lang(self):
        ICP = self.env["ir.config_parameter"].sudo()
        mode = ICP.get_param("aurora.lang_detection_mode", "manual")
        if mode == "automatic":
            return self._detect_language_from_github()
        return ICP.get_param("aurora.lang", "python")

    def _build_worker_odoo_conf(self):
        """Build odoo.conf content for the worker pod.

        Uses fixed paths matching the worker Docker image layout:
        - /opt/odoo/addons (Odoo core, always auto-added)
        - /opt/odoo/custom_addons (aurora module)

        NOTE: We cannot use odoo_config.get("addons_path") because:
        1. It returns a Python list, not a comma-separated string
        2. The main pod paths (/opt/ethara/app/...) don't exist in the worker image
        """
        data_dir = odoo_config.get("data_dir") or "/tmp/odoo-data"
        swm = odoo_config.get("server_wide_modules")
        server_wide_modules = ",".join(swm) if isinstance(swm, list) else (swm or "base,web")

        # DB credentials are injected as plain env vars and overridden
        # at boot by the worker's _DB_ENV_OVERRIDES mechanism.
        return (
            "[options]\n"
            "admin_passwd = False\n"
            "db_host = False\n"
            "db_port = 5432\n"
            "db_user = False\n"
            "db_password = False\n"
            "db_name = False\n"
            "addons_path = /opt/odoo/addons,/opt/odoo/custom_addons\n"
            f"data_dir = {data_dir}\n"
            "without_demo = all\n"
            f"server_wide_modules = {server_wide_modules}\n"
        )

    def _create_pipeline_secret(self, core_v1, labels):
        secret_name = f"aurora-pipeline-creds-{self.id}"
        secret = k8s_client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=k8s_client.V1ObjectMeta(
                name=secret_name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            string_data={
                "DB_PASSWORD": odoo_config["db_password"] or "",
                "AURORA_ENCRYPTION_KEY": _get_env("AURORA_ENCRYPTION_KEY"),
                "AURORA_S3_ACCESS_KEY": _get_env("AURORA_S3_ACCESS_KEY"),
                "AURORA_S3_SECRET_KEY": _get_env("AURORA_S3_SECRET_KEY"),
            },
        )
        try:
            core_v1.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except K8sApiException as exc:
            if exc.status == 409:
                core_v1.replace_namespaced_secret(
                    name=secret_name, namespace=NAMESPACE, body=secret,
                )
            else:
                raise
        return secret_name

    def _create_worker_configmap(self, core_v1, labels):
        """Create a per-pipeline ConfigMap with odoo.conf for the worker pod."""
        cm_name = f"aurora-worker-config-{self.id}"
        cm = k8s_client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=k8s_client.V1ObjectMeta(
                name=cm_name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            data={"odoo.conf": self._build_worker_odoo_conf()},
        )
        try:
            core_v1.create_namespaced_config_map(namespace=NAMESPACE, body=cm)
        except K8sApiException as exc:
            if exc.status == 409:
                core_v1.replace_namespaced_config_map(
                    name=cm_name, namespace=NAMESPACE, body=cm,
                )
            else:
                raise
        return cm_name

    def _delete_worker_configmap(self):
        cm_name = f"aurora-worker-config-{self.id}"
        try:
            core_v1 = k8s_client.CoreV1Api()
            core_v1.delete_namespaced_config_map(name=cm_name, namespace=NAMESPACE)
        except Exception:
            _logger.debug("ConfigMap %s already gone or failed to delete", cm_name)

    def _delete_pipeline_secret(self):
        secret_name = f"aurora-pipeline-creds-{self.id}"
        try:
            _load_k8s_config()
            core_v1 = k8s_client.CoreV1Api()
            core_v1.delete_namespaced_secret(name=secret_name, namespace=NAMESPACE)
            _logger.info("Deleted K8s Secret %s (pipeline %s)", secret_name, self.id)
        except K8sApiException as exc:
            if exc.status == 404:
                _logger.debug("Secret %s already gone (pipeline %s)", secret_name, self.id)
            else:
                _logger.warning("Failed to delete Secret %s (pipeline %s)", secret_name, self.id)
        except Exception:
            _logger.debug("Secret %s cleanup failed (pipeline %s)", secret_name, self.id)

    def _create_pipeline_job(self):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes Python package is not installed on this server.")

        db_name = self.env.cr.dbname
        job_uid = uuid.uuid4().hex[:12]
        job_name = f"aurora-pipeline-{self.id}-{job_uid}"

        _load_k8s_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()

        labels = {
            "app.kubernetes.io/name": "aurora-pipeline",
            "app.kubernetes.io/component": "pipeline-worker",
            "app.kubernetes.io/managed-by": "aurora-odoo",
            "platform": "aurora",
            "pipeline-id": str(self.id),
        }
        if KUEUE_QUEUE:
            labels["kueue.x-k8s.io/queue-name"] = KUEUE_QUEUE

        secret_name = self._create_pipeline_secret(core_v1, labels)
        cm_name = self._create_worker_configmap(core_v1, labels)

        ICP = self.env["ir.config_parameter"].sudo()
        docker_image = (
            ICP.get_param("aurora.worker_docker_image")
            or _get_env("AURORA_DOCKER_IMAGE")
            or DOCKER_IMAGE
        )

        env_vars = [
            k8s_client.V1EnvVar(name="PIPELINE_ID", value=str(self.id)),
            k8s_client.V1EnvVar(name="ODOO_DB", value=db_name),
            k8s_client.V1EnvVar(
                name="PYTHONPATH",
                value="/opt/odoo:/opt/odoo/custom_addons",
            ),
            k8s_client.V1EnvVar(name="ODOO_CONF", value=ODOO_CONF_PATH),
            k8s_client.V1EnvVar(name="DB_HOST", value=odoo_config["db_host"]),
            k8s_client.V1EnvVar(name="DB_PORT", value=str(odoo_config["db_port"] or "5432")),
            k8s_client.V1EnvVar(name="DB_USER", value=odoo_config["db_user"]),
            k8s_client.V1EnvVar(
                name="DB_PASSWORD",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name, key="DB_PASSWORD",
                    ),
                ),
            ),
            k8s_client.V1EnvVar(
                name="AURORA_ENCRYPTION_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name, key="AURORA_ENCRYPTION_KEY",
                    ),
                ),
            ),
            k8s_client.V1EnvVar(
                name="AURORA_S3_ACCESS_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name, key="AURORA_S3_ACCESS_KEY",
                    ),
                ),
            ),
            k8s_client.V1EnvVar(
                name="AURORA_S3_SECRET_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=secret_name, key="AURORA_S3_SECRET_KEY",
                    ),
                ),
            ),
        ]

        s3_endpoint = _get_env("AURORA_S3_ENDPOINT")
        if s3_endpoint:
            env_vars.append(k8s_client.V1EnvVar(name="AURORA_S3_ENDPOINT", value=s3_endpoint))

        webhook_url = (
            _get_env("AURORA_WEBHOOK_URL")
            or ICP.get_param("aurora.webhook_url")
            or ICP.get_param("web.base.url", "http://localhost:8069")
        )
        if webhook_url:
            env_vars.append(k8s_client.V1EnvVar(name="AURORA_WEBHOOK_URL", value=webhook_url))

        harness_repo = ICP.get_param("aurora.harness_git_repo", "EtharaAI/multi-swe-bench")
        harness_branch = ICP.get_param("aurora.harness_git_branch", "main")
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_REPO", value=harness_repo))
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_BRANCH", value=harness_branch))

        container = k8s_client.V1Container(
            name="pipeline",
            image=docker_image,
            image_pull_policy=IMAGE_PULL_POLICY,
            command=["python", WORKER_SCRIPT],
            env=env_vars,
            security_context=k8s_client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                allow_privilege_escalation=False,
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="odoo-config",
                    mount_path=ODOO_CONF_PATH,
                    sub_path="odoo.conf",
                    read_only=True,
                ),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={
                    "cpu": CPU_REQUEST,
                    "memory": MEMORY_REQUEST,
                },
                limits={
                    "memory": MEMORY_LIMIT,
                },
            ),
        )

        volumes = [
            k8s_client.V1Volume(
                name="odoo-config",
                config_map=k8s_client.V1ConfigMapVolumeSource(name=cm_name),
            ),
        ]

        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace=NAMESPACE,
                labels=labels,
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=600,
                active_deadline_seconds=DEADLINE_SECONDS,
                backoff_limit=1,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(
                        labels=labels,
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=SERVICE_ACCOUNT,
                        restart_policy="Never",
                        node_selector=NODE_SELECTOR,
                        containers=[container],
                        volumes=volumes,
                    ),
                ),
            ),
        )

        batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job)
        return job_name

    def action_run_pipeline(self):
        self.ensure_one()

        self.env.cr.execute(
            "SELECT stage FROM aurora_pipeline WHERE id = %s FOR UPDATE NOWAIT",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if not row or row[0] != "draft":
            raise UserError("Pipeline can only be started from Draft stage.")

        if not _SAFE_GITHUB_NAME.match(self.github_org or ""):
            raise UserError(
                f"Invalid GitHub org name: {self.github_org!r}. "
                "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
            )
        if not _SAFE_GITHUB_NAME.match(self.github_repo or ""):
            raise UserError(
                f"Invalid GitHub repo name: {self.github_repo!r}. "
                "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
            )

        self._check_max_active()

        config = self._get_config()
        pool_has_tokens = self.env["aurora.github.token"].search_count(
            [("state", "=", "active")], limit=1,
        )
        if not pool_has_tokens:
            raise UserError(
                "No GitHub tokens available in the token pool. "
                "Import tokens via Configuration → Import Tokens."
            )
        if not config.get("output_dir"):
            raise UserError(
                "No output directory configured. Go to Settings → Aurora Pipeline."
            )
        if config["retry_attempts"] < 0:
            raise UserError("Retry attempts must be non-negative.")
        if config["max_tags"] < 1:
            raise UserError("Max tags must be at least 1.")
        if config["window_days"] < 1:
            raise UserError("Window days must be at least 1.")

        lang = self._resolve_lang()

        from . import s3_storage

        s3_config = {
            "bucket": config.get("s3_bucket", ""),
            "region": config.get("s3_region", "ap-south-1"),
            "folder": config.get("s3_folder", ""),
        }
        use_s3 = s3_storage.is_configured(s3_config)

        if use_s3:
            s3_folder = s3_config.get("folder", "").strip("/")
            if s3_folder:
                out = f"s3://{s3_config['bucket']}/{s3_folder}/aurora_phase1/{self.github_org}__{self.github_repo}"
            else:
                out = f"s3://{s3_config['bucket']}/aurora_phase1/{self.github_org}__{self.github_repo}"
        else:
            out = os.path.join(
                config["output_dir"],
                f"{self.github_org}__{self.github_repo}",
            )
            os.makedirs(out, exist_ok=True)

        self.write({
            "output_dir": out,
            "stage": "fetch_prs",
            "detected_lang": lang,
            "log": False,
            "progress_text": False,
        })

        k8s_image = DOCKER_IMAGE if K8S_AVAILABLE else ""
        if k8s_image:
            try:
                job_name = self._create_pipeline_job()
                self.write({"job_name": job_name})
                _logger.info(
                    "Pipeline %s (id=%s) submitted as K8s Job %s",
                    self.name, self.id, job_name,
                )
            except Exception as exc:
                _logger.exception("Failed to create K8s Job for pipeline %s", self.id)
                self.write({
                    "stage": "failed",
                    "log": f"[SYSTEM] Failed to create K8s Job: {exc}\n",
                })
                raise UserError(
                    f"Failed to submit pipeline to Kubernetes: {exc}"
                ) from exc
        else:
            self._run_pipeline_local()

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _run_pipeline_local(self):
        from odoo.modules.registry import Registry
        db_name = self.env.cr.dbname
        rec_id = self.id
        rec_name = self.name
        registry = Registry(db_name)

        with _local_threads_lock:
            existing = _local_threads.get(rec_id)
            if existing and existing.is_alive():
                raise UserError(
                    f"Pipeline {rec_name} is already running in this Odoo process. "
                    "Wait for it to finish or cancel it before starting again."
                )

        def _worker():
            import time
            time.sleep(1)
            from ..worker.run_pipeline import run_pipeline
            _logger.info("Pipeline %s (id=%s) starting local execution", rec_name, rec_id)
            try:
                run_pipeline(registry, db_name, rec_id)
            finally:
                with _local_threads_lock:
                    _local_threads.pop(rec_id, None)

        t = threading.Thread(target=_worker, name=f"aurora-local-{rec_id}", daemon=True)
        with _local_threads_lock:
            _local_threads[rec_id] = t

        def _start_thread():
            t.start()
            _logger.info("Pipeline %s (id=%s) launched in local thread", rec_name, rec_id)

        self.env.cr.postcommit.add(_start_thread)

    def action_cancel(self):
        self.ensure_one()
        if self.stage in TERMINAL_STATES:
            raise UserError("Cannot cancel a finished pipeline.")

        if self.job_name and K8S_AVAILABLE:
            try:
                _load_k8s_config()
                batch_v1 = k8s_client.BatchV1Api()
                batch_v1.delete_namespaced_job(
                    name=self.job_name,
                    namespace=NAMESPACE,
                    body=k8s_client.V1DeleteOptions(
                        propagation_policy="Foreground",
                    ),
                )
                _logger.info("Deleted K8s Job %s for pipeline %s", self.job_name, self.id)
            except Exception:
                _logger.warning(
                    "Failed to delete K8s Job %s for pipeline %s (may already be gone)",
                    self.job_name, self.id,
                    exc_info=True,
                )
            self._delete_worker_configmap()
            self._delete_pipeline_secret()

        self.write({"stage": "failed"})
        self.message_post(body="Pipeline cancelled by user.")

    def action_reset_to_draft(self):
        self.ensure_one()
        if self.stage not in TERMINAL_STATES:
            raise UserError("Only finished/failed pipelines can be reset.")
        self.write({
            "stage": "draft",
            "job_name": False,
            "step1_status": "idle",
            "step2_status": "idle",
            "step3_status": "idle",
            "step4_status": "idle",
            "step5_status": "idle",
            "step6_status": "idle",
            "phase1_status": "idle",
            "phase1_file": False,
            "phase2_status": "idle",
            "phase2_file": False,
            "phase2_dataset_file": False,
            "phase2_final_report_file": False,
            "phase2_dataset_count": 0,
            "phase2_image_count": 0,
            "phase2_instance_count": 0,
            "phase2_resolved_count": 0,
            "phase2_log": False,
            "phase2_has_registry": False,
            "phase3_status": "idle",
            "phase3_file": False,
            "phase3_inference_count": 0,
            "phase3_pass_at_k": 0.0,
            "phase3_log": False,
            "step1_log": False,
            "step2_log": False,
            "step3_log": False,
            "step4_log": False,
            "step5_log": False,
            "step6_log": False,
            "log": False,
        })
        self.phase2_result_ids.unlink()

    def action_open_discovery_wizard(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Discover Repositories",
            "res_model": "aurora.discovery.wizard",
            "view_mode": "form",
            "target": "new",
        }

    def _safe_local_download(self, file_url: str):
        import base64
        allowed_base = self._get_config().get("output_dir", "/tmp/aurora_output")
        local_path = _validate_file_path(file_url, allowed_base)
        file_size = os.path.getsize(local_path)
        if file_size > _MAX_DOWNLOAD_FILE_SIZE:
            raise UserError(
                f"File too large for in-browser download "
                f"({file_size / (1024*1024):.0f} MB). "
                f"Use the server filesystem or S3 storage instead."
            )
        with open(local_path, "rb") as f:
            data = base64.b64encode(f.read())
        fname = os.path.basename(local_path)
        attachment = self.env["ir.attachment"].create({
            "name": fname,
            "type": "binary",
            "datas": data,
            "mimetype": "application/jsonl+json",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "new",
        }

    def action_download_dataset(self):
        self.ensure_one()
        if not self.dataset_url:
            from odoo.exceptions import UserError
            raise UserError("No dataset available to download yet.")
        return {
            "type": "ir.actions.act_url",
            "url": self.dataset_url,
            "target": "new",
        }

    def action_download_step_file(self):
        self.ensure_one()
        step = self.env.context.get("step_number")
        field_map = {
            1: "step1_file", 2: "step2_file", 3: "step3_file",
            4: "step4_file", 5: "step5_file", 6: "step6_file",
        }
        field_name = field_map.get(step)
        if not field_name:
            raise UserError("Invalid step number.")
        file_url = getattr(self, field_name, "")
        if not file_url:
            raise UserError(f"No file available for Step {step}.")
        if file_url.startswith("file://"):
            file_url = file_url[7:]
        if os.path.isfile(file_url):
            return self._safe_local_download(file_url)
        if file_url.startswith("https://") and ".s3." in file_url:
            file_url = self._presign_s3_url(file_url)
        return {
            "type": "ir.actions.act_url",
            "url": file_url,
            "target": "new",
        }

    def action_download_phase_file(self):
        self.ensure_one()
        phase = self.env.context.get("phase_number")
        field_map = {1: "phase1_file", 2: "phase2_file", 3: "phase3_file"}
        field_name = field_map.get(phase)
        if not field_name:
            raise UserError("Invalid phase number.")
        file_url = getattr(self, field_name, "")
        if not file_url:
            raise UserError(f"No file available for Phase {phase}.")
        if file_url.startswith("file://"):
            file_url = file_url[7:]
        if os.path.isfile(file_url):
            return self._safe_local_download(file_url)
        if file_url.startswith("https://") and ".s3." in file_url:
            file_url = self._presign_s3_url(file_url)
        return {
            "type": "ir.actions.act_url",
            "url": file_url,
            "target": "new",
        }

    def action_view_phase_file(self):
        self.ensure_one()
        phase = self.env.context.get("phase_number")
        field_map = {1: "phase1_file", 2: "phase2_file", 3: "phase3_file"}
        label_map = {1: "Phase 1 — Data Collection", 2: "Phase 2 — Test Execution", 3: "Phase 3 — Trajectories"}
        field_name = field_map.get(phase)
        if not field_name:
            raise UserError("Invalid phase number.")
        file_url = getattr(self, field_name, "")
        if not file_url:
            raise UserError(f"No file available for Phase {phase}.")
        if self.use_s3:
            raise UserError(
                "Preview is not available for S3-stored files. "
                "Use the Download button instead."
            )
        local_path = file_url[7:] if file_url.startswith("file://") else file_url
        allowed_base = self._get_config().get("output_dir", "/tmp/aurora_output")
        local_path = _validate_file_path(local_path, allowed_base)
        if not os.path.isfile(local_path):
            raise UserError(f"File not found on disk: {local_path}")

        PreviewWizard = self.env["aurora.pipeline.preview"]
        total = self.dataset_count or 0
        preview_text, preview_count = PreviewWizard._build_preview(local_path, total)
        wizard = PreviewWizard.create({
            "phase_label": label_map.get(phase, f"Phase {phase}"),
            "preview_text": preview_text,
            "record_count": total,
            "preview_count": preview_count,
        })
        return {
            "type": "ir.actions.act_window",
            "name": f"JSONL Preview — {label_map.get(phase, f'Phase {phase}')}",
            "res_model": "aurora.pipeline.preview",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_create_registry(self):
        self.ensure_one()

        org = self.github_org or ""
        repo = self.github_repo or ""
        lang = self.detected_lang or ""

        if not org or not repo or not lang:
            raise UserError(
                "Organisation, repository and language must be set before "
                "creating an instance registry."
            )

        from .registry_wizard import (
            _TEMPLATE,
            _find_sample_registry,
            _generate_llm_prompt,
            _to_class_name,
        )
        class_name = _to_class_name(repo)
        content = _TEMPLATE.format(
            class_name=class_name, org=org, repo=repo,
        )

        sample = _find_sample_registry(lang, exclude_org=org)
        llm_prompt = _generate_llm_prompt(org, repo, lang, sample) if sample else ""

        RegistryWiz = self.env["aurora.registry.wizard"]
        repo_safe = repo.replace("-", "_").lower()
        wiz = RegistryWiz.create({
            "pipeline_id": self.id,
            "org": org,
            "repo": repo,
            "lang": lang,
            "filename": f"{repo_safe}.py",
            "registry_content": content,
            "sample_content": sample,
            "llm_prompt": llm_prompt,
        })
        return {
            "type": "ir.actions.act_window",
            "name": f"Create Instance Registry — {org}/{repo}",
            "res_model": "aurora.registry.wizard",
            "res_id": wiz.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_edit_registry(self):
        self.ensure_one()

        org = self.github_org or ""
        repo = self.github_repo or ""
        lang = self.detected_lang or ""

        if not org or not repo or not lang:
            raise UserError(
                "Organisation, repository and language must be set."
            )

        from .registry_wizard import (
            _HARNESS_REPOS_ROOT,
            _find_sample_registry,
            _generate_llm_prompt,
        )

        repo_safe = repo.replace("-", "_").lower()
        registry_file = _HARNESS_REPOS_ROOT / lang / org / f"{repo_safe}.py"

        if not registry_file.exists():
            raise UserError(
                f"Registry file not found at:\n{registry_file}\n\n"
                "Use 'Create Instance Registry' to generate one first."
            )

        content = registry_file.read_text()
        sample = _find_sample_registry(lang, exclude_org=org)
        llm_prompt = _generate_llm_prompt(org, repo, lang, sample) if sample else ""

        RegistryWiz = self.env["aurora.registry.wizard"]
        wiz = RegistryWiz.create({
            "pipeline_id": self.id,
            "org": org,
            "repo": repo,
            "lang": lang,
            "filename": f"{repo_safe}.py",
            "registry_content": content,
            "sample_content": sample,
            "llm_prompt": llm_prompt,
            "edit_mode": True,
        })
        return {
            "type": "ir.actions.act_window",
            "name": f"Edit Instance Registry — {org}/{repo}",
            "res_model": "aurora.registry.wizard",
            "res_id": wiz.id,
            "view_mode": "form",
            "target": "new",
        }

    def _presign_s3_url(self, public_url):
        from . import s3_storage
        from urllib.parse import urlparse
        parsed = urlparse(public_url)
        if not parsed.scheme == "https" or not parsed.path:
            return public_url
        # Virtual-hosted style: bucket.s3.region.amazonaws.com/key
        m = re.match(r"(.+?)\.s3[.-](.+?)\.amazonaws\.com$", parsed.hostname or "")
        if not m:
            # Path-style: s3.region.amazonaws.com/bucket/key
            m = re.match(r"s3[.-](.+?)\.amazonaws\.com$", parsed.hostname or "")
            if not m:
                return public_url
            s3_key = "/".join(parsed.path.strip("/").split("/")[1:])
        else:
            s3_key = parsed.path.lstrip("/")
        config = self._get_config()
        s3_cfg = {
            "bucket": config.get("s3_bucket", ""),
            "access_key": config.get("s3_access_key", ""),
            "secret_key": config.get("s3_secret_key", ""),
            "region": config.get("s3_region", "ap-south-1"),
        }
        if not s3_storage.is_configured(s3_cfg):
            return public_url
        try:
            return s3_storage.generate_presigned_url(s3_cfg, s3_key)
        except Exception:
            _logger.warning("Failed to generate presigned URL, falling back to public URL", exc_info=True)
            return public_url

    @api.model
    def _cron_reconcile_pipelines(self):
        """Sync K8s Job status back to pipeline records.

        Runs every minute.  For each running pipeline that has a job_name,
        checks whether the K8s Job succeeded, failed, or vanished, and
        updates the pipeline record accordingly.
        """
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_RECONCILER_ADVISORY_LOCK_ID,))
        if not self.env.cr.fetchone()[0]:
            _logger.debug("Reconciler: another instance running, skipping")
            return
        try:
            self._run_reconcile_pipelines()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_RECONCILER_ADVISORY_LOCK_ID,))

    def _run_reconcile_pipelines(self):
        active = self.sudo().search([
            ("stage", "not in", list(TERMINAL_STATES) + ["draft"]),
            ("job_name", "!=", False),
        ])
        if not active:
            return

        if not K8S_AVAILABLE:
            _logger.warning("kubernetes package not available, skipping reconciliation")
            return

        try:
            _load_k8s_config()
            batch_v1 = k8s_client.BatchV1Api()
            jobs = batch_v1.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector="platform=aurora",
            )
        except Exception:
            _logger.exception("Failed to list K8s Jobs for reconciliation")
            return

        job_map = {}
        for job in jobs.items:
            jn = job.metadata.name
            if jn:
                job_map[jn] = job

        for pipeline in active:
            job = job_map.get(pipeline.job_name)

            if not job:
                age = (fields.Datetime.now() - pipeline.create_date).total_seconds()
                if age > 300:
                    _logger.warning(
                        "Reconciler: Job %s not found for pipeline %s (age=%ds), marking failed",
                        pipeline.job_name, pipeline.id, age,
                    )
                    pipeline.write({"stage": "failed"})
                    pipeline.message_post(
                        body="Pipeline marked failed: K8s Job not found in cluster.",
                        message_type="comment",
                        subtype_xmlid="mail.mt_note",
                    )
                continue

            if job.status.succeeded and job.status.succeeded > 0:
                if pipeline.stage != "done":
                    _logger.info(
                        "Reconciler: Job %s succeeded, pipeline %s already at stage=%s",
                        pipeline.job_name, pipeline.id, pipeline.stage,
                    )
                continue

            if job.status.failed and job.status.failed > 0:
                if pipeline.stage != "failed":
                    reason = "K8s Job failed"
                    conditions = job.status.conditions or []
                    for cond in conditions:
                        if cond.type == "Failed" and cond.reason == "DeadlineExceeded":
                            hours = DEADLINE_SECONDS / 3600
                            reason = (
                                f"Pipeline timed out after {hours:.0f} hours "
                                f"(K8s activeDeadlineSeconds={DEADLINE_SECONDS}). "
                                "Increase the deadline or optimize the pipeline."
                            )
                            break
                        if cond.type == "Failed" and cond.reason == "BackoffLimitExceeded":
                            reason = "K8s Job pod crashed (BackoffLimitExceeded)"
                            break
                    _logger.warning(
                        "Reconciler: Job %s failed (%s), marking pipeline %s as failed",
                        pipeline.job_name, reason, pipeline.id,
                    )
                    pipeline.write({"stage": "failed"})
                    pipeline.message_post(
                        body=f"Pipeline failed: {reason}",
                        message_type="comment",
                        subtype_xmlid="mail.mt_note",
                    )

    @api.model
    def _cron_watchdog_stalled(self):
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (_WATCHDOG_ADVISORY_LOCK_ID,))
        if not self.env.cr.fetchone()[0]:
            _logger.debug("Watchdog: another instance running, skipping")
            return
        try:
            self._run_watchdog_stalled()
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (_WATCHDOG_ADVISORY_LOCK_ID,))

    def _run_watchdog_stalled(self):
        stale_threshold = fields.Datetime.subtract(
            fields.Datetime.now(), minutes=10
        )
        stalled = self.sudo().search([
            ("stage", "not in", ["draft", "done", "failed"]),
            ("last_heartbeat", "<", stale_threshold),
        ])
        for rec in stalled:
            _logger.warning(
                "Aurora watchdog: pipeline %s (id=%s) stalled — last heartbeat %s. Marking failed.",
                rec.name, rec.id, rec.last_heartbeat,
            )
            if rec.job_name and K8S_AVAILABLE:
                try:
                    _load_k8s_config()
                    batch_v1 = k8s_client.BatchV1Api()
                    batch_v1.delete_namespaced_job(
                        name=rec.job_name,
                        namespace=NAMESPACE,
                        body=k8s_client.V1DeleteOptions(
                            propagation_policy="Foreground",
                        ),
                    )
                except Exception:
                    _logger.warning("Failed to delete stalled Job %s", rec.job_name, exc_info=True)
                rec._delete_worker_configmap()
                rec._delete_pipeline_secret()

            rec.write({"stage": "failed"})
            rec.message_post(
                body="Pipeline marked as failed by watchdog (no heartbeat for 10+ minutes).",
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

    @api.model
    def get_dashboard_data(self):
        cr = self.env.cr
        stage_labels = dict(STEP_SELECTION)

        cr.execute("""
            SELECT stage, COUNT(*) FROM aurora_pipeline GROUP BY stage
        """)
        pipe_counts = dict(cr.fetchall())
        running_stages = {"fetch_prs", "filter_prs", "discover_tags", "group_prs", "fetch_issues", "build_dataset"}

        cr.execute("""
            SELECT stage, COUNT(*) FROM aurora_evaluation GROUP BY stage
        """)
        eval_counts = dict(cr.fetchall())
        eval_running_stages = {"building_images", "running_instances", "generating_reports"}

        eval_stage_labels = dict([
            ("draft", "Draft"), ("building_images", "Building Images"),
            ("running_instances", "Running Instances"), ("generating_reports", "Generating Reports"),
            ("done", "Done"), ("failed", "Failed"),
        ])

        cr.execute("""
            SELECT id, name, github_org, github_repo, stage
            FROM aurora_pipeline ORDER BY id DESC LIMIT 8
        """)
        recent_pipes = [{
            "id": r[0], "name": r[1],
            "repo": f"{r[2]}/{r[3]}" if r[2] and r[3] else "",
            "stage": r[4],
            "stage_label": stage_labels.get(r[4], r[4]),
        } for r in cr.fetchall()]

        cr.execute("""
            SELECT e.id, e.name, e.stage, p.name as pipeline_name
            FROM aurora_evaluation e
            LEFT JOIN aurora_pipeline p ON e.pipeline_id = p.id
            ORDER BY e.id DESC LIMIT 8
        """)
        recent_evals = [{
            "id": r[0], "name": r[1],
            "stage": r[2],
            "stage_label": eval_stage_labels.get(r[2], r[2]),
            "pipeline_name": r[3] or "",
        } for r in cr.fetchall()]

        token_active = 0
        token_total = 0
        recent_tokens = []
        try:
            cr.execute("SELECT COUNT(*) FROM aurora_github_token WHERE state = 'active'")
            token_active = cr.fetchone()[0]
            cr.execute("SELECT COUNT(*) FROM aurora_github_token")
            token_total = cr.fetchone()[0]
            cr.execute("""
                SELECT id, name, state, rate_limit_remaining, rate_limit_reset
                FROM aurora_github_token
                ORDER BY state, name
                LIMIT 20
            """)
            for r in cr.fetchall():
                recent_tokens.append({
                    "id": r[0],
                    "name": r[1] or f"Token #{r[0]}",
                    "state": r[2],
                    "rate_remaining": r[3] or 0,
                    "rate_limit": r[4].isoformat() if r[4] else "",
                })
        except Exception:
            pass

        cr.execute("""
            SELECT phase3_status, COUNT(*)
            FROM aurora_pipeline
            WHERE phase3_status IS NOT NULL AND phase3_status != 'idle'
            GROUP BY phase3_status
        """)
        p3_counts = dict(cr.fetchall())
        p3_running = p3_counts.get("running", 0)
        p3_done = p3_counts.get("done", 0)
        p3_failed = p3_counts.get("failed", 0)
        p3_total = sum(p3_counts.values())

        ei_counts = {}
        try:
            cr.execute("""
                SELECT status, COUNT(*) FROM aurora_evaluation_instance GROUP BY status
            """)
            ei_counts = dict(cr.fetchall())
        except Exception:
            ei_counts = {}
        ei_running_statuses = {"building", "running"}
        ei_total = sum(ei_counts.values())
        ei_resolved = ei_counts.get("resolved", 0)
        ei_unresolved = ei_counts.get("unresolved", 0)
        ei_error = ei_counts.get("error", 0)
        ei_running = sum(ei_counts.get(s, 0) for s in ei_running_statuses)

        hs_counts = {}
        try:
            cr.execute("""
                SELECT stage, COUNT(*) FROM aurora_harness_staging
                WHERE active = TRUE
                GROUP BY stage
            """)
            hs_counts = dict(cr.fetchall())
        except Exception:
            hs_counts = {}
        hs_testing_stages = {"testing", "tested"}
        hs_total = sum(hs_counts.values())
        hs_deployed = hs_counts.get("deployed", 0)
        hs_notified = hs_counts.get("notified", 0)
        hs_failed = hs_counts.get("failed", 0)
        hs_testing = sum(hs_counts.get(s, 0) for s in hs_testing_stages)

        return {
            "pipelines": {
                "total": sum(pipe_counts.values()),
                "running": sum(pipe_counts.get(s, 0) for s in running_stages),
                "done": pipe_counts.get("done", 0),
                "failed": pipe_counts.get("failed", 0),
                "draft": pipe_counts.get("draft", 0),
            },
            "evaluations": {
                "total": sum(eval_counts.values()),
                "running": sum(eval_counts.get(s, 0) for s in eval_running_stages),
                "done": eval_counts.get("done", 0),
                "failed": eval_counts.get("failed", 0),
                "draft": eval_counts.get("draft", 0),
            },
            "phase1": {
                "total": sum(pipe_counts.values()),
                "running": sum(pipe_counts.get(s, 0) for s in running_stages),
                "done": pipe_counts.get("done", 0),
                "failed": pipe_counts.get("failed", 0),
                "draft": pipe_counts.get("draft", 0),
            },
            "phase2": {
                "total": sum(eval_counts.values()),
                "running": sum(eval_counts.get(s, 0) for s in eval_running_stages),
                "done": eval_counts.get("done", 0),
                "failed": eval_counts.get("failed", 0),
                "draft": eval_counts.get("draft", 0),
            },
            "phase3": {
                "total": p3_total,
                "running": p3_running,
                "done": p3_done,
                "failed": p3_failed,
                "draft": max(0, p3_total - p3_running - p3_done - p3_failed),
            },
            "evaluation_results": {
                "total": ei_total,
                "resolved": ei_resolved,
                "unresolved": ei_unresolved,
                "error": ei_error,
                "running": ei_running,
            },
            "harness_staging": {
                "total": hs_total,
                "deployed": hs_deployed,
                "notified": hs_notified,
                "testing": hs_testing,
                "failed": hs_failed,
            },
            "recent_pipelines": recent_pipes,
            "recent_evaluations": recent_evals,
            "tokens": {"active": token_active, "total": token_total, "list": recent_tokens},
            "discovery": self._get_discovery_stats(),
        }

    def _get_discovery_stats(self):
        cr = self.env.cr
        try:
            cr.execute("SELECT state, COUNT(*) FROM aurora_discovery GROUP BY state")
            counts = dict(cr.fetchall())
        except Exception:
            counts = {}
        return {
            "total": sum(counts.values()),
            "validated": counts.get("validated", 0),
            "promoted": counts.get("promoted", 0),
            "rejected": counts.get("rejected", 0),
            "new": counts.get("new", 0),
        }
