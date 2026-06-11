"""Dual sign-off wizards for BLOCK verdicts (iris v1.1, P0-1).

Every human-visible BLOCK requires a second screener before the candidate
is finally blocked (SCREENING.md rule 8 as code). Two thin TransientModels:

* ``iris.block.signoff.wizard`` — the co-sign: a DIFFERENT screener
  confirms the BLOCK and picks the ``block_kind`` (credibility vs
  competence), which structurally selects the rejection-email template.
* ``iris.block.reject.wizard`` — the fail-safe: anyone (including the
  proposer) rejects the pending sign-off with a mandatory reason, routing
  the candidate back to Needs Review for a manager decision.

Both delegate to ``iris.candidate._block_signoff(block_kind)`` /
``_block_signoff_reject(reason)`` — the single code path shared with the
REST API; all state guards (pending_block, proposer ≠ co-signer, group
membership) live there.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class IrisBlockSignoffWizard(models.TransientModel):
    _name = "iris.block.signoff.wizard"
    _description = "Iris BLOCK Co-sign Wizard"

    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate", required=True,
    )
    block_kind = fields.Selection(
        selection=[
            ("credibility", "Credibility"),
            ("competence", "Competence"),
        ],
        string="Block Kind", required=True,
        help="Drives the rejection letter: Credibility → the neutral "
             "\"unable to reconcile statements\" letter; Competence → the "
             "experience-fit letter. The sender cannot pick the template "
             "manually.",
    )
    note = fields.Text(
        string="Sign-off Note",
        help="Optional note recorded in the candidate's chatter alongside "
             "the sign-off.",
    )

    def action_confirm(self):
        """Co-sign the pending BLOCK (guards live on the candidate)."""
        self.ensure_one()
        candidate = self.candidate_id
        candidate._block_signoff(self.block_kind)
        if (self.note or "").strip():
            candidate.message_post(body=_(
                "BLOCK sign-off note from %(user)s: %(note)s",
                user=self.env.user.name, note=self.note,
            ))
        return {"type": "ir.actions.act_window_close"}


class IrisBlockRejectWizard(models.TransientModel):
    _name = "iris.block.reject.wizard"
    _description = "Iris BLOCK Sign-off Rejection Wizard"

    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate", required=True,
    )
    reason = fields.Text(
        string="Rejection Reason", required=True,
        help="Why this BLOCK should not stand as-is — recorded on the "
             "screening and in the chatter. The candidate returns to "
             "Needs Review for a manager decision.",
    )

    def action_confirm(self):
        """Reject the pending BLOCK sign-off (reason is mandatory)."""
        self.ensure_one()
        if not (self.reason or "").strip():
            raise UserError(_("A rejection reason is required."))
        self.candidate_id._block_signoff_reject(self.reason)
        return {"type": "ir.actions.act_window_close"}
