from odoo import fields, models
from odoo.exceptions import UserError

from ._common import QUESTION_TYPE_SELECTION


class EtpApplicantAssessmentBankPicker(models.TransientModel):
    _name = "etp.applicant.assessment.bank.picker"
    _description = "Pick Questions from Bank into a Template Section"

    template_id = fields.Many2one(
        "etp.applicant.assessment.template",
        required=True,
        ondelete="cascade",
    )
    section_id = fields.Many2one(
        "etp.applicant.assessment.template.section",
        string="Section",
        domain="[('template_id', '=', template_id)]",
        help="Leave empty to attach the picked questions to the template's default bucket.",
    )
    bank_question_ids = fields.Many2many(
        "etp.applicant.assessment.question.bank",
        relation="etp_bank_picker_bank_question_rel",
        column1="picker_id",
        column2="bank_question_id",
        string="Questions to Add",
        domain=[("active", "=", True)],
    )
    filter_type = fields.Selection(
        selection=QUESTION_TYPE_SELECTION,
        string="Filter by Type",
    )
    filter_difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        string="Filter by Difficulty",
    )
    filter_tags = fields.Char(
        string="Filter by Tags",
        help="Substring match against the comma-separated tags on bank questions.",
    )
    filter_skills = fields.Char(
        string="Filter by Skills",
        help="Substring match against the comma-separated skills on bank questions.",
    )

    def action_add_to_template(self):
        self.ensure_one()
        if not self.bank_question_ids:
            raise UserError("Select at least one question to add.")

        Question = self.env["etp.applicant.assessment.template.question"]
        Option = self.env["etp.applicant.assessment.template.option"]

        if self.section_id:
            existing = self.section_id.question_ids
        else:
            existing = self.template_id.question_ids.filtered(
                lambda q: not q.section_id
            )
        last_seq = max(existing.mapped("sequence")) if existing else 0
        step = 10

        for src in self.bank_question_ids:
            last_seq += step
            new_q = Question.create({
                "template_id": self.template_id.id,
                "section_id": self.section_id.id if self.section_id else False,
                "sequence": last_seq,
                "prompt": src.prompt,
                "question_type": src.question_type,
                "marks": src.marks,
                "negative_marks": src.negative_marks,
                "bank_question_id": src.id,
            })
            for opt in src.option_ids:
                Option.create({
                    "question_id": new_q.id,
                    "sequence": opt.sequence,
                    "label": opt.label,
                    "is_correct": opt.is_correct,
                })

        return {"type": "ir.actions.act_window_close"}
