import json
import logging
import os
import threading
import time
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config as odoo_config

from . import evaluation_executor

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

# Reload K8s config every 50 minutes to handle SA token expiry (default 1h).
_K8S_CONFIG_MAX_AGE = 3000

# ---------------------------------------------------------------------------
# Phase 2 infrastructure constants (DinD-enabled pods for Docker builds).
# ---------------------------------------------------------------------------
EVAL_NAMESPACE = "aurora"
EVAL_NODE_SELECTOR = (
    {} if os.environ.get("AURORA_LOCAL_MODE") else {"ethara.ai/node-pool": "general-purpose"}
)
EVAL_SERVICE_ACCOUNT = "aurora-worker"
EVAL_KUEUE_QUEUE = "" if os.environ.get("AURORA_LOCAL_MODE") else "aurora-pipelines"
EVAL_DOCKER_IMAGE = "426628337772.dkr.ecr.ap-south-1.amazonaws.com/aurora-worker:latest"
EVAL_IMAGE_PULL_POLICY = "IfNotPresent" if os.environ.get("AURORA_LOCAL_MODE") else "Always"
EVAL_DIND_IMAGE = "docker:27-dind"
EVAL_BINFMT_IMAGE = "tonistiigi/binfmt:latest"
EVAL_CPU_REQUEST = "500m" if os.environ.get("AURORA_LOCAL_MODE") else "2"
EVAL_MEMORY_REQUEST = "1Gi" if os.environ.get("AURORA_LOCAL_MODE") else "4Gi"
EVAL_MEMORY_LIMIT = "" if os.environ.get("AURORA_LOCAL_MODE") else "8Gi"
EVAL_DIND_CPU_REQUEST = "500m" if os.environ.get("AURORA_LOCAL_MODE") else "2"
EVAL_DIND_MEMORY_REQUEST = "2Gi" if os.environ.get("AURORA_LOCAL_MODE") else "4Gi"
EVAL_DIND_MEMORY_LIMIT = "" if os.environ.get("AURORA_LOCAL_MODE") else "8Gi"
EVAL_DEADLINE_SECONDS = 28800  # 8 hours
EVAL_WORKSPACE_SIZE = "20Gi" if os.environ.get("AURORA_LOCAL_MODE") else "100Gi"
EVAL_DIND_STORAGE_SIZE = "20Gi" if os.environ.get("AURORA_LOCAL_MODE") else "100Gi"
EVAL_WORKER_SCRIPT = "/opt/odoo/custom_addons/aurora/worker/run_evaluation.py"
EVAL_ODOO_CONF_PATH = "/etc/odoo/odoo.conf"
EVAL_WORKSPACE_PATH = "/tmp/aurora_output"


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

EVAL_STAGE_SELECTION = [
    ("draft", "Draft"),
    ("building_images", "Building Images"),
    ("running_instances", "Running Instances"),
    ("generating_reports", "Generating Reports"),
    ("awaiting_tar_decision", "Awaiting Tar Decision"),
    ("done", "Done"),
    ("failed", "Failed"),
]

EVAL_TERMINAL_STATES = {"done", "failed"}

EVAL_STATUS = [
    ("idle", "Idle"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]


class AuroraEvaluation(models.Model):
    _name = "aurora.evaluation"
    _description = "Aurora Evaluation Run"
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
        EVAL_STAGE_SELECTION,
        default="draft",
        tracking=True,
        required=True,
    )
    active = fields.Boolean(default=True)

    pipeline_id = fields.Many2one(
        "aurora.pipeline",
        string="Source Pipeline",
        help="Select a completed collect pipeline. Dataset file is auto-filled.",
    )
    dataset_source = fields.Selection(
        [("pipeline", "From Pipeline"), ("custom", "Custom Import")],
        string="Dataset Source",
        default="pipeline",
        required=True,
    )
    custom_org = fields.Char(string="GitHub Org")
    custom_repo = fields.Char(string="GitHub Repo")
    custom_jsonl_file = fields.Binary(string="Dataset JSONL File", attachment=False)
    custom_jsonl_filename = fields.Char()
    dataset_file = fields.Char(
        string="Dataset File",
        help="Auto-filled from Source Pipeline. Override for custom dataset.",
    )
    patch_file = fields.Char(string="Patch File", readonly=True)
    repo_dir = fields.Char(string="Repository Directory", readonly=True)
    workdir = fields.Char(string="Working Directory", readonly=True)
    output_dir = fields.Char(
        string="Output Directory",
        help="Auto-set to harness/org__repo/. Override if needed.",
    )

    @api.onchange("pipeline_id")
    def _onchange_pipeline_id(self):
        if not self.pipeline_id:
            return
        pl = self.pipeline_id
        if pl.step6_file:
            self.dataset_file = pl.step6_file
        if pl.github_org and pl.github_repo:
            ICP = self.env["ir.config_parameter"].sudo()
            base = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
            org_repo = f"{pl.github_org}__{pl.github_repo}"
            self.output_dir = os.path.join(base, "harness", org_repo)

    @api.constrains("docker_platform")
    def _check_docker_platform_multi_arch(self):
        """Reject single-arch or empty docker_platform on full evals.

        Test-evals (staging_test_id set) are exempt because they don't push to
        ECR — their builds can stay single-arch fast.
        """
        for rec in self:
            if rec.staging_test_id:
                continue
            value = (rec.docker_platform or "").strip()
            if "," not in value:
                raise ValidationError(
                    "Docker Platform must be multi-arch (comma-separated), "
                    "e.g. 'linux/amd64,linux/arm64'. The ECR push phase pushes "
                    "the OCI manifest list and requires multiple architectures. "
                    f"Got: {value!r}"
                )

    force_build = fields.Boolean(
        string="Force Build",
        help="Rebuild Docker images even if they already exist.",
    )
    max_workers_build = fields.Integer(
        string="Max Build Workers",
        default=4,
    )
    max_workers_run = fields.Integer(
        string="Max Run Workers",
        default=4,
    )
    docker_platform = fields.Char(
        string="Docker Platform",
        default="linux/amd64,linux/arm64",
        help='Build platforms for buildx. For *full* evaluations this must be '
             'multi-arch (comma-separated, e.g. "linux/amd64,linux/arm64") '
             'because the ECR push phase pushes the OCI manifest list and '
             'requires both arches present — enforced by _check_docker_platform_'
             'multi_arch. Test-evals (staging_test_id set) are exempt so the '
             'Test Harness dry-run keeps its fast single-arch path.',
    )
    instance_limit = fields.Integer(
        string="Instance Limit",
        default=0,
        help="Max instances to evaluate. 0 = all.",
    )
    specific_prs = fields.Char(
        string="Specific PRs",
        help="Comma-separated: 'org/repo:pr-123,org/repo:pr-456'. Empty = all.",
    )

    staging_test_id = fields.Many2one(
        "aurora.harness.staging",
        string="Staging Test For",
        readonly=True,
        index=True,
        ondelete="set null",
        help="Set when this evaluation is a Test Harness dry-run dispatched "
             "from the staging form. Hidden from the default Evaluations list.",
    )
    test_eval_promoted = fields.Boolean(
        string="Test Result Promoted",
        default=False,
        readonly=True,
        help="True once the cron has mirrored this test-eval's terminal "
             "status back to the linked staging record.",
    )

    tar_decision = fields.Selection(
        [("pending", "Pending"), ("export", "Export Tars"), ("skip", "Skip")],
        default="pending",
        readonly=True,
        help="User's button choice during the awaiting_tar_decision window. "
             "Worker polls this field and acts when it leaves 'pending'.",
    )
    tar_decision_deadline = fields.Datetime(
        string="Tar Decision Deadline",
        readonly=True,
        help="UTC timestamp after which the worker auto-skips the tar export.",
    )
    tar_decision_window_minutes = fields.Integer(
        string="Tar Decision Window (min)",
        default=20,
        help="Worker pauses for this many minutes after the report is uploaded, "
             "waiting for Export or Skip. 0 disables the wait entirely.",
    )
    oci_export_status = fields.Selection(
        [("idle", "Idle"), ("running", "Running"), ("done", "Done"),
         ("failed", "Failed"), ("skipped", "Skipped")],
        default="idle",
        readonly=True,
    )
    ecr_repository = fields.Char(
        string="ECR Repository",
        readonly=True,
        help="Eval-level ECR repo (no tag) — e.g. "
             "<account>.dkr.ecr.<region>.amazonaws.com/aurora/<org>__<repo>.",
    )
    ecr_pushed_count = fields.Integer(
        string="ECR Images Pushed",
        default=0,
        readonly=True,
        help="Count of resolved-and-successfully-pushed images.",
    )
    ecr_manifest_s3_uri = fields.Char(
        string="ECR Manifest (S3)",
        readonly=True,
        help="URL of the auto-published ecr_manifest.json (sibling of final_report.json).",
    )

    build_status = fields.Selection(EVAL_STATUS, default="idle", string="Build Images")
    run_status = fields.Selection(EVAL_STATUS, default="idle", string="Run Instances")
    report_status = fields.Selection(EVAL_STATUS, default="idle", string="Generate Reports")

    log = fields.Text(string="Execution Log", readonly=True)
    last_heartbeat = fields.Datetime(string="Last Heartbeat", readonly=True)
    progress_text = fields.Char(string="Current Progress", readonly=True)

    total_instances = fields.Integer(string="Total Instances", readonly=True)
    resolved_instances = fields.Integer(string="Resolved", readonly=True)
    unresolved_instances = fields.Integer(string="Unresolved", readonly=True)
    error_instances = fields.Integer(string="Errors", readonly=True)

    final_report_file = fields.Char(string="Final Report", readonly=True)
    dataset_jsonl_url = fields.Char(
        string="Dataset JSONL",
        readonly=True,
        help="S3 URL of the Phase 2 evaluation dataset JSONL produced during report generation.",
    )
    base_dockerfile_content = fields.Text(
        string="Base Dockerfile",
        readonly=True,
        help="Content of the base Docker image Dockerfile (shared across all PR instances).",
    )
    base_dockerfile_s3_uri = fields.Char(
        string="Base Dockerfile S3 URI",
        readonly=True,
        help="S3 URI of the uploaded base image Dockerfile.",
    )
    show_base_dockerfile = fields.Boolean(
        string="Show Base Dockerfile",
        default=False,
        store=False,
    )
    missing_registries = fields.Text(
        string="Missing Harness Registries",
        readonly=True,
        help="Repos in the dataset that have no harness implementation.",
    )
    base_fallback_prs = fields.Text(
        string="PRs using base harness (no interval match)",
        readonly=True,
        help="PR numbers whose `number` didn't fall in any registered interval "
             "file. The base `{repo}.py` harness handled them — verify the "
             "dependency setup matches before trusting results.",
    )

    instance_ids = fields.One2many(
        "aurora.evaluation.instance",
        "evaluation_id",
        string="Instances",
    )
    instance_count = fields.Integer(compute="_compute_instance_count")

    s3_run_number = fields.Integer(
        string="S3 Run #",
        readonly=True,
        copy=False,
        help="Incrementing run number chosen for this evaluation's S3 layout: "
             "{folder}/aurora_phase2/{org}__{repo}/run_{N}/pr-{pr}/<artifact>.",
    )
    s3_base_uri = fields.Char(
        string="S3 Path",
        compute="_compute_s3_base_uri",
    )

    @api.depends("s3_run_number", "pipeline_id.github_org", "pipeline_id.github_repo", "dataset_source", "custom_org", "custom_repo")
    def _compute_s3_base_uri(self):
        from .pipeline import S3_BUCKET, S3_AURORA_PREFIX
        for rec in self:
            org = ""
            repo = ""
            if rec.dataset_source == "custom":
                org = rec.custom_org or ""
                repo = rec.custom_repo or ""
            elif rec.pipeline_id:
                org = rec.pipeline_id.github_org or ""
                repo = rec.pipeline_id.github_repo or ""
            if rec.s3_run_number and org and repo:
                prefix = S3_AURORA_PREFIX.strip("/")
                rec.s3_base_uri = (
                    f"s3://{S3_BUCKET}/{prefix}/aurora_phase2/"
                    f"{org}__{repo}/run_{rec.s3_run_number}"
                )
            else:
                rec.s3_base_uri = False

    @api.depends("instance_ids")
    def _compute_instance_count(self):
        for rec in self:
            rec.instance_count = len(rec.instance_ids)

    is_admin = fields.Boolean(compute="_compute_is_admin")

    @api.depends_context("uid")
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group("aurora.group_aurora_admin")
        for rec in self:
            rec.is_admin = is_admin

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "aurora.evaluation"
                ) or "New"
        return super().create(vals_list)

    @staticmethod
    def _resolve_entry_number(entry):
        if "number" in entry:
            val = entry["number"]
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)
            if isinstance(val, str) and "-" in val:
                head = val.split("-", 1)[0]
                if head.isdigit():
                    return int(head)
        pr_numbers = entry.get("pr_numbers") or []
        if isinstance(pr_numbers, list) and pr_numbers:
            try:
                return int(pr_numbers[0])
            except (TypeError, ValueError):
                pass
        return None

    def _prepare_custom_dataset(self):
        import base64
        if not self.custom_jsonl_file:
            raise UserError("Please upload a JSONL file for the custom dataset.")
        if not self.custom_org or not self.custom_repo:
            raise UserError("GitHub Org and Repo are required for a custom dataset.")

        filename = (self.custom_jsonl_filename or "dataset.jsonl").strip()
        if not filename.lower().endswith(".jsonl"):
            raise UserError("Only .jsonl files are supported.")

        raw = base64.b64decode(self.custom_jsonl_file)
        lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
        if not lines:
            raise UserError("The uploaded JSONL file is empty.")
        try:
            json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise UserError(f"Invalid JSONL: first line is not valid JSON: {exc}") from exc

        ICP = self.env["ir.config_parameter"].sudo()
        base_dir = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
        cache_dir = os.path.join(base_dir, "dataset_cache")
        os.makedirs(cache_dir, exist_ok=True)
        local_path = os.path.join(cache_dir, f"custom_{self.id}_{filename}")
        with open(local_path, "wb") as fh:
            fh.write(raw)

        self.write({"dataset_file": local_path})

    def action_run_evaluation(self):
        self.ensure_one()
        if self.stage != "draft":
            raise UserError("Evaluation can only be started from Draft stage.")

        if self.specific_prs and "org/repo:pr-" in self.specific_prs:
            raise UserError(
                "The 'Specific PRs' field contains the placeholder text "
                "'org/repo:pr-123,org/repo:pr-456'. This is example text \u2014 not a valid filter. "
                "Clear the field to evaluate all dataset entries, or set it to real "
                "instance_id values from the dataset (e.g. 'gorilla/mux:pr-337')."
            )

        if self.dataset_source == "custom":
            self._prepare_custom_dataset()

        pl = self.pipeline_id
        if pl and not self.dataset_file and pl.step6_file:
            self.dataset_file = pl.step6_file

        if not self.dataset_file:
            raise UserError(
                "Dataset file is required. Select a Source Pipeline or set it manually."
            )
        from . import dataset_resolver
        if dataset_resolver.is_remote(self.dataset_file):
            self.dataset_file = dataset_resolver.resolve_to_local(
                self.env, self.dataset_file
            )
        if not os.path.isfile(self.dataset_file):
            raise UserError(f"Dataset file not found: {self.dataset_file}")

        ICP = self.env["ir.config_parameter"].sudo()
        default_base = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")

        org_repo = ""
        if pl and pl.github_org and pl.github_repo:
            org_repo = f"{pl.github_org}__{pl.github_repo}"

        if not self.output_dir:
            if org_repo:
                self.output_dir = os.path.join(default_base, "harness", org_repo)
            else:
                self.output_dir = os.path.join(default_base, "harness", self.name)
        if not self.workdir:
            self.workdir = os.path.join(self.output_dir, "workdir")
        if not self.repo_dir:
            self.repo_dir = os.path.join(default_base, "repos")

        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.repo_dir, exist_ok=True)

        if not self.patch_file:
            self.patch_file = self.dataset_file

        self.write({
            "stage": "building_images",
            "build_status": "idle",
            "run_status": "idle",
            "report_status": "idle",
        })

        use_k8s = K8S_AVAILABLE and _get_env("AURORA_EVAL_USE_K8S", "1") == "1"

        if use_k8s:
            try:
                job_name = self._create_evaluation_job()
                self.message_post(
                    body=f"Evaluation submitted as K8s Job: <code>{job_name}</code>",
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )
            except Exception as exc:
                _logger.warning(
                    "K8s job creation failed for eval %s, falling back to local: %s",
                    self.id, exc,
                )
                self._submit_local_evaluation()
        else:
            self._submit_local_evaluation()

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _submit_local_evaluation(self):
        db_name = self.env.cr.dbname
        uid = self.env.uid
        rec_id = self.id

        def _safe_submit():
            try:
                evaluation_executor.submit_evaluation_async(db_name, uid, rec_id)
            except Exception:
                _logger.exception("Failed to submit evaluation %s to executor", rec_id)
                submit_cr = None
                try:
                    submit_cr = self.env.registry.cursor()
                    submit_cr.execute(
                        "UPDATE aurora_evaluation SET stage = 'failed', "
                        "log = COALESCE(log, '') || %s WHERE id = %s",
                        ["[SYSTEM] Failed to submit evaluation to background executor.\n", rec_id],
                    )
                    submit_cr.commit()
                except Exception:
                    _logger.exception(
                        "Failed to record submission failure for eval rec=%s", rec_id
                    )
                finally:
                    if submit_cr:
                        submit_cr.close()

        self.env.cr.postcommit.add(_safe_submit)

    def _build_eval_odoo_conf(self):
        data_dir = odoo_config.get("data_dir") or "/tmp/odoo-data"
        swm = odoo_config.get("server_wide_modules")
        server_wide_modules = ",".join(swm) if isinstance(swm, list) else (swm or "base,web")
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

    def _create_eval_secret(self, core_v1, labels):
        secret_name = f"aurora-eval-creds-{self.id}"
        from odoo.tools import config as odoo_cfg
        # Only include AWS keys when they are actually configured. In prod
        # with IRSA, leaving the keys out of the Secret means the env vars
        # in the worker container (with optional=True secretKeyRef) will NOT
        # be set, which lets the AWS SDK fall through to the IRSA web-identity
        # provider. If we wrote empty strings here, AWS_ACCESS_KEY_ID="" in
        # the worker would override IRSA and fail.
        secret_data = {
            "DB_PASSWORD": odoo_cfg["db_password"] or "",
            "AURORA_ENCRYPTION_KEY": _get_env("AURORA_ENCRYPTION_KEY"),
            "AURORA_S3_ACCESS_KEY": _get_env("AURORA_S3_ACCESS_KEY"),
            "AURORA_S3_SECRET_KEY": _get_env("AURORA_S3_SECRET_KEY"),
        }
        aws_id = _get_env("AURORA_AWS_ACCESS_KEY_ID")
        aws_sk = _get_env("AURORA_AWS_SECRET_ACCESS_KEY")
        if aws_id and aws_sk:
            secret_data["AURORA_AWS_ACCESS_KEY_ID"] = aws_id
            secret_data["AURORA_AWS_SECRET_ACCESS_KEY"] = aws_sk
        secret = k8s_client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=k8s_client.V1ObjectMeta(
                name=secret_name,
                namespace=EVAL_NAMESPACE,
                labels=labels,
            ),
            string_data=secret_data,
        )
        try:
            core_v1.create_namespaced_secret(namespace=EVAL_NAMESPACE, body=secret)
        except K8sApiException as exc:
            if exc.status == 409:
                core_v1.replace_namespaced_secret(
                    name=secret_name, namespace=EVAL_NAMESPACE, body=secret,
                )
            else:
                raise
        return secret_name

    def _create_eval_configmap(self, core_v1, labels):
        cm_name = f"aurora-eval-config-{self.id}"
        cm = k8s_client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=k8s_client.V1ObjectMeta(
                name=cm_name,
                namespace=EVAL_NAMESPACE,
                labels=labels,
            ),
            data={"odoo.conf": self._build_eval_odoo_conf()},
        )
        try:
            core_v1.create_namespaced_config_map(namespace=EVAL_NAMESPACE, body=cm)
        except K8sApiException as exc:
            if exc.status == 409:
                core_v1.replace_namespaced_config_map(
                    name=cm_name, namespace=EVAL_NAMESPACE, body=cm,
                )
            else:
                raise
        return cm_name

    def _cleanup_k8s_resources(self):
        """Delete the K8s Secret and ConfigMap created for this evaluation.

        Called on cancel/watchdog/completion to prevent resource accumulation.
        Follows Talos destroy pattern: explicit delete with graceful 404 handling.
        """
        if not K8S_AVAILABLE:
            return
        try:
            _load_k8s_config()
            core_v1 = k8s_client.CoreV1Api()
        except Exception:
            _logger.debug("Cannot load K8s config for cleanup (eval %s)", self.id)
            return

        secret_name = f"aurora-eval-creds-{self.id}"
        cm_name = f"aurora-eval-config-{self.id}"

        for kind, name, delete_fn in [
            ("Secret", secret_name, core_v1.delete_namespaced_secret),
            ("ConfigMap", cm_name, core_v1.delete_namespaced_config_map),
        ]:
            try:
                delete_fn(name=name, namespace=EVAL_NAMESPACE)
                _logger.info("Deleted K8s %s %s (eval %s)", kind, name, self.id)
            except K8sApiException as exc:
                if exc.status == 404:
                    _logger.debug("K8s %s %s already gone (eval %s)", kind, name, self.id)
                else:
                    _logger.warning(
                        "Failed to delete K8s %s %s (eval %s): %s",
                        kind, name, self.id, exc.reason,
                    )
            except Exception:
                _logger.warning(
                    "Failed to delete K8s %s %s (eval %s)",
                    kind, name, self.id, exc_info=True,
                )

    def _create_evaluation_job(self):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes Python package is not installed on this server.")

        _load_k8s_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()

        db_name = self.env.cr.dbname
        job_uid = uuid.uuid4().hex[:12]
        job_name = f"aurora-eval-{self.id}-{job_uid}"

        ICP = self.env["ir.config_parameter"].sudo()
        docker_image = (
            ICP.get_param("aurora.worker_docker_image")
            or _get_env("AURORA_DOCKER_IMAGE")
            or EVAL_DOCKER_IMAGE
        )

        labels = {
            "app.kubernetes.io/name": "aurora-pipelines",
            "app.kubernetes.io/component": "evaluation-worker",
            "app.kubernetes.io/managed-by": "aurora-odoo",
            "platform": "aurora",
            "evaluation-id": str(self.id),
        }
        if EVAL_KUEUE_QUEUE:
            labels["kueue.x-k8s.io/queue-name"] = EVAL_KUEUE_QUEUE

        secret_name = self._create_eval_secret(core_v1, labels)
        cm_name = self._create_eval_configmap(core_v1, labels)

        env_vars = [
            k8s_client.V1EnvVar(name="EVALUATION_ID", value=str(self.id)),
            k8s_client.V1EnvVar(name="ODOO_DB", value=db_name),
            k8s_client.V1EnvVar(name="PYTHONPATH", value="/opt/odoo:/opt/odoo/custom_addons"),
            k8s_client.V1EnvVar(name="ODOO_CONF", value=EVAL_ODOO_CONF_PATH),
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
            k8s_client.V1EnvVar(name="DOCKER_HOST", value="tcp://localhost:2375"),
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

        if os.environ.get("AURORA_LOCAL_MODE"):
            env_vars.append(k8s_client.V1EnvVar(name="AURORA_LOCAL_MODE", value="1"))

        harness_repo = ICP.get_param("aurora.harness_git_repo", "EtharaAI/multi-swe-bench")
        harness_branch = ICP.get_param("aurora.harness_git_branch", "main")
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_REPO", value=harness_repo))
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_BRANCH", value=harness_branch))

        # ECR push phase configuration. Plain env values come from the
        # operator's AURORA_ECR_* env vars on the Odoo pod; AWS credentials
        # flow via the same secret used for S3 keys (only in local mode).
        # AURORA_REGISTRY_KIND defaults to 'ecr' in the worker if unset, so
        # prod requires zero extra config beyond the host/region — the local
        # opt-out is the only thing that needs an explicit value.
        for name in ("AURORA_ECR_REGISTRY", "AURORA_ECR_REGION", "AURORA_REGISTRY_KIND"):
            val = _get_env(name)
            if val:
                env_vars.append(k8s_client.V1EnvVar(name=name, value=val))
        # AWS credentials for skopeo/awscli inside the worker.
        # IMPORTANT: only project AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
        # into the worker container when static keys are actually configured
        # on the Odoo pod. In production the Pod uses IRSA — setting these
        # env vars (even to empty strings) would override the IRSA web-
        # identity provider and break ECR auth. _create_eval_secret only
        # populates the AWS keys when both are non-empty, so the secret-key-
        # ref-based env-var below is gated on the same condition.
        if _get_env("AURORA_AWS_ACCESS_KEY_ID") and _get_env("AURORA_AWS_SECRET_ACCESS_KEY"):
            for aws_var, secret_key in (
                ("AWS_ACCESS_KEY_ID", "AURORA_AWS_ACCESS_KEY_ID"),
                ("AWS_SECRET_ACCESS_KEY", "AURORA_AWS_SECRET_ACCESS_KEY"),
            ):
                env_vars.append(
                    k8s_client.V1EnvVar(
                        name=aws_var,
                        value_from=k8s_client.V1EnvVarSource(
                            secret_key_ref=k8s_client.V1SecretKeySelector(
                                name=secret_name, key=secret_key,
                            ),
                        ),
                    )
                )
        aws_region = _get_env("AURORA_ECR_REGION") or _get_env("AWS_REGION")
        if aws_region:
            env_vars.append(k8s_client.V1EnvVar(name="AWS_REGION", value=aws_region))
            env_vars.append(k8s_client.V1EnvVar(name="AWS_DEFAULT_REGION", value=aws_region))

        worker_container = k8s_client.V1Container(
            name="evaluation",
            image=docker_image,
            image_pull_policy=EVAL_IMAGE_PULL_POLICY,
            command=["python", EVAL_WORKER_SCRIPT],
            env=env_vars,
            security_context=k8s_client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
                allow_privilege_escalation=False,
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="odoo-config",
                    mount_path=EVAL_ODOO_CONF_PATH,
                    sub_path="odoo.conf",
                    read_only=True,
                ),
                k8s_client.V1VolumeMount(
                    name="workspace",
                    mount_path=EVAL_WORKSPACE_PATH,
                ),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": EVAL_CPU_REQUEST, "memory": EVAL_MEMORY_REQUEST},
                limits={"memory": EVAL_MEMORY_LIMIT} if EVAL_MEMORY_LIMIT else None,
            ),
        )

        # In local mode on ARM64 (Apple Silicon), we must:
        # 1. Switch iptables to legacy mode (Minikube lacks nf_tables kernel modules)
        # 2. Unregister Rosetta (which Docker Desktop registers for amd64 emulation)
        # 3. Register QEMU handlers inside DinD so amd64 image builds work correctly.
        # Rosetta crashes inside nested DinD namespaces, so QEMU must replace it.
        # Production path is unchanged — uses standard DinD entrypoint + binfmt init container.
        dind_command = None
        dind_args = None
        if os.environ.get("AURORA_LOCAL_MODE"):
            dind_command = ["/bin/sh", "-c"]
            dind_args = [
                # Switch to iptables-legacy (nf_tables not available in Minikube)
                "update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null"
                " || ln -sf /sbin/iptables-legacy /sbin/iptables 2>/dev/null"
                " || true;"
                " update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null"
                " || ln -sf /sbin/ip6tables-legacy /sbin/ip6tables 2>/dev/null"
                " || true;"
                # Start dockerd in background with full networking (iptables-legacy works)
                " dockerd"
                " --host=tcp://0.0.0.0:2375"
                " --host=unix:///var/run/docker.sock"
                " --tls=false"
                " --storage-driver=overlay2 &"
                " DOCKERD_PID=$!;"
                # Wait for dockerd to be ready
                " for i in $(seq 1 60); do"
                " docker -H unix:///var/run/docker.sock info >/dev/null 2>&1 && break;"
                " sleep 1; done;"
                " if ! docker -H unix:///var/run/docker.sock info >/dev/null 2>&1; then"
                " echo 'FATAL: dockerd failed to start'; exit 1; fi;"
                # Unregister ALL x86_64/amd64 binfmt handlers (Rosetta + stale QEMU)
                " echo 'Removing existing binfmt handlers...';"
                " for f in x86_64 rosetta rosetta-wrapper qemu-x86_64 i386; do"
                " [ -f /proc/sys/fs/binfmt_misc/$f ] && echo -1 > /proc/sys/fs/binfmt_misc/$f;"
                " done;"
                # Register QEMU via tonistiigi/binfmt (fresh, no conflicts)
                " docker -H unix:///var/run/docker.sock run --rm --privileged"
                " --platform linux/arm64 tonistiigi/binfmt:latest --install amd64"
                " && echo 'QEMU binfmt registered for amd64.'"
                " || { echo 'FATAL: binfmt registration failed'; exit 1; };"
                # Verify the new handler uses QEMU, not Rosetta
                " echo 'Verification:';"
                " cat /proc/sys/fs/binfmt_misc/qemu-x86_64 2>/dev/null"
                " || cat /proc/sys/fs/binfmt_misc/x86_64 2>/dev/null"
                " || echo 'WARNING: no x86_64 handler found';"
                " echo 'DinD ready.';"
                " wait $DOCKERD_PID"
            ]

        dind_container = k8s_client.V1Container(
            name="dind",
            image=EVAL_DIND_IMAGE,
            image_pull_policy="IfNotPresent",
            command=dind_command,
            args=dind_args,
            env=[
                k8s_client.V1EnvVar(name="DOCKER_TLS_CERTDIR", value=""),
            ],
            security_context=k8s_client.V1SecurityContext(privileged=True),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="workspace",
                    mount_path=EVAL_WORKSPACE_PATH,
                ),
                k8s_client.V1VolumeMount(
                    name="dind-storage",
                    mount_path="/var/lib/docker",
                ),
            ],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": EVAL_DIND_CPU_REQUEST, "memory": EVAL_DIND_MEMORY_REQUEST},
                limits={"memory": EVAL_DIND_MEMORY_LIMIT} if EVAL_DIND_MEMORY_LIMIT else None,
            ),
        )

        volumes = [
            k8s_client.V1Volume(
                name="odoo-config",
                config_map=k8s_client.V1ConfigMapVolumeSource(name=cm_name),
            ),
            k8s_client.V1Volume(
                name="workspace",
                empty_dir=k8s_client.V1EmptyDirVolumeSource(
                    size_limit=EVAL_WORKSPACE_SIZE,
                ),
            ),
            k8s_client.V1Volume(
                name="dind-storage",
                empty_dir=k8s_client.V1EmptyDirVolumeSource(
                    size_limit=EVAL_DIND_STORAGE_SIZE,
                ),
            ),
        ]

        # In production (x86_64), binfmt init container registers QEMU at node level
        # for cross-arch builds. In local mode (ARM64 Mac), we skip this because
        # QEMU is registered inside the DinD sidecar (see dind_command above).
        init_containers = []
        if not os.environ.get("AURORA_LOCAL_MODE"):
            binfmt_init_container = k8s_client.V1Container(
                name="binfmt-setup",
                image=EVAL_BINFMT_IMAGE,
                image_pull_policy=EVAL_IMAGE_PULL_POLICY,
                args=["--install", "all"],
                security_context=k8s_client.V1SecurityContext(privileged=True),
            )
            init_containers = [binfmt_init_container]

        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace=EVAL_NAMESPACE,
                labels=labels,
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=600,
                active_deadline_seconds=EVAL_DEADLINE_SECONDS,
                backoff_limit=1,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(
                        labels=labels,
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=EVAL_SERVICE_ACCOUNT,
                        restart_policy="Never",
                        node_selector=EVAL_NODE_SELECTOR,
                        init_containers=init_containers,
                        containers=[worker_container, dind_container],
                        volumes=volumes,
                    ),
                ),
            ),
        )

        batch_v1.create_namespaced_job(namespace=EVAL_NAMESPACE, body=job)
        _logger.info("Created K8s evaluation job: %s (eval_id=%s)", job_name, self.id)
        return job_name

    def action_cancel(self):
        self.ensure_one()
        if self.stage in EVAL_TERMINAL_STATES:
            raise UserError("Cannot cancel a finished evaluation.")
        evaluation_executor.request_cancel(self.id)
        self._delete_evaluation_job()
        self._cleanup_k8s_resources()
        self.write({"stage": "failed"})
        self.message_post(body="Evaluation cancelled by user.")

    def _delete_evaluation_job(self):
        if not K8S_AVAILABLE:
            return
        try:
            _load_k8s_config()
            batch_v1 = k8s_client.BatchV1Api()
            batch_v1.delete_collection_namespaced_job(
                namespace=EVAL_NAMESPACE,
                label_selector=f"evaluation-id={self.id}",
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            _logger.info("Deleted K8s Job for evaluation %s", self.id)
        except Exception:
            _logger.warning(
                "Failed to delete K8s Job for evaluation %s (may already be gone)",
                self.id, exc_info=True,
            )

    def action_reset_to_draft(self):
        self.ensure_one()
        if self.stage not in EVAL_TERMINAL_STATES:
            raise UserError("Only finished/failed evaluations can be reset.")
        self.instance_ids.unlink()
        self.write({
            "stage": "draft",
            "build_status": "idle",
            "run_status": "idle",
            "report_status": "idle",
            "log": False,
            "total_instances": 0,
            "resolved_instances": 0,
            "unresolved_instances": 0,
            "error_instances": 0,
            "final_report_file": False,
            "dataset_jsonl_url": False,
            "patch_file": False,
            "missing_registries": False,
            "base_dockerfile_content": False,
            "base_dockerfile_s3_uri": False,
            "dataset_file": self.pipeline_id.step6_file if self.pipeline_id else False,
            "custom_jsonl_file": False,
            "custom_jsonl_filename": False,
            # Clear tar-decision + ECR state from the previous run, otherwise
            # the worker's poll loop sees a stale 'export'/'skip' decision and
            # short-circuits the new wait window without asking the user.
            "tar_decision": "pending",
            "tar_decision_deadline": False,
            "oci_export_status": "idle",
            "ecr_repository": False,
            "ecr_pushed_count": 0,
            "ecr_manifest_s3_uri": False,
        })

    def action_regenerate_report(self):
        self.ensure_one()
        if self.stage not in EVAL_TERMINAL_STATES:
            raise UserError("Can only regenerate reports for finished or failed evaluations.")
        if not self.output_dir or not self.dataset_file:
            raise UserError("Output directory and dataset file are required.")

        from . import dataset_resolver
        if dataset_resolver.is_remote(self.dataset_file):
            self.dataset_file = dataset_resolver.resolve_to_local(
                self.env, self.dataset_file
            )

        self.write({"report_status": "running"})

        db_name = self.env.cr.dbname
        rec_id = self.id
        output_dir = self.output_dir
        workdir = self.workdir
        dataset_file = self.dataset_file
        max_workers = self.max_workers_run or 4

        def _run_regen():
            regen_cr = None
            try:
                from pathlib import Path
                from ..tools.harness.gen_report import ReportCliArgs

                log_dir = Path(output_dir) / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)

                report_args = ReportCliArgs(
                    mode="evaluation",
                    workdir=Path(workdir) if workdir else None,
                    output_dir=Path(output_dir),
                    specifics=None,
                    skips=None,
                    raw_dataset_files=[dataset_file],
                    dataset_files=[dataset_file],
                    max_workers=max_workers,
                    log_dir=log_dir,
                    log_level="INFO",
                    log_to_console=False,
                )
                report_args.run()

                final_report_path = Path(output_dir) / "final_report.json"
                vals = {"report_status": "done"}
                if final_report_path.exists():
                    fr = json.load(open(final_report_path, "r", encoding="utf-8"))
                    vals.update({
                        "total_instances": fr.get("total_instances", 0),
                        "resolved_instances": fr.get("resolved_instances", 0),
                        "unresolved_instances": fr.get("unresolved_instances", 0),
                        "error_instances": fr.get("error_instances", 0),
                    })
                    from . import artifact_collector, s3_storage
                    s3_config = artifact_collector.load_s3_config()
                    if s3_storage.is_configured(s3_config):
                        s3_folder = (s3_config.get("folder") or "").strip("/")
                        phase = "aurora_phase2"
                        org = self.pipeline_id.github_org or "unknown"
                        repo = self.pipeline_id.github_repo or "unknown"
                        run_number = self.s3_run_number or 1
                        s3_key = s3_storage.build_s3_key(
                            org, repo, run_number,
                            "final_report.json", folder=s3_folder, phase=phase,
                        )
                        try:
                            url = s3_storage.upload_file(s3_config, str(final_report_path), s3_key)
                            vals["final_report_file"] = url
                        except Exception:
                            _logger.warning("Failed to upload final_report.json to S3 during regen", exc_info=True)
                            vals["final_report_file"] = str(final_report_path)
                    else:
                        vals["final_report_file"] = str(final_report_path)

                regen_cr = self.env.registry.cursor()
                from odoo import api, SUPERUSER_ID
                env = api.Environment(regen_cr, SUPERUSER_ID, {})
                env["aurora.evaluation"].browse(rec_id).write(vals)
                env["aurora.evaluation"].browse(rec_id).message_post(
                    body="Reports regenerated successfully.",
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )
                regen_cr.commit()
            except Exception:
                _logger.exception("Failed to regenerate report for eval rec=%s", rec_id)
                try:
                    if regen_cr:
                        regen_cr.rollback()
                    fail_cr = self.env.registry.cursor()
                    from odoo import api, SUPERUSER_ID
                    env = api.Environment(fail_cr, SUPERUSER_ID, {})
                    env["aurora.evaluation"].browse(rec_id).write({"report_status": "failed"})
                    fail_cr.commit()
                    fail_cr.close()
                except Exception:
                    _logger.exception("Failed to record regen failure for eval rec=%s", rec_id)
            finally:
                if regen_cr:
                    try:
                        regen_cr.close()
                    except Exception:
                        pass

        self.env.cr.postcommit.add(_run_regen)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_sync_to_done_repo(self):
        self.ensure_one()
        from .done_repo_sync import get_config, append_done_repo
        try:
            repo_full, branch, token = get_config(self.env)
        except ValueError as exc:
            raise UserError(f"auroraScraping not configured: {exc}")

        seen, appended, skipped, failed = set(), [], [], []
        for inst in self.instance_ids:
            if not inst.org or not inst.repo:
                continue
            key = (inst.org.lower(), inst.repo.lower())
            if key in seen:
                continue
            seen.add(key)
            try:
                if append_done_repo(
                    token, repo_full, branch, inst.org, inst.repo,
                    commit_suffix=f"eval={self.id}",
                ):
                    appended.append(f"{inst.org}/{inst.repo}")
                else:
                    skipped.append(f"{inst.org}/{inst.repo}")
            except Exception as exc:
                failed.append(f"{inst.org}/{inst.repo}: {exc}")

        msg_parts = []
        if appended:
            msg_parts.append(f"Appended: {', '.join(appended)}")
        if skipped:
            msg_parts.append(f"Already present: {', '.join(skipped)}")
        if failed:
            msg_parts.append(f"Failed: {'; '.join(failed)}")
        message = " · ".join(msg_parts) or "No (org, repo) pairs to sync."
        if appended or skipped or failed:
            self.message_post(body=f"auroraScraping sync: {message}")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "auroraScraping sync",
                "message": message,
                "type": "danger" if failed else "success",
                "sticky": bool(failed),
            },
        }

    def action_upload_harness_for_missing(self):
        self.ensure_one()
        if not self.missing_registries:
            raise UserError("No missing harness registries to upload for.")

        first_repo = next(
            (r.strip() for r in self.missing_registries.split(",") if r.strip()),
            "",
        )
        if "/" not in first_repo:
            raise UserError(
                f"Invalid missing registry entry: {first_repo!r}. Expected 'org/repo' format."
            )
        org, repo = first_repo.split("/", 1)

        existing = self.env["aurora.harness.staging"].search(
            [("org", "=", org), ("repo", "=", repo), ("active", "=", True)],
            limit=1,
        )
        if existing:
            return {
                "type": "ir.actions.act_window",
                "res_model": "aurora.harness.staging",
                "res_id": existing.id,
                "view_mode": "form",
                "target": "current",
            }

        return {
            "type": "ir.actions.act_window",
            "name": f"Upload Harness for {org}/{repo}",
            "res_model": "aurora.harness.staging",
            "view_mode": "form",
            "target": "current",
            "context": {
                "default_org": org,
                "default_repo": repo,
                "default_pipeline_id": self.pipeline_id.id if self.pipeline_id else False,
                "default_dataset_file": self.dataset_file or False,
            },
        }

    @api.model
    def _cron_watchdog_stalled_eval(self):
        stale_threshold = fields.Datetime.subtract(
            fields.Datetime.now(), minutes=15
        )
        stalled = self.sudo().search([
            ("stage", "not in", ["draft", "done", "failed"]),
            ("last_heartbeat", "<", stale_threshold),
        ])
        for rec in stalled:
            _logger.warning(
                "Aurora eval watchdog: evaluation %s (id=%s) stalled. Marking failed.",
                rec.name, rec.id,
            )
            evaluation_executor.request_cancel(rec.id)
            rec._cleanup_k8s_resources()
            rec.write({"stage": "failed"})
            rec.message_post(
                body="Evaluation marked as failed by watchdog (no heartbeat for 15+ minutes).",
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

    @api.model
    def _cron_promote_finished_test_eval(self):
        """Mirror terminal test-eval status back to the linked staging row.

        Picks up evals dispatched by aurora.harness.staging.action_test_harness
        (staging_test_id set), once they reach a terminal stage. The worker
        writes eval.stage='done'/'failed' via raw SQL (bypassing the ORM), so
        this cron is how the staging row learns the outcome.

        Success rule: eval.stage='done' AND resolved/total >= 50% -> staging
        test_result='success'. Anything else -> 'failed'. Either way, staging
        stage flips to 'tested'.
        """
        finished = self.sudo().search([
            ("staging_test_id", "!=", False),
            ("stage", "in", list(EVAL_TERMINAL_STATES)),
            ("test_eval_promoted", "=", False),
        ])
        for rec in finished:
            staging = rec.staging_test_id
            if not staging:
                rec.test_eval_promoted = True
                continue
            ratio = 0.0
            if rec.stage == "done" and rec.total_instances:
                ratio = rec.resolved_instances / rec.total_instances
            success = rec.stage == "done" and ratio >= 0.5
            tail = (rec.log or "")[-2000:]
            summary = (
                f"K8s test eval {rec.name} finished: stage={rec.stage}, "
                f"resolved={rec.resolved_instances}/{rec.total_instances} "
                f"(threshold 50%). Result: "
                f"{'success' if success else 'failed'}.\n"
                f"--- eval log tail ---\n{tail}"
            )
            staging.sudo().write({
                "stage": "tested",
                "test_result": "success" if success else "failed",
                "test_log": summary,
            })
            rec.sudo().write({"test_eval_promoted": True})
            _logger.info(
                "Promoted test-eval %s -> staging %s (%s)",
                rec.id, staging.id, "success" if success else "failed",
            )

    def action_request_tar_export(self):
        """User clicked 'Export OCI Tars'. Worker poll picks up within ~5 s."""
        self.ensure_one()
        if self.stage != "awaiting_tar_decision":
            raise UserError(
                "This action is only available while the worker is waiting "
                f"for a tar export decision. Current stage: {self.stage}"
            )
        if self.tar_decision != "pending":
            raise UserError(
                f"Decision already recorded: {self.tar_decision}"
            )
        self.write({"tar_decision": "export"})
        self.message_post(
            body="Tar export requested by user. Worker will push images to ECR.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_skip_tar_export(self):
        """User clicked 'Skip'. Worker poll picks up within ~5 s."""
        self.ensure_one()
        if self.stage != "awaiting_tar_decision":
            raise UserError(
                "This action is only available while the worker is waiting "
                f"for a tar export decision. Current stage: {self.stage}"
            )
        if self.tar_decision != "pending":
            raise UserError(
                f"Decision already recorded: {self.tar_decision}"
            )
        self.write({"tar_decision": "skip"})
        self.message_post(
            body="Tar export skipped by user. No images pushed to ECR.",
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def action_download_ecr_manifest(self):
        """Build a zip with ecr_manifest.json + ecr_pull.sh and serve as download."""
        self.ensure_one()
        if self.oci_export_status != "done" or self.ecr_pushed_count <= 0:
            raise UserError(
                "No ECR images have been pushed for this evaluation yet."
            )

        import base64
        import io
        import json
        import zipfile

        pushed_instances = self.instance_ids.filtered(lambda i: i.ecr_image_uri)
        org = self.pipeline_id.github_org if self.pipeline_id else (
            pushed_instances[:1].org if pushed_instances else ""
        )
        repo = self.pipeline_id.github_repo if self.pipeline_id else (
            pushed_instances[:1].repo if pushed_instances else ""
        )
        registry_host = (self.ecr_repository or "").split("/", 1)[0]
        region = ""
        if ".dkr.ecr." in registry_host:
            region = registry_host.split(".dkr.ecr.")[1].split(".", 1)[0]

        manifest = {
            "evaluation_id": self.id,
            "evaluation_name": self.name or "",
            "pipeline": f"{org}/{repo}" if org and repo else "",
            "exported_at": fields.Datetime.to_string(fields.Datetime.now()),
            "ecr_repository": self.ecr_repository or "",
            "image_count": len(pushed_instances),
            "images": [
                {
                    "instance_id": inst.instance_id or "",
                    "org": inst.org,
                    "repo": inst.repo,
                    "pr_number": inst.pr_numbers or "",
                    "ecr_image_uri": inst.ecr_image_uri,
                    "ecr_image_digest": inst.ecr_image_digest or "",
                    "status": inst.status,
                }
                for inst in pushed_instances
            ],
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

        is_ecr = ".dkr.ecr." in (registry_host or "")
        pull_script_lines = [
            "#!/usr/bin/env bash",
            f"# Registry images from evaluation #{self.id} ({manifest['pipeline']})",
            f"# Generated {manifest['exported_at']}",
            "set -euo pipefail",
            "",
        ]
        if is_ecr and region and registry_host:
            pull_script_lines.extend([
                f"aws ecr get-login-password --region {region} \\",
                f"  | docker login --username AWS --password-stdin {registry_host}",
                "",
            ])
        elif not is_ecr and registry_host:
            pull_script_lines.extend([
                "# Local in-cluster registry — port-forward first if pulling from your laptop:",
                f"# kubectl port-forward -n aurora svc/{registry_host.split(':')[0].split('.')[0]} 5000:5000",
                "# then replace the host in the URIs below with localhost:5000.",
                "",
            ])
        for img in manifest["images"]:
            ref = img["ecr_image_uri"]
            if img["ecr_image_digest"]:
                base_ref = ref.rsplit(":", 1)[0]
                ref = f"{base_ref}@{img['ecr_image_digest']}"
            pull_script_lines.append(f"docker pull {ref}")
        pull_script_lines.append("")
        pull_script_bytes = "\n".join(pull_script_lines).encode("utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ecr_manifest.json", manifest_bytes)
            zf.writestr("ecr_pull.sh", pull_script_bytes)
        attachment = self.env["ir.attachment"].create({
            "name": f"ecr_manifest_eval_{self.id}.zip",
            "type": "binary",
            "datas": base64.b64encode(buf.getvalue()),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/zip",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    @api.model
    def _cron_tar_decision_timeout_sweep(self):
        """Safety net: mark abandoned awaiting_tar_decision evals as failed.

        Fires only when (deadline + 5 min) has passed AND the heartbeat is
        stale (>2 min). Under healthy worker conditions the worker exits the
        wait loop on its own at the deadline, so this cron is a backstop for
        worker process crashes during the wait.
        """
        from datetime import timedelta
        now = fields.Datetime.now()
        stale_hb = fields.Datetime.subtract(now, minutes=2)
        abandoned = self.sudo().search([
            ("stage", "=", "awaiting_tar_decision"),
            ("tar_decision_deadline", "<", now - timedelta(minutes=5)),
            "|", ("last_heartbeat", "<", stale_hb), ("last_heartbeat", "=", False),
        ])
        for rec in abandoned:
            _logger.warning(
                "Tar-decision timeout sweep: eval %s (id=%s) abandoned past "
                "deadline with stale heartbeat. Marking failed.",
                rec.name, rec.id,
            )
            rec.write({
                "stage": "failed",
                "oci_export_status": "failed",
            })
            rec.message_post(
                body="Marked failed by tar-decision watchdog (deadline crossed, "
                     "heartbeat stale for 2+ minutes — worker likely died).",
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
