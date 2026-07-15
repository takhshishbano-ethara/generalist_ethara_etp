from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ._common import MCQ_TYPES, MANUAL_REVIEW_TYPES, OPTION_STORAGE_TYPES


class EtpApplicantAssessmentAnswer(models.Model):
    _name = "etp.applicant.assessment.answer"
    _description = "Applicant Assessment Answer"
    _order = "question_sequence, id"

    assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        required=True, ondelete="cascade", index=True,
    )
    question_id = fields.Many2one(
        "etp.applicant.assessment.question",
        required=True, ondelete="cascade", index=True,
    )
    question_sequence = fields.Integer(
        related="question_id.sequence", store=True,
    )
    question_type = fields.Selection(
        related="question_id.question_type", store=True,
    )
    max_score = fields.Integer(
        related="question_id.marks", store=True, string="Max Marks",
    )

    selected_option_ids = fields.Many2many(
        "etp.applicant.assessment.option",
        "etp_applicant_assessment_answer_option_rel",
        "answer_id", "option_id",
        string="Selected Options",
    )
    text_answer = fields.Text()
    answer_attachment_ids = fields.Many2many(
        "ir.attachment",
        "eaa_answer_attachment_rel",
        "answer_id", "attachment_id",
        string="Uploaded Files",
    )

    is_answered = fields.Boolean(
        compute="_compute_is_answered", store=True,
    )
    is_correct = fields.Boolean(
        compute="_compute_is_correct", store=True,
    )
    needs_review = fields.Boolean(
        compute="_compute_needs_review", store=True,
        help="Text/long-text answer awaiting HR manual scoring.",
    )

    manual_score = fields.Integer(default=0)
    manual_score_set = fields.Boolean(default=False)

    score = fields.Integer(
        compute="_compute_score", store=True, string="Awarded Marks",
    )
    submitted_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        (
            "assessment_question_uniq",
            "unique(assessment_id, question_id)",
            "One answer per question per assessment.",
        ),
    ]

    @api.depends("selected_option_ids", "text_answer", "answer_attachment_ids", "question_type")
    def _compute_is_answered(self):
        for rec in self:
            if rec.question_type in OPTION_STORAGE_TYPES:
                rec.is_answered = bool(rec.selected_option_ids)
            elif rec.question_type == "file_upload":
                rec.is_answered = bool(rec.answer_attachment_ids)
            else:
                rec.is_answered = bool(
                    rec.text_answer and rec.text_answer.strip()
                )

    @api.depends(
        "selected_option_ids",
        "question_id.option_ids.is_correct",
        "question_type",
    )
    def _compute_is_correct(self):
        for rec in self:
            if rec.question_type not in MCQ_TYPES:
                rec.is_correct = False
                continue
            correct = set(rec.question_id.option_ids.filtered("is_correct").ids)
            selected = set(rec.selected_option_ids.ids)
            rec.is_correct = bool(correct) and correct == selected

    @api.depends("question_type", "text_answer", "manual_score_set")
    def _compute_needs_review(self):
        for rec in self:
            if rec.question_type in MANUAL_REVIEW_TYPES:
                rec.needs_review = (
                    bool(rec.text_answer and rec.text_answer.strip())
                    and not rec.manual_score_set
                )
            else:
                rec.needs_review = False

    @api.depends(
        "is_correct", "is_answered",
        "question_id.marks", "question_id.negative_marks",
        "question_type",
        "manual_score", "manual_score_set",
    )
    def _compute_score(self):
        for rec in self:
            if rec.question_type in MCQ_TYPES:
                if rec.is_correct:
                    rec.score = rec.question_id.marks
                elif rec.is_answered:
                    rec.score = -rec.question_id.negative_marks
                else:
                    rec.score = 0
            elif rec.question_type in MANUAL_REVIEW_TYPES:
                rec.score = rec.manual_score if rec.manual_score_set else 0
            else:
                rec.score = 0

    @api.constrains("manual_score", "manual_score_set", "question_id")
    def _check_manual_score(self):
        for rec in self:
            if not rec.manual_score_set:
                continue
            if rec.manual_score < 0 or rec.manual_score > rec.question_id.marks:
                raise ValidationError(
                    _("Manual score must be between 0 and %d.")
                    % rec.question_id.marks
                )
