# -*- coding: utf-8 -*-
"""Modal wizard used by the QC reviewer to approve/reject/rework a version."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class VideoQCReviewWizard(models.TransientModel):
    _name = "video.qc.review.wizard"
    _description = "Video QC Review Wizard"

    version_id = fields.Many2one("video.task.version", required=True)
    task_id = fields.Many2one(related="version_id.task_id", readonly=True)
    decision = fields.Selection(
        [
            ("approved", "Approve"),
            ("rejected", "Reject"),
            ("rework", "Request Rework"),
        ],
        required=True,
        default="approved",
    )
    comment = fields.Text(string="Reviewer Comment")
    next_prompt = fields.Text(
        string="Suggested Next Prompt",
        help="Optional prompt suggestion attached to the next version if rework is requested.",
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if active_model == "video.task.version" and active_id:
            values["version_id"] = active_id
        return values

    def action_apply(self):
        self.ensure_one()
        if not self.version_id:
            raise UserError(_("No version selected for QC."))
        if self.decision == "approved":
            self.version_id.action_qc_approve(self.comment)
        elif self.decision == "rejected":
            self.version_id.action_qc_reject(self.comment)
        elif self.decision == "rework":
            self.version_id.action_qc_rework(self.comment)
            # Pre-create the next version so the editor can immediately resume.
            new_version = self.task_id.create_new_version(
                vals={"prompt_text": self.next_prompt or self.version_id.prompt_text}
            )
            return {
                "type": "ir.actions.act_window",
                "name": _("Continue editing"),
                "res_model": "video.task.version",
                "res_id": new_version.id,
                "view_mode": "form",
                "target": "current",
            }
        return {"type": "ir.actions.act_window_close"}
