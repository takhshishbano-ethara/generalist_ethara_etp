from odoo import models, fields


class EtpAssessmentSkill(models.Model):
    _name = "etp.assessment.skill"
    _description = "Assessment Skill"
    _order = "sequence, name"

    name = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    description = fields.Text()
    tags = fields.Char(help="Comma-separated")
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
        ],
        required=True,
        default="mcq",
    )
    question_count = fields.Integer(default=5)
    time_minutes = fields.Integer(default=10)
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        default="medium",
    )
    source_resource_ids = fields.Many2many("etp.assessment.prompt.resource")
    extracted_from_prompt_id = fields.Many2one(
        "etp.assessment.prompt", ondelete="set null"
    )
    question_ids = fields.Many2many(
        "etp.assessment.question",
        relation="etp_assessment_question_skill_rel",
        column1="skill_id",
        column2="question_id",
        string="Bank Questions",
    )
    bank_question_count = fields.Integer(compute="_compute_bank_question_count")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "Skill names must be unique."),
    ]

    def _compute_bank_question_count(self):
        for rec in self:
            rec.bank_question_count = len(rec.question_ids)
