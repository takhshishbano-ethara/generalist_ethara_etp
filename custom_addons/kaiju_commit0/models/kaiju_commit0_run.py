# -*- coding: utf-8 -*-
import logging


from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

RUN_WORKFLOW_TEMPLATE = "kaiju-run-pipeline"


class KaijuCommit0Run(models.Model):
    _name = "kaiju.commit0.run"
    _description = "Commit0 Evaluation Run"
    _order = "create_date desc"

    name = fields.Char(string="Run ID", readonly=True, copy=False, default="New")
    build_id = fields.Many2one(
        "kaiju.commit0", string="Build", required=True, ondelete="cascade"
    )

    # ── Run Configuration ────────────────────────────────────────────────────

    model_name = fields.Selection(
        [
            ("glm_5", "GLM-5"),
            ("nova2_lite", "Nova2-Lite"),
            ("opus_4_7", "Opus-4.7"),
        ],
        string="Model",
        required=True,
        default="glm_5",
    )
    num_samples = fields.Integer(
        string="Samples",
        default=1,
        help="Number of trajectory samples per test",
    )
    max_iteration = fields.Integer(
        string="Max Iterations",
        default=3,
        help="Maximum draft-lint-test iterations",
    )
    use_spec_info = fields.Boolean(
        string="Use Spec Info",
        default=True,
        help="Include specification in LLM context",
    )

    # ── Run Status ───────────────────────────────────────────────────────────

    run_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="pending",
    )
    run_start = fields.Datetime(string="Run Start", readonly=True)
    run_end = fields.Datetime(string="Run End", readonly=True)
    run_log = fields.Text(string="Run Log")
    workflow_name = fields.Char(string="Run Workflow", readonly=True)
    s3_log_prefix = fields.Char(
        string="S3 Log Prefix",
        readonly=True,
        help="S3 key prefix where workflow step logs are stored "
        "(e.g. kaiju_logs/RepoName/123/). Set by the Argo exit-hook callback.",
    )
    step_ids = fields.One2many(
        "kaiju.commit0.workflow.step", "run_id", string="Workflow Steps"
    )

    # ── Log summary (computed from step_ids) ────────────────────────

    error_summary = fields.Char(
        string="Error Summary",
        compute="_compute_error_summary",
        store=False,
        help="Truncated error from first failed step — quick triage in list view",
    )
    combined_log_text = fields.Text(
        string="Combined Logs",
        compute="_compute_combined_log_text",
        store=False,
    )
    step_count = fields.Integer(
        string="Step Count",
        compute="_compute_step_count",
        store=False,
    )

    # ── Metrics ──────────────────────────────────────────────────────────────

    pass_rate = fields.Float(string="Pass Rate (%)", digits=(5, 1), readonly=True)
    tests_passed = fields.Integer(string="Tests Passed", readonly=True)
    tests_failed = fields.Integer(string="Tests Failed", readonly=True)
    tests_total = fields.Integer(string="Total Tests", readonly=True)
    duration_seconds = fields.Float(string="Duration (s)", digits=(6, 1), readonly=True)
    cost_usd = fields.Float(string="Cost (USD)", digits=(8, 4), readonly=True)
    tokens_input = fields.Integer(string="Input Tokens", readonly=True)
    tokens_output = fields.Integer(string="Output Tokens", readonly=True)

    # ── Related (for display) ────────────────────────────────────────────────

    repo_name = fields.Char(related="build_id.repo_name", store=False)
    language = fields.Selection(related="build_id.language", store=False)
    image_uri = fields.Char(related="build_id.image_uri", store=False)

    # ── Computed ─────────────────────────────────────────────────────────────

    is_running = fields.Boolean(compute="_compute_is_running", store=False)

    @api.depends("run_status")
    def _compute_is_running(self):
        for rec in self:
            rec.is_running = rec.run_status == "running"

    @api.depends("step_ids.phase", "step_ids.message", "step_ids.display_name")
    def _compute_error_summary(self):
        for rec in self:
            failed = rec.step_ids.filtered(
                lambda s: s.phase in ("Failed", "Error")
            ).sorted("started_at")
            if failed:
                first = failed[0]
                step_label = first.display_name or first.node_id or "unknown"
                msg = (first.message or "(no error message)").strip()
                if len(msg) > 200:
                    msg = msg[:197] + "…"
                rec.error_summary = f"{step_label}: {msg}"
            else:
                rec.error_summary = False

    @api.depends(
        "step_ids.log_text",
        "step_ids.display_name",
        "step_ids.phase",
        "step_ids.started_at",
        "step_ids.finished_at",
    )
    def _compute_combined_log_text(self):
        for rec in self:
            steps = rec.step_ids.sorted("started_at")
            if not steps:
                rec.combined_log_text = False
                continue
            parts = []
            for i, step in enumerate(steps, start=1):
                header_bits = [
                    f"[{i}/{len(steps)}]",
                    step.display_name or step.node_id or "step",
                    f"({step.phase or 'Pending'})",
                ]
                if step.started_at:
                    header_bits.append(
                        f"started {step.started_at.strftime('%H:%M:%S')}"
                    )
                if step.finished_at:
                    header_bits.append(
                        f"finished {step.finished_at.strftime('%H:%M:%S')}"
                    )
                header = " ".join(header_bits)
                separator = (
                    "─" * 6 + " " + header + " " + "─" * max(6, 100 - len(header) - 14)
                )
                body = step.log_text or "(no log captured)"
                parts.append(f"{separator}\n{body}")
            rec.combined_log_text = "\n\n".join(parts)

    @api.depends("step_ids")
    def _compute_step_count(self):
        for rec in self:
            rec.step_count = len(rec.step_ids)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("kaiju.commit0.run") or "New"
                )
        return super().create(vals_list)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_run(self):
        """Submit run workflow to Argo Server (stages + eval + finalize)."""
        self.ensure_one()
        build = self.build_id
        if not build.image_uri:
            raise UserError("Build must be complete with a valid image before running.")
        if self.run_status == "done":
            raise UserError("Run already complete.")
        if self.run_status == "running":
            raise UserError("Run is already in progress.")

        argo = self.env["kaiju.argo.client"]
        repo_short = (build.repo_name or "unknown").split("/")[-1]

        callback_url = self._get_run_callback_url()

        # Map model_name to model_preset expected by the pipeline
        model_preset_map = {
            "glm_5": "glm-5",
            "nova2_lite": "nova2-lite",
            "opus_4_7": "opus-4.7",
        }

        parameters = {
            "repo_name": build.repo_name,
            "language": build.language,
            "branch_name": "commit0_combined",  # constant; pipeline hardcodes commit0_all internally
            "model_preset": model_preset_map.get(self.model_name, self.model_name),
            "num_samples": str(self.num_samples),
            "max_iteration": str(self.max_iteration),
            "use_spec_info": "true" if self.use_spec_info else "false",
            "odoo_job_id": str(self.id),
            "callback_url": callback_url,
            "kaiju_agent_image": build.image_uri,
        }

        try:
            workflow_name = argo.submit_workflow(
                RUN_WORKFLOW_TEMPLATE,
                parameters,
                labels={"kaiju/run-id": self.name, "kaiju/repo": repo_short},
            )
        except RuntimeError as e:
            _logger.error("Failed to submit run workflow for %s: %s", self.name, e)
            raise UserError(f"Failed to submit run workflow: {e}") from e

        self.write(
            {
                "run_status": "running",
                "run_start": fields.Datetime.now(),
                "workflow_name": workflow_name,
                "run_log": f"Workflow submitted: {workflow_name}\nWaiting for pipeline...",
            }
        )

    def _get_run_callback_url(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = ICP.get_param(
            "kaiju.odoo_internal_url", "http://odoo-web.odoo.svc:8069"
        )
        return f"{base_url}/kaiju/callback/run"

    def action_abort(self):
        """Stop running workflow via Argo Server."""
        self.ensure_one()
        if self.run_status != "running":
            raise UserError("Run is not currently in progress.")

        if self.workflow_name:
            argo = self.env["kaiju.argo.client"]
            try:
                argo.stop_workflow(
                    self.workflow_name, f"Aborted from Odoo ({self.name})"
                )
            except RuntimeError as e:
                _logger.warning("Failed to stop workflow %s: %s", self.workflow_name, e)

        self.write(
            {
                "run_status": "failed",
                "run_end": fields.Datetime.now(),
            }
        )

    # ── S3 Log Sync (exit-hook based) ───────────────────────────────

    def action_sync_logs(self):
        """Download step log files from S3 for one or more runs.

        Works on single record (form button) and multi-record (list
        Action menu).  Silently skips records without *s3_log_prefix*.
        """
        eligible = self.filtered(lambda r: r.s3_log_prefix)
        if not eligible:
            from odoo.exceptions import UserError

            raise UserError(
                "None of the selected runs have an S3 log prefix yet. "
                "Logs are uploaded by the Argo exit hook on workflow completion."
            )
        for rec in eligible:
            rec._fetch_logs_from_s3()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_sync_all_logs(self):
        """Sync logs for every terminal run that has an S3 log prefix.

        Intended as a bulk convenience action from the list-view header.
        """
        eligible = self.search(
            [
                ("s3_log_prefix", "!=", False),
                ("run_status", "in", ["done", "failed"]),
            ]
        )
        if not eligible:
            from odoo.exceptions import UserError

            raise UserError("No completed runs need log syncing.")
        for rec in eligible:
            rec._fetch_logs_from_s3()
        return {"type": "ir.actions.client", "tag": "reload"}

    def _parse_s3_log_location(self):
        from odoo.exceptions import UserError

        raw = (self.s3_log_prefix or "").strip()
        if not raw:
            raise UserError("No S3 log prefix set on this record.")

        ICP = self.env["ir.config_parameter"].sudo()

        if raw.startswith("s3://"):
            rest = raw[5:]
            if "/" not in rest:
                raise UserError(f"Malformed S3 log prefix: {raw}")
            bucket, prefix = rest.split("/", 1)
        else:
            bucket = ICP.get_param("kaiju.s3_log_bucket", "production-grtlabs-tag")
            prefix = raw

        if prefix and not prefix.endswith("/"):
            prefix += "/"

        region = ICP.get_param("kaiju.aws_region", "ap-south-1")
        access_key = ICP.get_param("kaiju.aws_access_key_id", "") or None
        secret_key = ICP.get_param("kaiju.aws_secret_access_key", "") or None

        return bucket, prefix, region, access_key, secret_key

    def _fetch_logs_from_s3(self):
        from odoo.exceptions import UserError

        self.ensure_one()
        bucket, prefix, region, access_key, secret_key = self._parse_s3_log_location()

        _logger.info(
            "Sync logs for %s: bucket=%s prefix=%s",
            self.name,
            bucket,
            prefix,
        )

        try:
            import boto3
        except ImportError:
            raise UserError("boto3 is not installed in the Odoo Python environment.")

        client_kwargs = {"region_name": region}
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        try:
            s3 = boto3.client("s3", **client_kwargs)
        except Exception as e:
            raise UserError(f"Failed to create S3 client: {e}")

        if not self.step_ids:
            self._discover_steps_from_manifest(s3, bucket, prefix)

        steps_with_logfile = [s for s in self.step_ids if s.log_file]
        if not steps_with_logfile:
            raise UserError(
                f"No steps with log files found.\n\n"
                f"S3 prefix: s3://{bucket}/{prefix}\n"
                f"Total steps: {len(self.step_ids)}\n"
                f"Steps with log_file: 0\n\n"
                f"Check that the Argo exit hook sent steps[] with log_file "
                f"in the callback payload."
            )

        fetched = 0
        failed = 0
        errors = []
        for step in steps_with_logfile:
            key = prefix + step.log_file
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                log_content = obj["Body"].read().decode("utf-8", errors="replace")
                step.write(
                    {
                        "log_text": log_content,
                        "log_fetched_at": fields.Datetime.now(),
                    }
                )
                fetched += 1
            except Exception as e:
                _logger.warning(
                    "S3 log fetch failed for step %s (key=s3://%s/%s): %s",
                    step.display_name or step.node_id,
                    bucket,
                    key,
                    e,
                )
                errors.append(f"  {step.display_name or step.node_id}: {key} → {e}")
                failed += 1

        _logger.info(
            "S3 log sync for %s: fetched=%d failed=%d total_steps=%d",
            self.name,
            fetched,
            failed,
            len(self.step_ids),
        )

        if fetched == 0 and failed > 0:
            raise UserError(
                f"All {failed} log downloads failed:\n\n" + "\n".join(errors)
            )

    def _discover_steps_from_manifest(self, s3, bucket, prefix):
        """Read manifest.json from S3 and create step records.

        Used as a fallback when the callback's ``steps[]`` payload was
        lost (network error, Odoo downtime, etc.).
        """
        import json

        manifest_key = prefix + "manifest.json"
        try:
            obj = s3.get_object(Bucket=bucket, Key=manifest_key)
            manifest = json.loads(obj["Body"].read())
        except Exception as e:
            _logger.info(
                "No manifest.json for %s at s3://%s/%s: %s",
                self.name,
                bucket,
                manifest_key,
                e,
            )
            return

        steps_data = manifest.get("steps", [])
        if not steps_data:
            return

        Step = self.env["kaiju.commit0.workflow.step"]
        for entry in steps_data:
            order = entry.get("order", 0)
            Step.create(
                {
                    "run_id": self.id,
                    "node_id": f"manifest-{order}",
                    "display_name": entry.get("name", f"step-{order}"),
                    "phase": entry.get("phase", "Succeeded"),
                    "log_file": entry.get("log_file", ""),
                    "step_order": order,
                    "node_type": "Pod",
                }
            )

    @staticmethod
    def _append_log(existing_log, new_line):
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {new_line}"
        if existing_log:
            return f"{existing_log}\n{entry}"
        return entry
