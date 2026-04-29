"""Per-PR evaluation instance record.

Every Phase-2 evaluation expands into N per-PR instances. This model stores:
  - Identifying info (org, repo, pr_number, image_tag).
  - Current status (pending/building/built/running/resolved/unresolved/error).
  - Per-artifact S3 URIs (durable storage).
  - Small artifact content inlined in DB (Dockerfile, report.json, fix.patch).
  - Tail slices (last ~64 KB) of each log for instant UI preview.
  - Local path hints (transient; valid only while the run is on the worker host).

Large logs always flow through a controller endpoint: it serves from the local
disk during a run (live tail) and falls back to the S3 URI once the phase
completes. See evaluation_executor.py for the write path.
"""
from odoo import api, fields, models

EVAL_INSTANCE_STATUS = [
    ("pending", "Pending"),
    ("building", "Building"),
    ("built", "Built"),
    ("running", "Running"),
    ("resolved", "Resolved"),
    ("unresolved", "Unresolved"),
    ("error", "Error"),
]


class AuroraEvaluationInstance(models.Model):
    _name = "aurora.evaluation.instance"
    _description = "Aurora Evaluation Instance (per PR)"
    _order = "evaluation_id desc, pr_number asc"
    _rec_name = "display_name"

    evaluation_id = fields.Many2one(
        "aurora.evaluation",
        string="Evaluation",
        required=True,
        ondelete="cascade",
        index=True,
    )
    org = fields.Char(string="Org", required=True)
    repo = fields.Char(string="Repo", required=True)
    pr_number = fields.Integer(string="PR #", required=True, index=True)
    image_tag = fields.Char(
        string="Image Tag",
        help="Docker image tag (e.g. pr-123). Multiple PRs can share one image.",
    )
    image_workdir = fields.Char(
        string="Image Workdir",
        help="Name of the subdir under workdir/<org>/<repo>/images/. Matches "
             "instance.dependency().workdir() — usually pr-<number>, or "
             "pr-<first>-<last> for PR bundles.",
    )

    status = fields.Selection(
        EVAL_INSTANCE_STATUS,
        default="pending",
        required=True,
        tracking=True,
        index=True,
    )
    resolved = fields.Boolean(string="Resolved", default=False)
    error_message = fields.Text(string="Error", readonly=True)

    f2p_count = fields.Integer(string="F2P", default=0, help="Failing tests now passing.")
    p2p_count = fields.Integer(string="P2P", default=0, help="Passing tests still passing.")
    s2p_count = fields.Integer(string="S2P", default=0)
    n2p_count = fields.Integer(string="N2P", default=0)

    # --- Inline content (small artifacts live here, NOT on disk). -------------
    dockerfile_content = fields.Text(
        string="Dockerfile",
        help="Inline Dockerfile (capped at 16 KB). Larger uploads only go to S3.",
    )
    report_json_content = fields.Text(
        string="Report JSON",
        help="Raw per-instance report.json (capped at 128 KB).",
    )
    fix_patch_content = fields.Text(
        string="fix.patch",
        help="Generated fix patch (capped at 256 KB). Full patch on S3.",
    )

    # --- Log tails (last ~64 KB of each log for instant preview). -------------
    build_log_tail = fields.Text(string="Build Log (tail)", readonly=True)
    run_log_tail = fields.Text(string="Run Log (tail)", readonly=True)
    test_patch_log_tail = fields.Text(string="Test Patch Log (tail)", readonly=True)
    fix_patch_log_tail = fields.Text(string="Fix Patch Log (tail)", readonly=True)

    # --- S3 URIs (durable storage; public URLs built by s3_storage). ---------
    dockerfile_s3_uri = fields.Char(string="Dockerfile (S3)", readonly=True)
    build_log_s3_uri = fields.Char(string="Build Log (S3)", readonly=True)
    run_log_s3_uri = fields.Char(string="Run Log (S3)", readonly=True)
    test_patch_log_s3_uri = fields.Char(string="Test Patch Log (S3)", readonly=True)
    fix_patch_log_s3_uri = fields.Char(string="Fix Patch Log (S3)", readonly=True)
    report_json_s3_uri = fields.Char(string="Report (S3)", readonly=True)
    fix_patch_s3_uri = fields.Char(string="fix.patch (S3)", readonly=True)

    # --- Local paths (transient; only valid while workdir exists). ------------
    dockerfile_local_path = fields.Char(string="Dockerfile (local)", readonly=True)
    build_log_local_path = fields.Char(string="Build Log (local)", readonly=True)
    run_log_local_path = fields.Char(string="Run Log (local)", readonly=True)
    test_patch_log_local_path = fields.Char(string="Test Patch Log (local)", readonly=True)
    fix_patch_log_local_path = fields.Char(string="Fix Patch Log (local)", readonly=True)
    report_json_local_path = fields.Char(string="Report (local)", readonly=True)

    display_name = fields.Char(compute="_compute_display_name", store=True)

    _sql_constraints = [
        (
            "uniq_eval_pr",
            "UNIQUE(evaluation_id, pr_number)",
            "Each PR can appear only once per evaluation.",
        ),
    ]

    @api.depends("org", "repo", "pr_number")
    def _compute_display_name(self):
        for rec in self:
            if rec.org and rec.repo and rec.pr_number:
                rec.display_name = f"{rec.org}/{rec.repo}#{rec.pr_number}"
            else:
                rec.display_name = f"Instance {rec.id}"

    def action_open_build_log(self):
        return self._open_log_action("build_log")

    def action_open_run_log(self):
        return self._open_log_action("run_log")

    def action_open_test_patch_log(self):
        return self._open_log_action("test_patch_log")

    def action_open_fix_patch_log(self):
        return self._open_log_action("fix_patch_log")

    def _open_log_action(self, kind):
        """Stream a log file via the /aurora/evaluation/instance/<id>/log/<kind> endpoint.

        The controller handles live-tail (local disk) vs archived (S3) fallback.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/aurora/evaluation/instance/{self.id}/log/{kind}",
            "target": "new",
        }
