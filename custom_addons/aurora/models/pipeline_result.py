from odoo import api, fields, models


class AuroraPipelineResult(models.Model):
    _name = "aurora.pipeline.result"
    _description = "Aurora Phase 2 Instance Result"
    _order = "sequence, id"

    pipeline_id = fields.Many2one(
        "aurora.pipeline",
        string="Pipeline",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)

    instance_id = fields.Char(
        string="Instance ID", readonly=True,
        help="LHT instance identifier: {org}__{repo}-{tag_start}..{tag_end}",
    )
    tag_start = fields.Char(string="Tag Start", readonly=True)
    tag_end = fields.Char(string="Tag End", readonly=True)
    pr_numbers = fields.Char(
        string="PR Numbers", readonly=True,
        help="Comma-separated list of PR numbers in this version interval",
    )
    pr_attribution_method = fields.Selection(
        [
            ("merge_log", "Merge Log"),
            ("compare_api", "Compare API"),
            ("git_cherry", "Git Cherry"),
            ("date_range", "Date Range"),
        ],
        string="Attribution Method", readonly=True,
    )
    version_scheme = fields.Selection(
        [("semver", "Semver"), ("calver", "Calver"), ("mixed", "Mixed")],
        string="Version Scheme", readonly=True,
    )
    valid = fields.Boolean(string="Resolved", readonly=True)

    # --- Test category counts ---
    f2p_count = fields.Integer(string="F2P", readonly=True, help="Fail-to-Pass tests")
    p2p_count = fields.Integer(string="P2P", readonly=True, help="Pass-to-Pass tests")
    s2p_count = fields.Integer(string="S2P", readonly=True, help="Skip-to-Pass tests")
    n2p_count = fields.Integer(string="N2P", readonly=True, help="None-to-Pass tests")
    fixed_count = fields.Integer(string="Fixed", readonly=True, help="Fixed tests")

    # --- Run result (base run, no patches applied) ---
    run_passed = fields.Integer(
        string="Run Pass", readonly=True, help="Passed tests in base run (no patches)")
    run_failed = fields.Integer(
        string="Run Fail", readonly=True, help="Failed tests in base run (no patches)")
    run_skipped = fields.Integer(
        string="Run Skip", readonly=True, help="Skipped tests in base run (no patches)")
    run_total = fields.Integer(
        string="Run Total", compute="_compute_run_total", store=True)

    # --- Test-patch result (after applying test patch only) ---
    test_passed = fields.Integer(
        string="Test Pass", readonly=True, help="Passed tests after test patch")
    test_failed = fields.Integer(
        string="Test Fail", readonly=True, help="Failed tests after test patch")
    test_skipped = fields.Integer(
        string="Test Skip", readonly=True, help="Skipped tests after test patch")
    test_total = fields.Integer(
        string="Test Total", compute="_compute_test_total", store=True)

    # --- Fix-patch result (after applying test + fix patches) ---
    fix_passed = fields.Integer(
        string="Fix Pass", readonly=True, help="Passed tests after fix patch")
    fix_failed = fields.Integer(
        string="Fix Fail", readonly=True, help="Failed tests after fix patch")
    fix_skipped = fields.Integer(
        string="Fix Skip", readonly=True, help="Skipped tests after fix patch")
    fix_total = fields.Integer(
        string="Fix Total", compute="_compute_fix_total", store=True)

    # --- Detailed test lists ---
    f2p_tests = fields.Text(string="F2P Tests", readonly=True)
    p2p_tests = fields.Text(string="P2P Tests", readonly=True)
    s2p_tests = fields.Text(string="S2P Tests", readonly=True)
    n2p_tests = fields.Text(string="N2P Tests", readonly=True)
    fixed_tests = fields.Text(string="Fixed Tests", readonly=True)

    error_msg = fields.Text(string="Error", readonly=True)

    status_icon = fields.Char(
        string="Status",
        compute="_compute_status_icon",
        store=False,
    )

    @api.depends("run_passed", "run_failed", "run_skipped")
    def _compute_run_total(self):
        for rec in self:
            rec.run_total = rec.run_passed + rec.run_failed + rec.run_skipped

    @api.depends("test_passed", "test_failed", "test_skipped")
    def _compute_test_total(self):
        for rec in self:
            rec.test_total = rec.test_passed + rec.test_failed + rec.test_skipped

    @api.depends("fix_passed", "fix_failed", "fix_skipped")
    def _compute_fix_total(self):
        for rec in self:
            rec.fix_total = rec.fix_passed + rec.fix_failed + rec.fix_skipped

    @api.depends("valid", "error_msg")
    def _compute_status_icon(self):
        for rec in self:
            if rec.valid:
                rec.status_icon = "✓ Resolved"
            elif rec.error_msg:
                rec.status_icon = "✗ Error"
            else:
                rec.status_icon = "✗ Unresolved"
