from odoo import fields, models


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    candidate_user_id = fields.Many2one(
        "res.users",
        string="Candidate Portal User",
        ondelete="set null",
        index=True,
        copy=False,
    )
