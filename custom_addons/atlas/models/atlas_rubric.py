from odoo import models, fields, api


class AtlasRubricCriterion(models.Model):
    _name = "atlas.rubric.criterion"
    _description = "Atlas Rubric Criterion"
    _order = "sequence, id"

    atlas_id = fields.Many2one(
        "atlas.atlas", string="Task", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(string="Criteria Description", required=True)
    category = fields.Selection(
        [
            ("factuality_hallucination", "Factuality & Hallucination"),
            ("task_completion", "Task Completion"),
            ("instruction_following", "Instruction Following"),
            ("communication_style", "Communication Style"),
            ("other", "Other"),
        ],
        string="Category",
        default="other",
    )
    custom_category = fields.Char(string="Custom Category")
    importance = fields.Selection(
        [
            ("critically_detrimental", "Critically Detrimental"),
            ("detrimental", "Detrimental"),
            ("slightly_detrimental", "Slightly Detrimental"),
            ("slightly_important", "Slightly Important"),
            ("important", "Important"),
            ("critically_important", "Critically Important"),
        ],
        string="Importance",
    )
    weight = fields.Integer(string="Weight", default=5)
    is_negative = fields.Boolean(string="Negative Criterion", default=False)
    suggestion = fields.Text(string="Scoring Suggestion")
    sequence = fields.Integer(string="Sequence", default=10)
    level_ids = fields.One2many(
        "atlas.rubric.level", "criterion_id", string="Scoring Levels"
    )

    qc_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="QC Status",
    )
    qc_feedback = fields.Text(string="QC Feedback")
    qc_severity = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        string="QC Severity",
    )

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)


class AtlasRubricLevel(models.Model):
    _name = "atlas.rubric.level"
    _description = "Atlas Rubric Scoring Level"
    _order = "score, id"

    criterion_id = fields.Many2one(
        "atlas.rubric.criterion",
        string="Criterion",
        required=True,
        ondelete="cascade",
        index=True,
    )
    score = fields.Integer(string="Score", required=True, default=0)
    label = fields.Char(string="Label")
