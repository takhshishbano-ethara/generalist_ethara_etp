from odoo import api, fields, models


class EtpApplicantAssessmentSection(models.Model):
    _name = "etp.applicant.assessment.section"
    _description = "Applicant Assessment Section (Snapshot)"
    _order = "sequence, id"

    assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, string="Section Title")
    description = fields.Char(string="Section Description")
    question_ids = fields.One2many(
        "etp.applicant.assessment.question",
        "section_id",
        string="Questions",
    )
    question_count = fields.Integer(
        compute="_compute_rollups", store=True,
    )

    @api.depends("question_ids")
    def _compute_rollups(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)
