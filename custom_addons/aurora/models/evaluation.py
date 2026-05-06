import json
import logging
import os
import threading
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError
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

# ---------------------------------------------------------------------------
# Phase 2 infrastructure constants (DinD-enabled pods for Docker builds).
# ---------------------------------------------------------------------------
EVAL_NAMESPACE = "aurora"
EVAL_NODE_SELECTOR = {"ethara.ai/node-pool": "general-purpose"}
EVAL_SERVICE_ACCOUNT = "aurora-worker"
EVAL_KUEUE_QUEUE = "aurora-evaluations"
EVAL_DOCKER_IMAGE = "426628337772.dkr.ecr.ap-south-1.amazonaws.com/aurora-worker:latest"
EVAL_DIND_IMAGE = "docker:27-dind"
EVAL_CPU_REQUEST = "2"
EVAL_MEMORY_REQUEST = "4Gi"
EVAL_MEMORY_LIMIT = "8Gi"
EVAL_DIND_CPU_REQUEST = "2"
EVAL_DIND_MEMORY_REQUEST = "4Gi"
EVAL_DIND_MEMORY_LIMIT = "8Gi"
EVAL_DEADLINE_SECONDS = 28800  # 8 hours
EVAL_WORKER_SCRIPT = "/opt/odoo/custom_addons/aurora/worker/run_evaluation.py"
EVAL_ODOO_CONF_PATH = "/etc/odoo/odoo.conf"
EVAL_WORKSPACE_PATH = "/tmp/aurora_output"


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _load_k8s_config():
    global _k8s_config_loaded
    if _k8s_config_loaded:
        return
    with _k8s_config_lock:
        if _k8s_config_loaded:
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        _k8s_config_loaded = True

EVAL_STAGE_SELECTION = [
    ("draft", "Draft"),
    ("building_images", "Building Images"),
    ("running_instances", "Running Instances"),
    ("generating_reports", "Generating Reports"),
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
        help='e.g. "linux/amd64". Leave empty for default.',
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
    missing_registries = fields.Text(
        string="Missing Harness Registries",
        readonly=True,
        help="Repos in the dataset that have no harness implementation.",
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

    def _generate_patch_file(self, dataset_path, output_path):
        with open(dataset_path, "r", encoding="utf-8") as f_in, \
             open(output_path, "w", encoding="utf-8") as f_out:
            for line in f_in:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                number = self._resolve_entry_number(entry)
                if number is None:
                    continue
                patch_entry = {
                    "org": entry["org"],
                    "repo": entry["repo"],
                    "number": number,
                    "fix_patch": entry.get("fix_patch", ""),
                }
                f_out.write(json.dumps(patch_entry, ensure_ascii=False) + "\n")

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
            patch_path = os.path.join(self.output_dir, "patches.jsonl")
            self._generate_patch_file(self.dataset_file, patch_path)
            self.patch_file = patch_path

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

    def _create_evaluation_job(self):
        if not K8S_AVAILABLE:
            raise UserError("kubernetes Python package is not installed on this server.")

        _load_k8s_config()
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()

        db_name = self.env.cr.dbname
        job_uid = uuid.uuid4().hex[:12]
        job_name = f"aurora-eval-{self.id}-{job_uid}"

        docker_image = _get_env("AURORA_DOCKER_IMAGE", EVAL_DOCKER_IMAGE)

        env_vars = [
            k8s_client.V1EnvVar(name="EVALUATION_ID", value=str(self.id)),
            k8s_client.V1EnvVar(name="ODOO_DB", value=db_name),
            k8s_client.V1EnvVar(name="PYTHONPATH", value="/opt/odoo:/opt/odoo/custom_addons"),
            k8s_client.V1EnvVar(name="ODOO_CONF", value=EVAL_ODOO_CONF_PATH),
            k8s_client.V1EnvVar(name="DB_HOST", value=odoo_config["db_host"]),
            k8s_client.V1EnvVar(name="DB_PORT", value=str(odoo_config["db_port"] or "5432")),
            k8s_client.V1EnvVar(name="DB_USER", value=odoo_config["db_user"]),
            k8s_client.V1EnvVar(name="DB_PASSWORD", value=odoo_config["db_password"]),
            k8s_client.V1EnvVar(name="AURORA_ENCRYPTION_KEY", value=_get_env("AURORA_ENCRYPTION_KEY")),
            k8s_client.V1EnvVar(name="DOCKER_HOST", value="tcp://localhost:2375"),
        ]

        ICP = self.env["ir.config_parameter"].sudo()
        harness_repo = ICP.get_param("aurora.harness_git_repo", "EtharaAI/multi-swe-bench")
        harness_branch = ICP.get_param("aurora.harness_git_branch", "main")
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_REPO", value=harness_repo))
        env_vars.append(k8s_client.V1EnvVar(name="AURORA_HARNESS_GIT_BRANCH", value=harness_branch))

        labels = {
            "app.kubernetes.io/name": "aurora-evaluation",
            "app.kubernetes.io/component": "evaluation-worker",
            "app.kubernetes.io/managed-by": "aurora-odoo",
            "platform": "aurora",
            "evaluation-id": str(self.id),
            "kueue.x-k8s.io/queue-name": EVAL_KUEUE_QUEUE,
        }

        cm_name = self._create_eval_configmap(core_v1, labels)

        worker_container = k8s_client.V1Container(
            name="evaluation",
            image=docker_image,
            image_pull_policy="Always",
            command=["python", EVAL_WORKER_SCRIPT],
            env=env_vars,
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
                limits={"memory": EVAL_MEMORY_LIMIT},
            ),
        )

        dind_container = k8s_client.V1Container(
            name="dind",
            image=EVAL_DIND_IMAGE,
            image_pull_policy="IfNotPresent",
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
                limits={"memory": EVAL_DIND_MEMORY_LIMIT},
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
                    size_limit="100Gi",
                ),
            ),
            k8s_client.V1Volume(
                name="dind-storage",
                empty_dir=k8s_client.V1EmptyDirVolumeSource(
                    size_limit="100Gi",
                ),
            ),
        ]

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
                backoff_limit=0,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(labels=labels),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=EVAL_SERVICE_ACCOUNT,
                        restart_policy="Never",
                        node_selector=EVAL_NODE_SELECTOR,
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
        self.write({"stage": "failed"})
        self.message_post(body="Evaluation cancelled by user.")

    def action_reset_to_draft(self):
        self.ensure_one()
        if self.stage not in EVAL_TERMINAL_STATES:
            raise UserError("Only finished/failed evaluations can be reset.")
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
            "patch_file": False,
            "missing_registries": False,
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
                        "final_report_file": str(final_report_path),
                    })

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
            rec.write({"stage": "failed"})
            rec.message_post(
                body="Evaluation marked as failed by watchdog (no heartbeat for 15+ minutes).",
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
