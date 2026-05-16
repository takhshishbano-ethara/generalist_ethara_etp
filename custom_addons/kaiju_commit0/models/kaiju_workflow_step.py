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
    _rec_name = "display_name"

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
        store=False,
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

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_fetch_logs(self):
        """Re-fetch logs from Argo for the selected steps.

        Captures detailed diagnostic info per attempt into `last_fetch_diagnostic`
        so the user can see exactly what happened without needing server log
        access. On success, preserves the existing cached log_text if Argo
        returned empty (avoids overwriting good data with nothing).
        """
        from datetime import datetime, timezone
        argo = self.env["kaiju.argo.client"]
        fetched = 0
        skipped = 0
        failed = 0
        for step in self:
            workflow_name = step.workflow_name
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if not workflow_name or not step.pod_name:
                diag = (
                    f"[{now_iso}] SKIPPED \u2014 missing inputs\n"
                    f"  workflow_name: {workflow_name!r}\n"
                    f"  pod_name:      {step.pod_name!r}\n"
                    f"  display_name:  {step.display_name!r}\n"
                    f"  node_id:       {step.node_id!r}"
                )
                step.write({"last_fetch_diagnostic": diag})
                _logger.info(
                    "Skipping log fetch for step %s \u2014 missing workflow or pod name",
                    step.display_name or step.node_id,
                )
                skipped += 1
                continue
            try:
                logs = argo.get_pod_logs(workflow_name, step.pod_name)
            except RuntimeError as e:
                diag = (
                    f"[{now_iso}] FAILED \u2014 Argo API error\n"
                    f"  workflow:      {workflow_name}\n"
                    f"  pod:           {step.pod_name}\n"
                    f"  template:      {step.template_name}\n"
                    f"  error:         {e}\n"
                    f"\nLikely causes:\n"
                    f"  - RBAC: Odoo's ServiceAccount lacks 'pods/log' on namespace "
                    f"(check: HTTP 403 in error above)\n"
                    f"  - Argo Server unreachable (check: connection error in message)\n"
                    f"  - Wrong namespace / workflow GC'd by Argo controller"
                )
                step.write({"last_fetch_diagnostic": diag})
                _logger.warning(
                    "Failed to fetch logs for step %s (workflow=%s pod=%s): %s",
                    step.display_name or step.node_id,
                    workflow_name,
                    step.pod_name,
                    e,
                )
                failed += 1
                continue

            if logs:
                size = len(logs.encode("utf-8", errors="replace"))
                diag = (
                    f"[{now_iso}] SUCCESS \u2014 fetched {size:,} bytes\n"
                    f"  workflow:      {workflow_name}\n"
                    f"  pod:           {step.pod_name}\n"
                    f"  template:      {step.template_name}\n"
                    f"  lines:         {logs.count(chr(10)) + 1:,}\n"
                    f"  preview:       {logs[:200]!r}"
                )
                step.write(
                    {
                        "log_text": logs,
                        "log_fetched_at": fields.Datetime.now(),
                        "last_fetch_diagnostic": diag,
                    }
                )
                _logger.info(
                    "Fetched %d bytes of logs for step %s (pod=%s)",
                    len(logs),
                    step.display_name or step.node_id,
                    step.pod_name,
                )
                fetched += 1
            else:
                # Pod returned NO log content. Still record the attempt so the
                # user can see that we tried and Argo returned empty.
                diag = (
                    f"[{now_iso}] EMPTY \u2014 Argo returned 0 bytes\n"
                    f"  workflow:      {workflow_name}\n"
                    f"  pod:           {step.pod_name}\n"
                    f"  template:      {step.template_name}\n"
                    f"  phase:         {step.phase}\n"
                    f"\nLikely causes:\n"
                    f"  - Pod has been garbage-collected by Argo (default 300s/5min after completion)\n"
                    f"  - Pod's main container hasn't produced output yet (still starting)\n"
                    f"  - Wrong container name (we request 'main' \u2014 verify in Argo UI)\n"
                    f"  - RBAC: Odoo's ServiceAccount lacks 'pods/log' \u2014 silent 403/empty response\n"
                    f"\nVerify from a pod with the Odoo SA token:\n"
                    f"  curl -v -N -H 'Accept: text/event-stream' \\\n"
                    f"    -H \"Authorization: Bearer $TOKEN\" \\\n"
                    f"    '<argo>/api/v1/workflows/<ns>/{workflow_name}/log"
                    f"?podName={step.pod_name}&logOptions.container=main&logOptions.follow=true'"
                )
                step.write({
                    "log_fetched_at": fields.Datetime.now(),
                    "last_fetch_diagnostic": diag,
                })
                _logger.info(
                    "Empty logs returned for step %s (pod=%s) \u2014 marking fetched_at",
                    step.display_name or step.node_id,
                    step.pod_name,
                )
                skipped += 1
        _logger.info(
            "action_fetch_logs summary: fetched=%d skipped=%d failed=%d",
            fetched, skipped, failed,
        )
        return {"type": "ir.actions.client", "tag": "reload"}
