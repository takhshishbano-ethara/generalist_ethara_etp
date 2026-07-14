from odoo import models, fields, api
from odoo.exceptions import ValidationError


class EtpAssessmentQuestionDimension(models.Model):
    _name = "etp.assessment.pro.question.dimension"
    _description = "Question Dimension Link"
    _order = "sequence, id"
    _rec_name = "name"

    question_id = fields.Many2one(
        "etp.assessment.pro.question", required=True, ondelete="cascade"
    )
    name = fields.Char(string="Dimension", required=True)
    sequence = fields.Integer(default=10)
    option_line_ids = fields.One2many(
        "etp.assessment.pro.question.dimension.option",
        "question_dimension_id",
        string="Options",
    )

    @api.constrains("question_id", "name")
    def _check_unique_dimension_per_question(self):
        for rec in self:
            duplicate = self.search_count([
                ("question_id", "=", rec.question_id.id),
                ("name", "=", rec.name),
                ("id", "!=", rec.id),
            ])
            if duplicate:
                raise ValidationError(
                    "Dimension '%s' is already assigned to this question."
                    % rec.name
                )


class EtpAssessmentQuestionDimensionOption(models.Model):
    _name = "etp.assessment.pro.question.dimension.option"
    _description = "Question Dimension Option"
    _order = "sequence, id"

    question_dimension_id = fields.Many2one(
        "etp.assessment.pro.question.dimension", required=True, ondelete="cascade"
    )
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    is_correct = fields.Boolean(string="Correct", default=False)
    score = fields.Integer(string="Score", compute="_compute_score", store=True)

    @api.depends("is_correct")
    def _compute_score(self):
        for rec in self:
            rec.score = 1 if rec.is_correct else 0
