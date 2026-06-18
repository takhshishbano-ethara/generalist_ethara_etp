from odoo import models, fields, api
from odoo.exceptions import ValidationError


class EtpAssessmentQuestionDimension(models.Model):
    _name = "etp.assessment.question.dimension"
    _description = "Question Dimension Link"
    _order = "sequence, id"
    _rec_name = "dimension_id"

    question_id = fields.Many2one(
        "etp.assessment.question", required=True, ondelete="cascade"
    )
    dimension_id = fields.Many2one(
        "etp.assessment.dimension",
        string="Dimension",
        required=True,
        ondelete="restrict",
    )
    sequence = fields.Integer(default=10)
    option_line_ids = fields.One2many(
        "etp.assessment.question.dimension.option",
        "question_dimension_id",
        string="Options",
    )

    @api.constrains("question_id", "dimension_id")
    def _check_unique_dimension_per_question(self):
        for rec in self:
            duplicate = self.search_count([
                ("question_id", "=", rec.question_id.id),
                ("dimension_id", "=", rec.dimension_id.id),
                ("id", "!=", rec.id),
            ])
            if duplicate:
                raise ValidationError(
                    "Dimension '%s' is already assigned to this question."
                    % rec.dimension_id.name
                )

    @api.onchange("dimension_id")
    def _onchange_dimension_id(self):
        if self.dimension_id:
            lines = [
                (0, 0, {
                    "master_option_id": opt.id,
                    "sequence": opt.sequence,
                    "is_correct": False,
                })
                for opt in self.dimension_id.option_ids.sorted("sequence")
            ]
            self.option_line_ids = [(5, 0, 0)] + lines
        else:
            self.option_line_ids = [(5, 0, 0)]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(lambda r: not r.option_line_ids)._populate_options()
        return records

    def _populate_options(self):
        OptionModel = self.env["etp.assessment.question.dimension.option"]
        for rec in self:
            existing_master_ids = rec.option_line_ids.mapped("master_option_id").ids
            vals_to_create = [
                {
                    "question_dimension_id": rec.id,
                    "master_option_id": opt.id,
                    "sequence": opt.sequence,
                }
                for opt in rec.dimension_id.option_ids.sorted("sequence")
                if opt.id not in existing_master_ids
            ]
            if vals_to_create:
                OptionModel.create(vals_to_create)


class EtpAssessmentQuestionDimensionOption(models.Model):
    _name = "etp.assessment.question.dimension.option"
    _description = "Question Dimension Option"
    _order = "sequence, id"

    question_dimension_id = fields.Many2one(
        "etp.assessment.question.dimension", required=True, ondelete="cascade"
    )
    master_option_id = fields.Many2one(
        "etp.assessment.dimension.option",
        string="Source Option",
        required=True,
        ondelete="restrict",
    )
    name = fields.Char(related="master_option_id.name", store=True, readonly=True)
    sequence = fields.Integer(default=10)
    is_correct = fields.Boolean(string="Correct", default=False)
    score = fields.Integer(string="Score", compute="_compute_score", store=True)

    @api.depends("is_correct")
    def _compute_score(self):
        for rec in self:
            rec.score = 1 if rec.is_correct else 0
