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
    step_ids = fields.One2many(
        "kaiju.commit0.workflow.step", "run_id", string="Workflow Steps"
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

    # ── Cron: Poll Argo for running runs ─────────────────────────────────────

    @api.model
    def _cron_poll_run_status(self):
        """Called by ir.cron every 60s to update running evaluations from Argo."""
        running_runs = self.search(
            [("run_status", "=", "running"), ("workflow_name", "!=", False)]
        )
        if not running_runs:
            return

        argo = self.env["kaiju.argo.client"]
        for run in running_runs:
            try:
                status = argo.get_workflow_status(run.workflow_name)
            except RuntimeError as e:
                _logger.warning(
                    "Failed to poll run %s (workflow %s): %s",
                    run.name,
                    run.workflow_name,
                    e,
                )
                continue

            phase = status.get("phase", "")
            progress = status.get("progress", "")
            previous_status = run.run_status

            # Sync per-step records on every poll so the UI updates progressively
            try:
                run._sync_steps()
            except Exception as e:
                _logger.warning(
                    "Failed to sync steps for run %s: %s", run.name, e
                )

            if phase in ("Succeeded",):
                run.write(
                    {
                        "run_status": "done",
                        "run_end": fields.Datetime.now(),
                        "run_log": self._append_log(
                            run.run_log, f"Workflow completed. Progress: {progress}"
                        ),
                    }
                )
            elif phase in ("Failed", "Error"):
                message = status.get("message", "Unknown error")
                run.write(
                    {
                        "run_status": "failed",
                        "run_end": fields.Datetime.now(),
                        "run_log": self._append_log(
                            run.run_log, f"Workflow failed: {phase} \u2014 {message}"
                        ),
                    }
                )
            elif phase in ("Running", "Pending", ""):
                run.write(
                    {
                        "run_log": self._append_log(
                            run.run_log, f"Status: {phase or 'Pending'} ({progress})"
                        ),
                    }
                )

            # On running → terminal transition, persist all step logs once
            if (
                previous_status == "running"
                and run.run_status in ("done", "failed")
            ):
                try:
                    run._persist_step_logs()
                except Exception as e:
                    _logger.warning(
                        "Failed to persist step logs for run %s: %s",
                        run.name,
                        e,
                    )

    # ── Step Sync & Log Persistence ─────────────────────────────────

    def _sync_steps(self):
        """Fetch workflow nodes from Argo and upsert step records."""
        self.ensure_one()
        if not self.workflow_name:
            return
        argo = self.env["kaiju.argo.client"]
        nodes = argo.list_workflow_nodes(self.workflow_name)
        if not nodes:
            return
        Step = self.env["kaiju.commit0.workflow.step"]
        # Lazy import to avoid circular reference at module load
        from .kaiju_commit0 import _parse_argo_dt
        existing = {s.node_id: s for s in self.step_ids}
        for node in nodes:
            node_id = node.get("id") or ""
            if not node_id:
                continue
            vals = {
                "display_name": node.get("displayName") or node.get("name") or node_id,
                "pod_name": node_id,
                "template_name": node.get("templateName") or "",
                "node_type": node.get("type") or "Pod",
                "phase": node.get("phase") or "Pending",
                "message": node.get("message") or "",
                "started_at": _parse_argo_dt(node.get("startedAt")),
                "finished_at": _parse_argo_dt(node.get("finishedAt")),
            }
            if node_id in existing:
                existing[node_id].write(vals)
            else:
                vals["node_id"] = node_id
                vals["run_id"] = self.id
                Step.create(vals)

    def _persist_step_logs(self):
        """Fetch and persist logs for every Pod-type step. Called once on
        running → terminal transition so logs survive Argo pod GC."""
        self.ensure_one()
        for step in self.step_ids:
            if step.node_type != "Pod" or not step.pod_name:
                continue
            step.action_fetch_logs()

    def action_fetch_all_step_logs(self):
        """Manual trigger: sync step list from Argo, then fetch logs for each step."""
        self.ensure_one()
        if not self.workflow_name:
            from odoo.exceptions import UserError
            raise UserError("No workflow has been submitted for this run yet.")
        try:
            self._sync_steps()
        except Exception as e:
            _logger.warning("action_fetch_all_step_logs: _sync_steps failed: %s", e)
        self.step_ids.action_fetch_logs()
        return {"type": "ir.actions.client", "tag": "reload"}

    @staticmethod
    def _append_log(existing_log, new_line):
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {new_line}"
        if existing_log:
            return f"{existing_log}\n{entry}"
        return entry
