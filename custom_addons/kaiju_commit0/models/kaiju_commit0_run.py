# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

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

    @api.depends("step_ids.log_text", "step_ids.display_name",
                 "step_ids.phase", "step_ids.started_at", "step_ids.finished_at")
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
                    header_bits.append(f"started {step.started_at.strftime('%H:%M:%S')}")
                if step.finished_at:
                    header_bits.append(f"finished {step.finished_at.strftime('%H:%M:%S')}")
                header = " ".join(header_bits)
                separator = "─" * 6 + " " + header + " " + "─" * max(
                    6, 100 - len(header) - 14
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

    # ── Cron: Poll Argo for running runs ─────────────────────────────────────

    POLL_RUNNING_LIMIT = 20
    POLL_TERMINAL_INCOMPLETE_LIMIT = 10

    @api.model
    def _cron_poll_run_status(self):
        """Called by ir.cron every 60s to update running evaluations from Argo.

        Bounded per tick (POLL_RUNNING_LIMIT + POLL_TERMINAL_INCOMPLETE_LIMIT)
        so a large backlog can't stack ticks. Ordered by run_end asc so the
        runs closest to Argo pod GC (5 min) are polled first.
        """
        start_ts = fields.Datetime.now()
        running_runs = self.search(
            [("run_status", "=", "running"), ("workflow_name", "!=", False)],
            order="run_start asc, create_date asc",
            limit=self.POLL_RUNNING_LIMIT,
        )
        # Defense in depth: re-poll recently-finished runs missing steps/logs
        # (catches callback-wins-race where steps weren't synced before pod GC).
        # Pure SQL via stored has_log + polish OR to include zero-step runs.
        recent_cutoff = fields.Datetime.now() - timedelta(minutes=10)
        terminal_incomplete = self.search([
                ("run_status", "in", ("done", "failed")),
                ("workflow_name", "!=", False),
                ("run_end", ">=", recent_cutoff),
                "|",
                ("step_ids", "=", False),
                ("step_ids.has_log", "=", False),
            ],
            order="run_end asc, create_date asc",
            limit=self.POLL_TERMINAL_INCOMPLETE_LIMIT,
        )
        all_runs = running_runs | terminal_incomplete
        if not all_runs:
            return
        _logger.info(
            "[kaiju.commit0.run] poll_run: tick running=%s terminal_incomplete=%s",
            len(running_runs), len(terminal_incomplete),
        )

        argo = self.env["kaiju.argo.client"]
        for run in all_runs:
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

            # Incrementally persist logs for any finished step that doesn't
            # yet have logs cached. This ensures logs land in Postgres well
            # before Argo's pod GC (default 5min) deletes the source pods.
            try:
                run._persist_step_logs_incremental()
            except Exception as e:
                _logger.warning(
                    "Incremental log persist failed for run %s: %s", run.name, e
                )

            # Skip status write if already terminal (callback won). Step sync above runs unconditionally.
            if run.run_status in ("done", "failed"):
                pass
            elif phase in ("Succeeded",):
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
                            run.run_log, f"Workflow failed: {phase} — {message}"
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
        elapsed = (fields.Datetime.now() - start_ts).total_seconds()
        _logger.info(
            "[kaiju.commit0.run] poll_run: done processed=%s elapsed=%.2fs",
            len(all_runs), elapsed,
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
                "pod_name": node.get("podName") or node_id,
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

    def _persist_step_logs_incremental(self):
        """Fetch logs ONLY for steps that have finished but don't yet have logs.

        Called on every cron poll to capture step logs as soon as each pod
        completes — well before Argo's pod garbage collection (typically 5min)
        kicks in. Idempotent: skips steps that already have log_text.
        """
        self.ensure_one()
        terminal_phases = ("Succeeded", "Failed", "Error")
        for step in self.step_ids:
            if step.node_type != "Pod" or not step.pod_name:
                continue
            if step.phase not in terminal_phases:
                continue
            if step.has_log:
                continue  # already captured — don't re-fetch
            try:
                step.action_fetch_logs()
            except Exception as e:
                _logger.warning(
                    "Incremental log fetch failed for step %s (pod=%s): %s",
                    step.display_name or step.node_id,
                    step.pod_name,
                    e,
                )

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
