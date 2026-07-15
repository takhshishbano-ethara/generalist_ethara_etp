from odoo import fields, models

from ._common import QUESTION_TYPE_SELECTION


class EtpApplicantAssessmentQuestion(models.Model):
    _name = "etp.applicant.assessment.question"
    _description = "Applicant Assessment Question (Snapshot)"
    _order = "sequence, id"

    assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        required=True, ondelete="cascade", index=True,
    )
    section_id = fields.Many2one(
        "etp.applicant.assessment.section",
        string="Section", ondelete="set null", index=True,
    )
    sequence = fields.Integer(default=10)
    prompt = fields.Text(required=True, string="Question")
    question_type = fields.Selection(
        QUESTION_TYPE_SELECTION,
        required=True,
    )
    marks = fields.Integer(default=1, required=True)
    negative_marks = fields.Integer(default=0, required=True)
    option_ids = fields.One2many(
        "etp.applicant.assessment.option", "question_id",
        string="Options",
    )


class EtpApplicantAssessmentOption(models.Model):
    _name = "etp.applicant.assessment.option"
    _description = "Applicant Assessment Option (Snapshot)"
    _order = "sequence, id"

    question_id = fields.Many2one(
        "etp.applicant.assessment.question",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    label = fields.Char(required=True)
    is_correct = fields.Boolean(default=False)
