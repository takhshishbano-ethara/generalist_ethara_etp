from odoo import fields, models


class ModAssessmentQuestionPointer(models.Model):
    _name = "etp.assessment.question.pointer"
    _description = "Prompt-Writing Pointer"
    _order = "question_id, sequence, id"

    question_id = fields.Many2one(
        "etp.assessment.question",
        string="Question",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(
        string="Sequence",
        default=10,
        help="Display order inside the pointers checklist.",
    )
    text = fields.Char(
        string="Pointer Text",
        required=True,
        help="One bullet on the prompt-writing pointers checklist.",
    )

    def to_api_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "sequence": self.sequence or 0,
            "text": self.text or "",
        }
