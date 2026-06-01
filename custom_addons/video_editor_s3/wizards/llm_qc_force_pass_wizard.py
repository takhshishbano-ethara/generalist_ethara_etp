# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class LLMQCForcePassWizard(models.TransientModel):
    _name = "video.editor.llm.qc.force.pass.wizard"
    _description = "LLM QC Force Pass Wizard"

    project_id = fields.Many2one(
        "video.editor.project",
        string="Project",
        required=True,
        ondelete="cascade",
        readonly=True,
    )
    original_verdict = fields.Selection(
        related="project_id.llm_qc_result",
        string="Reviewer Verdict",
        readonly=True,
    )
    original_failure_reason = fields.Text(
        related="project_id.llm_failure_reason",
        string="Reviewer Failure Reason",
        readonly=True,
    )
    reason = fields.Char(
        string="Why force-pass this QC?",
        required=True,
        help=(
            "Recorded on the project (Force-Pass Reason field) and posted to "
            "the project chatter. Future reviewers will see this rationale."
        ),
    )

    def action_confirm(self):
        self.ensure_one()
        reason = (self.reason or "").strip()
        if not reason:
            raise UserError(_("Please provide a reason for the force pass."))
        self.project_id.with_context(
            default_llm_qc_force_pass_reason=reason
        ).action_force_pass_llm_qc()
        return {"type": "ir.actions.client", "tag": "soft_reload"}
