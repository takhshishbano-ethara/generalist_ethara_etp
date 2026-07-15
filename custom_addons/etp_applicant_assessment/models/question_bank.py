from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ._common import QUESTION_TYPE_SELECTION, MCQ_TYPES


class EtpApplicantAssessmentQuestionBank(models.Model):
    _name = "etp.applicant.assessment.question.bank"
    _description = "Assessment Question Bank"
    _order = "id desc"

    name = fields.Char(
        required=True,
        help="Short admin-facing title for this question, e.g. 'GIL - long answer'.",
    )
    active = fields.Boolean(default=True)
    prompt = fields.Text(required=True, string="Question")
    question_type = fields.Selection(
        QUESTION_TYPE_SELECTION,
        default="mcq_single",
        required=True,
    )
    marks = fields.Integer(default=1, required=True)
    negative_marks = fields.Integer(
        default=0, required=True,
        help="Marks deducted on a wrong answer (0 disables negative marking).",
    )
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        default="medium",
        help="Record-keeping only; not used for scoring.",
    )
    tags = fields.Char(
        string="Tags",
        help="Comma-separated tags for organising the bank (record-keeping only).",
    )
    skills = fields.Char(
        string="Skills",
        help="Comma-separated skills exercised by this question (record-keeping only).",
    )
    option_ids = fields.One2many(
        "etp.applicant.assessment.question.bank.option",
        "bank_question_id",
        string="Options",
    )

    @api.constrains("marks")
    def _check_marks(self):
        for rec in self:
            if rec.marks <= 0:
                raise ValidationError("Marks must be a positive integer.")

    @api.constrains("negative_marks")
    def _check_negative_marks(self):
        for rec in self:
            if rec.negative_marks < 0:
                raise ValidationError("Negative marks cannot be less than 0.")

    @api.constrains("question_type", "option_ids")
    def _check_options_for_mcq(self):
        for rec in self:
            if rec.question_type in MCQ_TYPES:
                if len(rec.option_ids) < 2:
                    raise ValidationError(
                        "MCQ-style questions need at least two options."
                    )
                if not any(o.is_correct for o in rec.option_ids):
                    raise ValidationError(
                        "MCQ-style questions need at least one correct option."
                    )


class EtpApplicantAssessmentQuestionBankOption(models.Model):
    _name = "etp.applicant.assessment.question.bank.option"
    _description = "Assessment Question Bank Option"
    _order = "sequence, id"

    bank_question_id = fields.Many2one(
        "etp.applicant.assessment.question.bank",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    label = fields.Char(required=True)
    is_correct = fields.Boolean(default=False)
