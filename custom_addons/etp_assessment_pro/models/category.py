from odoo import models, fields, api


class EtpAssessmentCategory(models.Model):
    _name = "etp.assessment.pro.category"
    _description = "Assessment Question Category"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text()
    question_ids = fields.One2many(
        "etp.assessment.pro.question", "category_id", string="Questions"
    )
    question_count = fields.Integer(
        compute="_compute_question_count", string="# Questions"
    )
    add_question_ids = fields.Many2many(
        "etp.assessment.pro.question",
        relation="etp_assessment_pro_category_addq_rel",
        column1="category_id",
        column2="question_id",
        string="Add Questions From Bank",
        domain=[("active", "=", True)],
        help="Pick existing bank questions to move into this category; "
        "filter by skill / type / difficulty in the search. "
        "Because a question belongs to a single category, adding it here "
        "MOVES it into this category (removing it from any previous one). "
        "Click 'Add Selected to Category' to apply.",
    )

    def _compute_question_count(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)

    def action_add_questions_from_bank(self):
        """Reparent the picked bank questions into this category, then clear
        the transient picker so it never accumulates / duplicates data."""
        self.ensure_one()
        if self.add_question_ids:
            self.add_question_ids.write({"category_id": self.id})
            self.add_question_ids = [(5, 0, 0)]
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }
