"""Verification-evidence wizard for HOLD candidates.

Records what was verified (reference call, public artifact, recruiter
follow-up, written response, ...) onto the candidate's current HOLD
screening, then optionally triggers the re-screen immediately.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class IrisEvidenceWizard(models.TransientModel):
    _name = "iris.evidence.wizard"
    _description = "Iris Record Verification Evidence"

    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate", required=True,
    )
    clarifying_questions = fields.Text(
        string="Clarifying Questions", readonly=True,
        help="Candidate-facing questions generated from the HOLD "
             "screening's verification checklist (v1.1). The candidate's "
             "written answers belong in the evidence field below.",
    )
    evidence = fields.Text(
        string="Verification Evidence", required=True,
        help="What was verified, how, and by whom — per the HOLD "
             "screening's verification checklist.",
    )
    rescreen_now = fields.Boolean(
        string="Re-screen Immediately", default=True,
        help="Queue the re-screen as soon as the evidence is recorded.",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if "clarifying_questions" in fields_list:
            candidate_id = (
                res.get("candidate_id")
                or self.env.context.get("default_candidate_id")
            )
            if candidate_id:
                candidate = (
                    self.env["iris.candidate"].browse(candidate_id).exists()
                )
                hold_screening = (
                    candidate._get_current_hold_screening()
                    if candidate else None
                )
                if hold_screening:
                    res["clarifying_questions"] = (
                        hold_screening.clarifying_questions_markdown or False
                    )
        return res

    def action_confirm(self):
        """Record the evidence on the current HOLD screening (+ re-screen)."""
        self.ensure_one()
        candidate = self.candidate_id
        if candidate.state != "hold":
            raise UserError(_(
                "Evidence can only be recorded while the candidate is on "
                "Hold (current: %s).") % candidate.state)
        hold_screening = candidate._get_current_hold_screening()
        if not hold_screening:
            raise UserError(_("No HOLD screening found for this candidate."))
        if not (self.evidence or "").strip():
            raise UserError(_("Verification evidence cannot be empty."))

        hold_screening.sudo().write({
            "verification_evidence": self.evidence,
            "evidence_recorded_by": self.env.user.id,
            "evidence_recorded_at": fields.Datetime.now(),
        })
        candidate.message_post(body=_(
            "Verification evidence recorded by %(user)s: %(evidence)s",
            user=self.env.user.name,
            evidence=self.evidence,
        ))
        if self.rescreen_now:
            candidate.action_rescreen()
        return {"type": "ir.actions.act_window_close"}
