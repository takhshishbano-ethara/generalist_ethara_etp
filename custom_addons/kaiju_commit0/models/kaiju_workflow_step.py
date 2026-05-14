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
    _order = "started_at asc, id asc"

    name = fields.Char(
        string="Step",
        compute="_compute_name",
        store=True,
    )

    # ── Argo identifiers ─────────────────────────────────────────────────────

    node_id = fields.Char(string="Node ID", required=True, index=True)
    display_name = fields.Char(string="Display Name")
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

    # ── Logs ─────────────────────────────────────────────────────────────────

    log_text = fields.Text(string="Log Text")
    log_fetched_at = fields.Datetime(string="Log Fetched At")

    # ── Timing ───────────────────────────────────────────────────────────────

    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")

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

    # ── Computed ─────────────────────────────────────────────────────────────

    @api.depends("display_name", "node_id")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.display_name or rec.node_id or "Step"

    @api.depends("build_id.workflow_name", "run_id.workflow_name")
    def _compute_workflow_name(self):
        for rec in self:
            if rec.build_id:
                rec.workflow_name = rec.build_id.workflow_name or ""
            elif rec.run_id:
                rec.workflow_name = rec.run_id.workflow_name or ""
            else:
                rec.workflow_name = ""

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_fetch_logs(self):
        """Re-fetch logs from Argo for the selected steps.

        On failure (pod GC'd, network error, empty result), preserves
        the existing cached log_text and logs a warning.
        """
        argo = self.env["kaiju.argo.client"]
        for step in self:
            workflow_name = step.workflow_name
            if not workflow_name or not step.pod_name:
                _logger.info(
                    "Skipping log fetch for step %s — missing workflow or pod name",
                    step.display_name or step.node_id,
                )
                continue
            try:
                logs = argo.get_pod_logs(workflow_name, step.pod_name)
            except RuntimeError as e:
                _logger.warning(
                    "Failed to fetch logs for step %s (pod=%s): %s",
                    step.display_name or step.node_id,
                    step.pod_name,
                    e,
                )
                continue

            if logs:
                step.write(
                    {
                        "log_text": logs,
                        "log_fetched_at": fields.Datetime.now(),
                    }
                )
            else:
                _logger.info(
                    "Empty logs returned for step %s — keeping cached value",
                    step.display_name or step.node_id,
                )
        return {"type": "ir.actions.client", "tag": "reload"}
