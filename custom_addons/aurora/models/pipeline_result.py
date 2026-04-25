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

    instance_id = fields.Char(string="Instance ID", readonly=True)
    valid = fields.Boolean(string="Resolved", readonly=True)
    f2p_count = fields.Integer(string="F2P", readonly=True, help="Fail-to-Pass tests")
    p2p_count = fields.Integer(string="P2P", readonly=True, help="Pass-to-Pass tests")
    s2p_count = fields.Integer(string="S2P", readonly=True, help="Skip-to-Pass tests")
    n2p_count = fields.Integer(string="N2P", readonly=True, help="None-to-Pass tests")
    fixed_count = fields.Integer(string="Fixed", readonly=True, help="Fixed tests")

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

    @api.depends("valid", "error_msg")
    def _compute_status_icon(self):
        for rec in self:
            if rec.valid:
                rec.status_icon = "✓ Resolved"
            elif rec.error_msg:
                rec.status_icon = "✗ Error"
            else:
                rec.status_icon = "✗ Unresolved"
