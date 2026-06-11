from odoo import api, fields, models
from odoo.exceptions import ValidationError


_PROMPT_POINTERS_MIN = 8
_PROMPT_POINTERS_MAX = 10


def _ondelete_fallback_to_text(records):
    # On addon uninstall, demote rows using extension values to
    # the base 'text' type instead of cascading delete.
    records.write({"question_type": "text"})


class ModAssessmentQuestionExtension(models.Model):
    _inherit = "etp.assessment.question"

    question_type = fields.Selection(
        selection_add=[
            ("eval_compare", "Eval Compare"),
            ("prompt_writing", "Prompt Writing"),
            ("bbox_labeling", "BBox Labeling"),
        ],
        ondelete={
            "eval_compare": _ondelete_fallback_to_text,
            "prompt_writing": _ondelete_fallback_to_text,
            "bbox_labeling": _ondelete_fallback_to_text,
        },
    )
    answer_ids = fields.One2many(
        "etp.assessment.question.answer",
        "question_id",
        string="Answers (Correct / Wrong)",
    )
    pointer_ids = fields.One2many(
        "etp.assessment.question.pointer",
        "question_id",
        string="Prompt Pointers",
    )

    @api.constrains("question_type", "pointer_ids")
    def _check_prompt_writing_pointer_count(self):
        for rec in self:
            if rec.question_type != "prompt_writing":
                continue
            count = len(rec.pointer_ids)
            if not (_PROMPT_POINTERS_MIN <= count <= _PROMPT_POINTERS_MAX):
                raise ValidationError(
                    "Prompt-Writing question '%s' must have between %d "
                    "and %d pointers (currently %d)."
                    % (
                        rec.name or rec.display_name or rec.id,
                        _PROMPT_POINTERS_MIN,
                        _PROMPT_POINTERS_MAX,
                        count,
                    )
                )

    def _get_correct_answer(self):
        self.ensure_one()
        return self.answer_ids.filtered(
            lambda a: a.answer_role == "correct"
        )[:1]

    def _get_wrong_answer(self):
        self.ensure_one()
        return self.answer_ids.filtered(
            lambda a: a.answer_role == "wrong"
        )[:1]

    @api.model
    def prompt_pointer_range(self):
        return (_PROMPT_POINTERS_MIN, _PROMPT_POINTERS_MAX)
