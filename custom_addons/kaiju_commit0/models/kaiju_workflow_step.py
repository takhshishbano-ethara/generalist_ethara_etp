# -*- coding: utf-8 -*-
"""Argo Workflow Step model — represents a single Pod node within a workflow.

Stores per-step status and persisted log text so logs survive Argo pod GC.
"""

import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class KaijuWorkflowStep(models.Model):
    _name = "kaiju.commit0.workflow.step"
    _description = "Kaiju Argo Workflow Step"
    _order = "step_order asc, started_at asc, id asc"
    _rec_name = "step_name"

    name = fields.Char(
        string="Step",
        compute="_compute_name",
        store=True,
    )

    # ── Argo identifiers ─────────────────────────────────────────────────────

    node_id = fields.Char(string="Node ID", required=True, index=True)
    step_name = fields.Char(string="Step Name")
    pod_name = fields.Char(string="Pod Name")
    template_name = fields.Char(string="Template Name")
    node_type = fields.Char(string="Node Type", default="Pod")

    # ── Status ───────────────────────────────────────────────────────────────

    phase = fields.Selection(
        [
            ("Pending", "Pending"),
            ("Running", "Running"),
            ("Succeeded", "Succeeded"),
            ("Failed", "Failed"),
            ("Error", "Error"),
            ("Skipped", "Skipped"),
            ("Omitted", "Omitted"),
        ],
        string="Phase",
        default="Pending",
    )
    message = fields.Text(string="Message")

    # ── Step ordering & S3 log reference ────────────────────────────────────

    step_order = fields.Integer(
        string="Step Order",
        default=0,
        help="Execution order from the Argo exit-hook callback (1-based). "
        "0 means order is unknown (legacy Argo-polled steps).",
    )
    log_file = fields.Char(
        string="Log File",
        help="S3 object key (relative to the parent's s3_log_prefix) for "
        "this step's log file, e.g. 'clone-repo.log'.",
    )

    # ── Logs ─────────────────────────────────────────────────────────────────

    log_text = fields.Text(string="Log Text")
    log_fetched_at = fields.Datetime(string="Log Fetched At")
    last_fetch_diagnostic = fields.Text(
        string="Last Fetch Diagnostic",
        help="Detailed result of the most recent log fetch attempt \u2014 "
        "surfaces Argo API response details (status, bytes, frames, errors) "
        "so the UI can show what happened without needing server log access.",
    )

    # ── Timing ───────────────────────────────────────────────────────────────

    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")

    duration_seconds = fields.Float(
        string="Duration (s)",
        compute="_compute_duration",
        store=False,
    )

    # ── Log metrics ─────────────────────────────────────────────────────────

    log_size = fields.Integer(
        string="Log Size (bytes)",
        compute="_compute_log_size",
        store=False,
    )
    log_size_human = fields.Char(
        string="Log Size",
        compute="_compute_log_size",
        store=False,
        help="Human-readable log size (e.g. '8.2 KB')",
    )
    has_log = fields.Boolean(
        string="Has Log",
        compute="_compute_log_size",
        store=True,
        help="True when log_text is non-empty — distinguishes 'fetched but empty' from 'never fetched'",
    )

    # ── Parent references ────────────────────────────────────────────────────

    build_id = fields.Many2one(
        "kaiju.commit0",
        string="Build",
        ondelete="cascade",
        index=True,
    )
    run_id = fields.Many2one(
        "kaiju.commit0.run",
        string="Run",
        ondelete="cascade",
        index=True,
    )

    workflow_name = fields.Char(
        string="Workflow Name",
        compute="_compute_workflow_name",
        store=False,
    )

    _sql_constraints = [
        (
            "build_or_run_required",
            "CHECK ((build_id IS NOT NULL AND run_id IS NULL) "
            "OR (build_id IS NULL AND run_id IS NOT NULL))",
            "A workflow step must belong to exactly one of build or run.",
        ),
        (
            "unique_build_node",
            "UNIQUE(build_id, node_id)",
            "Node ID must be unique within a build.",
        ),
        (
            "unique_run_node",
            "UNIQUE(run_id, node_id)",
            "Node ID must be unique within a run.",
        ),
    ]

    # ── Partial indexes for "steps without logs" queries ──────────────────
    # Plain index on a Boolean is near-useless (planner prefers seqscan on 50/50
    # split). Partial indexes only on rows where has_log IS FALSE give cheap
    # lookups for the Sync Logs action even at 100k+ step records.

    def _auto_init(self):
        res = super()._auto_init()
        cr = self.env.cr
        # Partial index on (build_id) WHERE has_log IS FALSE — supports
        # queries like "find builds with unfetched step logs" efficiently.
        cr.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self._table}_build_no_log_idx
            ON {self._table} (build_id)
            WHERE has_log IS FALSE AND build_id IS NOT NULL
            """
        )
        cr.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self._table}_run_no_log_idx
            ON {self._table} (run_id)
            WHERE has_log IS FALSE AND run_id IS NOT NULL
            """
        )
        return res

    # ── Computed ─────────────────────────────────────────────────────────────

    @api.depends("step_name", "node_id")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.step_name or rec.node_id or "Step"

    @api.depends("build_id.workflow_name", "run_id.workflow_name")
    def _compute_workflow_name(self):
        for rec in self:
            if rec.build_id:
                rec.workflow_name = rec.build_id.workflow_name or ""
            elif rec.run_id:
                rec.workflow_name = rec.run_id.workflow_name or ""
            else:
                rec.workflow_name = ""

    @api.depends("started_at", "finished_at")
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration_seconds = delta.total_seconds()
            else:
                rec.duration_seconds = 0.0

    @api.depends("log_text")
    def _compute_log_size(self):
        for rec in self:
            if not rec.log_text:
                rec.log_size = 0
                rec.log_size_human = ""
                rec.has_log = False
                continue
            size = len(rec.log_text.encode("utf-8", errors="replace"))
            rec.log_size = size
            rec.has_log = True
            # Human-readable
            if size < 1024:
                rec.log_size_human = f"{size} B"
            elif size < 1024 * 1024:
                rec.log_size_human = f"{size / 1024:.1f} KB"
            else:
                rec.log_size_human = f"{size / (1024 * 1024):.1f} MB"
