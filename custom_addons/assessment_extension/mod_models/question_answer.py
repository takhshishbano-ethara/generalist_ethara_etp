from odoo import api, fields, models
from odoo.exceptions import ValidationError


_EVAL_DIM_KEYS = (
    "instruction_following",
    "visual_quality",
    "less_ai_generated",
    "overall",
)


class ModAssessmentQuestionAnswer(models.Model):
    _name = "etp.assessment.question.answer"
    _description = "Question Correct / Wrong Answer"
    _order = "question_id, answer_role, id"
    _sql_constraints = [
        (
            "answer_role_unique_per_question",
            "UNIQUE(question_id, answer_role)",
            "A question can only have one 'correct' and one 'wrong' answer.",
        ),
    ]

    question_id = fields.Many2one(
        "etp.assessment.question",
        string="Question",
        required=True,
        ondelete="cascade",
        index=True,
    )
    answer_role = fields.Selection(
        [
            ("correct", "Correct"),
            ("wrong", "Wrong (Distractor)"),
        ],
        string="Role",
        required=True,
    )
    payload_json = fields.Json(
        string="Payload",
        help="Shape varies by question_type; see controller schema.",
    )

    @api.constrains("payload_json", "answer_role", "question_id")
    def _check_payload_shape(self):
        for rec in self:
            payload = rec.payload_json
            if not isinstance(payload, dict):
                # NULL / empty allowed during regeneration state.
                continue
            qtype = rec.question_id.question_type
            if qtype == "eval_compare":
                rec._check_eval_compare_payload(payload)
            elif qtype == "prompt_writing":
                rec._check_prompt_writing_payload(payload)

    def _check_eval_compare_payload(self, payload):
        picks = payload.get("picks")
        if not isinstance(picks, dict) or not picks:
            raise ValidationError(
                "Eval Compare answer payload must include a non-empty "
                "'picks' object keyed by dimension."
            )
        for key, value in picks.items():
            if value not in ("A", "B"):
                raise ValidationError(
                    "Eval Compare 'picks.%s' must be 'A' or 'B' (got %r)."
                    % (key, value)
                )

    def _check_prompt_writing_payload(self, payload):
        if self.answer_role == "correct":
            golden = payload.get("golden_prompt")
            if not isinstance(golden, str) or not golden.strip():
                raise ValidationError(
                    "Prompt Writing correct answer must include a "
                    "non-empty 'golden_prompt'."
                )
        else:
            thin = payload.get("thin_prompt")
            if not isinstance(thin, str) or not thin.strip():
                raise ValidationError(
                    "Prompt Writing wrong answer must include a "
                    "non-empty 'thin_prompt'."
                )

    @api.model
    def eval_compare_dimension_keys(self):
        return list(_EVAL_DIM_KEYS)
