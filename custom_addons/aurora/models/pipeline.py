import logging
import os
import re
import threading
import uuid

from github import Auth, Github, GithubException

from odoo import api, fields, models
from odoo.exceptions import UserError

from .pipeline_config import GITHUB_LANG_MAP, LANGUAGE_SELECTION
from .credential_manager import get_encrypted_param

_logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


_k8s_config_lock = threading.Lock()
_k8s_config_loaded = False


def _load_k8s_config():
    global _k8s_config_loaded
    if _k8s_config_loaded:
        return
    with _k8s_config_lock:
        # Double-check after acquiring lock
        if _k8s_config_loaded:
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        _k8s_config_loaded = True

_SAFE_GITHUB_NAME = re.compile(r'^[a-zA-Z0-9._-]+$')

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
            "cache_dir": ICP.get_param("aurora.cache_dir", "/data/repo_cache"),
            "delay_on_error": int(ICP.get_param("aurora.delay_on_error", "300")),
            "retry_attempts": int(ICP.get_param("aurora.retry_attempts", "3")),
            "max_tags": int(ICP.get_param("aurora.max_tags", "200")),
            "window_days": int(ICP.get_param("aurora.window_days", "30")),
            "lang": ICP.get_param("aurora.lang", "python"),
            "s3_bucket": ICP.get_param("aurora.s3_bucket", ""),
            "s3_access_key": get_encrypted_param(self.env, "aurora.s3_access_key"),
            "s3_secret_key": get_encrypted_param(self.env, "aurora.s3_secret_key"),
            "s3_region": ICP.get_param("aurora.s3_region", "ap-south-1"),
            "s3_folder": ICP.get_param("aurora.s3_folder", ""),
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

    def _get_k8s_setting(self, key, default=""):
        return self.env["ir.config_parameter"].sudo().get_param(
            f"aurora.k8s_{key}", default,
        )

    def _create_pipeline_job(self):
        """Create a K8s Job to run this pipeline in an isolated pod."""
        if not K8S_AVAILABLE:
            raise UserError("kubernetes Python package is not installed on this server.")

        namespace = self._get_k8s_setting("namespace", "ethara")
        image = self._get_k8s_setting("image")
        service_account = self._get_k8s_setting("service_account", "aurora-worker")
        node_pool = self._get_k8s_setting("node_pool", "")
        kueue_queue = self._get_k8s_setting("kueue_queue", "aurora-pipelines")
        db_name = self.env.cr.dbname
        job_uid = uuid.uuid4().hex[:12]
        job_name = f"aurora-pipeline-{self.id}-{job_uid}"

        if not image:
            raise UserError(
                "K8s worker image not configured. "
                "Set it in Settings → Aurora Pipeline → K8s Docker Image."
            )

        worker_script = self._get_k8s_setting(
            "worker_script",
            "/opt/odoo/custom_addons/aurora/worker/run_pipeline.py",
        )

        _load_k8s_config()
        batch_v1 = k8s_client.BatchV1Api()

        env_vars = [
            k8s_client.V1EnvVar(name="PIPELINE_ID", value=str(self.id)),
            k8s_client.V1EnvVar(name="ODOO_DB", value=db_name),
        ]

        odoo_conf = self._get_k8s_setting("odoo_conf", "")
        if odoo_conf:
            env_vars.append(k8s_client.V1EnvVar(name="ODOO_CONF", value=odoo_conf))

        secret_name = self._get_k8s_setting("secret", "aurora-secrets")
        if secret_name:
            for secret_key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "AURORA_ENCRYPTION_KEY"):
                env_vars.append(
                    k8s_client.V1EnvVar(
                        name=secret_key,
                        value_from=k8s_client.V1EnvVarSource(
                            secret_key_ref=k8s_client.V1SecretKeySelector(
                                name=secret_name,
                                key=secret_key,
                                optional=True,
                            ),
                        ),
                    ),
                )

        volume_mounts = []
        volumes = []

        configmap_name = self._get_k8s_setting("configmap", "aurora-worker-config")
        if configmap_name:
            volumes.append(
                k8s_client.V1Volume(
                    name="odoo-config",
                    config_map=k8s_client.V1ConfigMapVolumeSource(
                        name=configmap_name,
                    ),
                ),
            )
            volume_mounts.append(
                k8s_client.V1VolumeMount(
                    name="odoo-config",
                    mount_path="/etc/odoo",
                    read_only=True,
                ),
            )

        efs_pvc = self._get_k8s_setting("efs_pvc", "")
        if efs_pvc:
            volumes.append(
                k8s_client.V1Volume(
                    name="repo-cache",
                    persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=efs_pvc,
                    ),
                ),
            )
            volume_mounts.append(
                k8s_client.V1VolumeMount(
                    name="repo-cache",
                    mount_path="/data/repo_cache",
                ),
            )

        labels = {
            "app.kubernetes.io/name": "aurora-pipeline",
            "app.kubernetes.io/component": "pipeline-worker",
            "platform": "aurora",
            "pipeline-id": str(self.id),
            "kueue.x-k8s.io/queue-name": kueue_queue,
        }

        pull_policy = "IfNotPresent" if ":" in image and image.rsplit(":", 1)[-1] == "local" else "Always"

        container = k8s_client.V1Container(
            name="pipeline",
            image=image,
            image_pull_policy=pull_policy,
            command=["python", worker_script],
            env=env_vars,
            volume_mounts=volume_mounts or None,
            resources=k8s_client.V1ResourceRequirements(
                requests={
                    "cpu": self._get_k8s_setting("cpu_request", "1"),
                    "memory": self._get_k8s_setting("memory_request", "2Gi"),
                },
                limits={
                    "memory": self._get_k8s_setting("memory_limit", "4Gi"),
                },
            ),
        )

        # activeDeadlineSeconds: hard kill after 4 hours to prevent runaway pods
        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels=labels,
            ),
            spec=k8s_client.V1JobSpec(
                ttl_seconds_after_finished=600,
                active_deadline_seconds=int(self._get_k8s_setting("deadline_seconds", "14400")),
                backoff_limit=0,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(labels=labels),
                    spec=k8s_client.V1PodSpec(
                        service_account_name=service_account,
                        restart_policy="Never",
                        node_selector={"ethara.ai/node-pool": node_pool} if node_pool else None,
                        containers=[container],
                        volumes=volumes or None,
                    ),
                ),
            ),
        )

        batch_v1.create_namespaced_job(namespace=namespace, body=job)
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
            "access_key": config.get("s3_access_key", ""),
            "secret_key": config.get("s3_secret_key", ""),
            "region": config.get("s3_region", "ap-south-1"),
            "folder": config.get("s3_folder", ""),
        }
        use_s3 = s3_storage.is_configured(s3_config)

        if use_s3:
            try:
                s3_storage.validate_credentials(s3_config)
            except Exception as exc:
                raise UserError(
                    f"S3 credential validation failed: {exc}\n"
                    "Check your S3 bucket, access key, secret key, and region in Settings."
                ) from exc
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

        self.write({"output_dir": out, "stage": "fetch_prs", "detected_lang": lang})

        k8s_image = self._get_k8s_setting("image") if K8S_AVAILABLE else ""
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

        self.env.cr.commit()

        def _worker():
            import time
            time.sleep(1)
            from ..worker.run_pipeline import run_pipeline
            _logger.info("Pipeline %s (id=%s) starting local execution", rec_name, rec_id)
            run_pipeline(registry, db_name, rec_id)

        t = threading.Thread(target=_worker, name=f"aurora-local-{rec_id}", daemon=True)
        t.start()
        _logger.info("Pipeline %s (id=%s) launched in local thread", rec_name, rec_id)

    def action_cancel(self):
        self.ensure_one()
        if self.stage in TERMINAL_STATES:
            raise UserError("Cannot cancel a finished pipeline.")

        if self.job_name and K8S_AVAILABLE:
            try:
                namespace = self._get_k8s_setting("namespace", "ethara")
                _load_k8s_config()
                batch_v1 = k8s_client.BatchV1Api()
                batch_v1.delete_namespaced_job(
                    name=self.job_name,
                    namespace=namespace,
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
            import base64
            with open(file_url, "rb") as f:
                data = base64.b64encode(f.read())
            fname = os.path.basename(file_url)
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
        local_path = file_url[7:] if file_url.startswith("file://") else file_url
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

        from .registry_wizard import _TEMPLATE, _to_class_name
        class_name = _to_class_name(repo)
        content = _TEMPLATE.format(
            class_name=class_name, org=org, repo=repo,
        )

        RegistryWiz = self.env["aurora.registry.wizard"]
        repo_safe = repo.replace("-", "_").lower()
        wiz = RegistryWiz.create({
            "pipeline_id": self.id,
            "org": org,
            "repo": repo,
            "lang": lang,
            "filename": f"{repo_safe}.py",
            "registry_content": content,
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

        from .registry_wizard import _HARNESS_REPOS_ROOT

        repo_safe = repo.replace("-", "_").lower()
        registry_file = _HARNESS_REPOS_ROOT / lang / org / f"{repo_safe}.py"

        if not registry_file.exists():
            raise UserError(
                f"Registry file not found at:\n{registry_file}\n\n"
                "Use 'Create Instance Registry' to generate one first."
            )

        content = registry_file.read_text()

        RegistryWiz = self.env["aurora.registry.wizard"]
        wiz = RegistryWiz.create({
            "pipeline_id": self.id,
            "org": org,
            "repo": repo,
            "lang": lang,
            "filename": f"{repo_safe}.py",
            "registry_content": content,
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

        namespace = self.env["ir.config_parameter"].sudo().get_param(
            "aurora.k8s_namespace", "ethara",
        )

        try:
            _load_k8s_config()
            batch_v1 = k8s_client.BatchV1Api()
            jobs = batch_v1.list_namespaced_job(
                namespace=namespace,
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
                            deadline = int(self.env["ir.config_parameter"].sudo().get_param(
                                "aurora.k8s_deadline_seconds", "14400",
                            ))
                            hours = deadline / 3600
                            reason = (
                                f"Pipeline timed out after {hours:.0f} hours "
                                f"(K8s activeDeadlineSeconds={deadline}). "
                                "Increase the deadline in Settings or optimize the pipeline."
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
                    namespace = self.env["ir.config_parameter"].sudo().get_param(
                        "aurora.k8s_namespace", "ethara",
                    )
                    _load_k8s_config()
                    batch_v1 = k8s_client.BatchV1Api()
                    batch_v1.delete_namespaced_job(
                        name=rec.job_name,
                        namespace=namespace,
                        body=k8s_client.V1DeleteOptions(
                            propagation_policy="Foreground",
                        ),
                    )
                except Exception:
                    _logger.warning("Failed to delete stalled Job %s", rec.job_name, exc_info=True)

            rec.write({"stage": "failed"})
            rec.message_post(
                body="Pipeline marked as failed by watchdog (no heartbeat for 10+ minutes).",
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
