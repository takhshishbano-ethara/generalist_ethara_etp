from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class CrowleyAttemptReviewWizard(models.TransientModel):
    _name = "crowley.attempt.review.wizard"
    _description = "Approve or Reject a Crowley Video Attempt"

    attempt_id = fields.Many2one(
        "crowley.attempt",
        string="Attempt",
        required=True,
        ondelete="cascade",
    )
    review_action = fields.Selection(
        [("approve", "Approve"), ("reject", "Reject")],
        string="Action",
        required=True,
    )
    review_reason = fields.Text(
        string="Notes",
        help="Required for rejections, optional for approvals.",
    )

    @api.constrains("review_action", "review_reason")
    def _check_reject_reason(self):
        for rec in self:
            if rec.review_action == "reject" and not (rec.review_reason or "").strip():
                raise ValidationError(_("A reason is required when rejecting an attempt."))

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group("crowley.group_crowley_manager"):
            raise UserError(_("Only Crowley Managers can review attempts."))
        attempt = self.attempt_id
        if not attempt.exists():
            raise UserError(_("Attempt no longer exists."))
        self.env.cr.execute(
            "SELECT review_state FROM crowley_attempt WHERE id = %s FOR UPDATE",
            (attempt.id,),
        )
        row = self.env.cr.fetchone()
        if not row or row[0] != "pending":
            raise UserError(_(
                "This attempt has already been reviewed (%(status)s).",
                status=row[0] if row else "deleted",
            ))
        attempt.invalidate_recordset(["review_state"])

        new_state = "approved" if self.review_action == "approve" else "rejected"
        attempt.write({
            "review_state": new_state,
            "reviewed_by": self.env.user.id,
            "reviewed_at": fields.Datetime.now(),
            "review_reason": self.review_reason or False,
        })

        if new_state == "approved":
            body = _("Approved by %(user)s.", user=self.env.user.display_name)
            if self.review_reason:
                body += " " + _("Notes: %(reason)s", reason=self.review_reason)
        else:
            body = _(
                "Rejected by %(user)s. Reason: %(reason)s",
                user=self.env.user.display_name,
                reason=self.review_reason,
            )
        attempt.job_id.message_post(body=body)

        return {"type": "ir.actions.client", "tag": "reload"}
