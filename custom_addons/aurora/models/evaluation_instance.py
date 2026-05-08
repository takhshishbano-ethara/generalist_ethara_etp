"""Per-instance evaluation record (LHT tag-interval model).

Every Phase-2 evaluation expands into N per-instance records, one per LHT
version interval (not per individual PR). This model stores:
  - Identifying info (org, repo, instance_id, tag_start/tag_end, pr_numbers,
    image_tag).
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
    _description = "Aurora Evaluation Instance (LHT interval)"
    _order = "evaluation_id desc, instance_id asc"
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
    instance_id = fields.Char(
        string="Instance ID", required=True, index=True,
        help="LHT instance identifier: {org}__{repo}-{tag_start}..{tag_end}",
    )
    tag_start = fields.Char(string="Tag Start")
    tag_end = fields.Char(string="Tag End")
    pr_numbers = fields.Char(
        string="PR Numbers",
        help="Comma-separated list of PR numbers merged in this version interval.",
    )
    pr_attribution_method = fields.Selection(
        [
            ("merge_log", "Merge Log"),
            ("compare_api", "Compare API"),
            ("git_cherry", "Git Cherry"),
            ("date_range", "Date Range"),
        ],
        string="Attribution Method",
    )
    version_scheme = fields.Selection(
        [("semver", "Semver"), ("calver", "Calver"), ("mixed", "Mixed")],
        string="Version Scheme",
    )
    image_tag = fields.Char(
        string="Image Tag",
        help="Docker image tag assigned by the harness "
             "(e.g. v4.28.0..v4.28.1).",
    )
    image_workdir = fields.Char(
        string="Image Workdir",
        help="Name of the subdir under workdir/<org>/<repo>/images/. Matches "
             "instance.dependency().workdir() — harness-defined.",
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

    build_log_tail = fields.Text(string="Build Log (tail)", readonly=True)
    run_log_tail = fields.Text(string="Run Log (tail)", readonly=True)
    test_patch_log_tail = fields.Text(string="Test Patch Log (tail)", readonly=True)
    fix_patch_log_tail = fields.Text(string="Fix Patch Log (tail)", readonly=True)

    dockerfile_s3_uri = fields.Char(string="Dockerfile (S3)", readonly=True)
    build_log_s3_uri = fields.Char(string="Build Log (S3)", readonly=True)
    run_log_s3_uri = fields.Char(string="Run Log (S3)", readonly=True)
    test_patch_log_s3_uri = fields.Char(string="Test Patch Log (S3)", readonly=True)
    fix_patch_log_s3_uri = fields.Char(string="Fix Patch Log (S3)", readonly=True)
    report_json_s3_uri = fields.Char(string="Report (S3)", readonly=True)
    fix_patch_s3_uri = fields.Char(string="fix.patch (S3)", readonly=True)
    oci_tar_s3_uri = fields.Char(string="OCI Image Tar (S3)", readonly=True)

    dockerfile_local_path = fields.Char(string="Dockerfile (local)", readonly=True)
    build_log_local_path = fields.Char(string="Build Log (local)", readonly=True)
    run_log_local_path = fields.Char(string="Run Log (local)", readonly=True)
    test_patch_log_local_path = fields.Char(string="Test Patch Log (local)", readonly=True)
    fix_patch_log_local_path = fields.Char(string="Fix Patch Log (local)", readonly=True)
    report_json_local_path = fields.Char(string="Report (local)", readonly=True)

    display_name = fields.Char(compute="_compute_display_name", store=True)

    _sql_constraints = [
        (
            "uniq_eval_instance",
            "UNIQUE(evaluation_id, instance_id)",
            "Each LHT instance can appear only once per evaluation.",
        ),
    ]

    @api.depends("org", "repo", "tag_start", "tag_end", "instance_id")
    def _compute_display_name(self):
        for rec in self:
            if rec.org and rec.repo and rec.tag_start and rec.tag_end:
                rec.display_name = f"{rec.org}/{rec.repo}-{rec.tag_start}..{rec.tag_end}"
            elif rec.instance_id:
                rec.display_name = rec.instance_id
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
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/aurora/evaluation/instance/{self.id}/log/{kind}",
            "target": "new",
        }
