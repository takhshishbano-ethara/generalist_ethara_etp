# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PHASE_ORDER = ["config", "build"]

BUILD_WORKFLOW_TEMPLATE = "kaiju-build-pipeline"


class KaijuCommit0(models.Model):
    _name = "kaiju.commit0"
    _description = "Commit0 Build"
    _order = "create_date desc"

    # ── Configuration ────────────────────────────────────────────────────────

    name = fields.Char(string="Build ID", readonly=True, copy=False, default="New")
    repo_name = fields.Char(string="Repository", required=True)
    language = fields.Selection(
        [
            ("python", "Python"),
            ("java", "Java"),
            ("go", "Go"),
            ("typescript", "TypeScript"),
            ("rust", "Rust"),
        ],
        string="Language",
        required=True,
        default="python",
    )
    language_version = fields.Char(
        string="Language Version",
        help="e.g. 3.11, 11, 1.21, 18, 1.75.0",
    )
    branch_name = fields.Char(
        string="Branch",
        default="commit0_combined",
        help="Git branch to clone from the fork",
    )

    # ── Navigation ───────────────────────────────────────────────────────────

    current_phase = fields.Selection(
        [
            ("config", "Configuration"),
            ("build", "Build"),
        ],
        string="Current Phase",
        default="config",
    )

    # ── Phase: Config Validation ─────────────────────────────────────────────

    config_valid = fields.Boolean(string="Config Validated", default=False)

    # ── Phase: Build (prepare + build-repo-image) ────────────────────────────

    build_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Build Status",
        default="pending",
    )
    build_start = fields.Datetime(string="Build Start", readonly=True)
    build_end = fields.Datetime(string="Build End", readonly=True)
    build_log = fields.Text(string="Build Log")

    image_uri = fields.Char(string="Image URI", readonly=True)
    s3_dataset_uri = fields.Char(string="Dataset S3 URI", readonly=True)
    workflow_name = fields.Char(string="Build Workflow", readonly=True)

    # ── Runs ─────────────────────────────────────────────────────────────────

    run_ids = fields.One2many("kaiju.commit0.run", "build_id", string="Runs")
    run_count = fields.Integer(string="Run Count", compute="_compute_run_count")

    # ── Computed ─────────────────────────────────────────────────────────────

    is_build_running = fields.Boolean(compute="_compute_is_build_running", store=False)
    can_go_next = fields.Boolean(compute="_compute_navigation_flags", store=False)
    can_go_back = fields.Boolean(compute="_compute_navigation_flags", store=False)
    can_create_run = fields.Boolean(compute="_compute_can_create_run", store=False)

    @api.depends("run_ids")
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    @api.depends("build_status")
    def _compute_is_build_running(self):
        for rec in self:
            rec.is_build_running = rec.build_status == "running"

    @api.depends("current_phase", "config_valid", "build_status")
    def _compute_navigation_flags(self):
        for rec in self:
            idx = (
                PHASE_ORDER.index(rec.current_phase)
                if rec.current_phase in PHASE_ORDER
                else 0
            )
            rec.can_go_back = idx > 0

            if rec.current_phase == "config":
                rec.can_go_next = rec.config_valid
            else:
                rec.can_go_next = False

    @api.depends("build_status")
    def _compute_can_create_run(self):
        for rec in self:
            rec.can_create_run = rec.build_status == "done"

    # ── CRUD ─────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("kaiju.commit0") or "New"
                )
        return super().create(vals_list)

    # ── Navigation Actions ───────────────────────────────────────────────────

    def action_next_phase(self):
        self.ensure_one()
        idx = (
            PHASE_ORDER.index(self.current_phase)
            if self.current_phase in PHASE_ORDER
            else 0
        )
        if idx < len(PHASE_ORDER) - 1:
            self.current_phase = PHASE_ORDER[idx + 1]

    def action_prev_phase(self):
        self.ensure_one()
        idx = (
            PHASE_ORDER.index(self.current_phase)
            if self.current_phase in PHASE_ORDER
            else 0
        )
        if idx > 0:
            self.current_phase = PHASE_ORDER[idx - 1]

    # ── Phase Actions ────────────────────────────────────────────────────────

    def action_validate_config(self):
        self.ensure_one()
        if not self.repo_name:
            raise UserError("Repository name is required.")
        if "/" not in self.repo_name:
            raise UserError("Repository must be in 'owner/repo' format.")
        self.write({"config_valid": True})

    def action_run_build(self):
        """Submit build workflow to Argo Server (prepare + build-repo-image)."""
        self.ensure_one()
        if not self.config_valid:
            raise UserError("Validate configuration first.")
        if self.build_status == "done":
            raise UserError("Build already complete. Create a new run instead.")
        if self.build_status == "running":
            raise UserError("Build is already running.")

        argo = self.env["kaiju.argo.client"]
        repo_short = (self.repo_name or "unknown").split("/")[-1]

        # repo_hash: short identifier for image tagging
        import hashlib

        repo_hash = hashlib.sha256(
            f"{self.repo_name}:{self.branch_name}".encode()
        ).hexdigest()[:12]

        callback_url = self._get_build_callback_url()

        parameters = {
            "repo_name": self.repo_name,
            "repo_hash": repo_hash,
            "language": self.language,
            "branch_name": self.branch_name or "commit0_combined",
            "odoo_job_id": str(self.id),
            "callback_url": callback_url,
        }

        try:
            workflow_name = argo.submit_workflow(
                BUILD_WORKFLOW_TEMPLATE,
                parameters,
                labels={"kaiju/build-id": self.name, "kaiju/repo": repo_short},
            )
        except RuntimeError as e:
            _logger.error("Failed to submit build workflow for %s: %s", self.name, e)
            raise UserError(f"Failed to submit build workflow: {e}") from e

        self.write(
            {
                "build_status": "running",
                "build_start": fields.Datetime.now(),
                "workflow_name": workflow_name,
                "build_log": f"Workflow submitted: {workflow_name}\nWaiting for pipeline...",
            }
        )

    def _get_build_callback_url(self):
        """Construct the callback URL for the build pipeline to call on completion."""
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = ICP.get_param(
            "kaiju.odoo_internal_url", "http://odoo-web.odoo.svc:8069"
        )
        return f"{base_url}/kaiju/callback/build"

    def action_create_run(self):
        self.ensure_one()
        if self.build_status != "done":
            raise UserError("Build must be complete before creating a run.")
        return {
            "type": "ir.actions.act_window",
            "name": "New Run",
            "res_model": "kaiju.commit0.run",
            "view_mode": "form",
            "context": {"default_build_id": self.id},
            "target": "current",
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Runs for {self.name}",
            "res_model": "kaiju.commit0.run",
            "view_mode": "list,form",
            "domain": [("build_id", "=", self.id)],
            "context": {"default_build_id": self.id},
        }

    def action_abort(self):
        """Stop running build workflow via Argo Server."""
        self.ensure_one()
        if self.build_status != "running":
            raise UserError("No build is currently running.")

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
                "build_status": "failed",
                "build_end": fields.Datetime.now(),
            }
        )

    def action_reset(self):
        self.ensure_one()
        if self.build_status == "running":
            raise UserError("Cannot reset while build is running. Abort first.")
        self.write(
            {
                "current_phase": "config",
                "config_valid": False,
                "build_status": "pending",
                "build_start": False,
                "build_end": False,
                "build_log": False,
                "image_uri": False,
                "s3_dataset_uri": False,
                "workflow_name": False,
            }
        )

    # ── Cron: Poll Argo for running builds ───────────────────────────────────

    @api.model
    def _cron_poll_build_status(self):
        """Called by ir.cron every 60s to update running builds from Argo."""
        running_builds = self.search(
            [("build_status", "=", "running"), ("workflow_name", "!=", False)]
        )
        if not running_builds:
            return

        argo = self.env["kaiju.argo.client"]
        for build in running_builds:
            try:
                status = argo.get_workflow_status(build.workflow_name)
            except RuntimeError as e:
                _logger.warning(
                    "Failed to poll build %s (workflow %s): %s",
                    build.name,
                    build.workflow_name,
                    e,
                )
                continue

            phase = status.get("phase", "")
            progress = status.get("progress", "")

            if phase in ("Succeeded",):
                build.write(
                    {
                        "build_status": "done",
                        "build_end": fields.Datetime.now(),
                        "build_log": self._append_log(
                            build.build_log, f"Workflow completed. Progress: {progress}"
                        ),
                    }
                )
            elif phase in ("Failed", "Error"):
                message = status.get("message", "Unknown error")
                build.write(
                    {
                        "build_status": "failed",
                        "build_end": fields.Datetime.now(),
                        "build_log": self._append_log(
                            build.build_log, f"Workflow failed: {phase} — {message}"
                        ),
                    }
                )
            elif phase in ("Running", "Pending", ""):
                build.write(
                    {
                        "build_log": self._append_log(
                            build.build_log,
                            f"Status: {phase or 'Pending'} ({progress})",
                        ),
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
