from __future__ import annotations

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


_MODE_TO_VERDICT = {
    "accept": "accept",
    "reject": "reject",
    "review": "review",
    "clear": False,
}


class T2AVReviewOverrideWizard(models.TransientModel):
    _name = "t2av.review.override.wizard"
    _description = "T2AV Video Review Human-Verdict Override Wizard"

    review_id = fields.Many2one(
        "t2av.video.review", string="Review",
        required=True, ondelete="cascade",
    )
    mode = fields.Selection(
        [
            ("accept", "Mark Accepted"),
            ("reject", "Mark Rejected"),
            ("review", "Mark Review"),
            ("clear", "Clear Override"),
        ],
        string="Action", required=True,
    )
    current_model_verdict = fields.Selection(
        related="review_id.verdict", string="Model Verdict", readonly=True,
    )
    current_human_verdict = fields.Selection(
        related="review_id.human_verdict", string="Current Human Verdict",
        readonly=True,
    )
    current_effective_verdict = fields.Selection(
        related="review_id.effective_verdict", string="Current Effective Verdict",
        readonly=True,
    )
    reason = fields.Text(string="Reason", required=True)

    @api.constrains("reason")
    def _check_reason(self):
        for rec in self:
            if not (rec.reason or "").strip():
                raise ValidationError(_(
                    "An override reason is required for the audit trail."
                ))

    def action_apply(self):
        self.ensure_one()
        if self.mode not in _MODE_TO_VERDICT:
            raise ValidationError(_(
                "Unknown override mode %(m)r."
            ) % {"m": self.mode})
        target_verdict = _MODE_TO_VERDICT[self.mode]
        self.review_id._apply_human_override(target_verdict, self.reason)
        return {"type": "ir.actions.act_window_close"}
