"""Iris clarification — candidate-facing questions for a HOLD screening.

A sub-artifact of ``iris.screening`` (mixin ONLY, no chatter of its own —
it posts on the candidate): one row per LLM generation, so regenerations
never overwrite a prior run's audit trail. The latest successful run is
also denormalized onto ``screening_id.clarifying_questions_markdown``
(read by the HOLD verification email + the evidence wizard).

The trigger actions live on ``iris.screening`` / ``iris.candidate``
(another stream); this module only provides the artifact model:

* ``_llm_build_messages`` — CLARIFYING_QUESTIONS prompt + candidate
  name / target role + the HOLD screening record fenced as untrusted data;
* ``_llm_on_success`` — store the questions + denormalize onto the
  screening + chatter on the candidate (no attachment, no parsing, no
  needs_review — done is terminal);
* ``_llm_on_failure`` — chatter only (the candidate stays on Hold).
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import prompt_loader, prompt_sanitizer

_logger = logging.getLogger(__name__)


class IrisClarification(models.Model):
    _name = "iris.clarification"
    _description = "Iris Clarifying Questions"
    _inherit = "iris.llm.job.mixin"
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    screening_id = fields.Many2one(
        "iris.screening", string="HOLD Screening",
        required=True, ondelete="cascade", index=True,
    )
    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate",
        related="screening_id.candidate_id", store=True, index=True,
    )
    questions_markdown = fields.Text(
        string="Clarifying Questions (Markdown)", copy=False,
    )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @api.depends("candidate_id.name", "screening_id.attempt")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(candidate)s — Clarifying Questions (Screening #%(attempt)s)",
                candidate=rec.candidate_id.name or "?",
                attempt=rec.screening_id.attempt or rec.screening_id.id,
            )

    # ------------------------------------------------------------------
    # LLM template methods (parse-free: the question list IS the artifact)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """CLARIFYING_QUESTIONS prompt + fenced HOLD screening record."""
        self.ensure_one()
        candidate = self.candidate_id
        system_prompt = prompt_loader.get_prompt(
            self.env, "clarifying_questions",
        )
        lines = [
            f"CANDIDATE NAME:        {candidate.name or ''}",
            f"TARGET ROLE / LEVEL:   {candidate.target_role or ''}",
            "",
            "HOLD SCREENING RECORD:",
            prompt_sanitizer.fence_untrusted(
                "HOLD SCREENING RECORD",
                self.screening_id.markdown_record or "",
            ),
        ]
        return system_prompt, "\n".join(lines)

    def _llm_on_success(self, content, meta):
        """Store the questions + denormalize onto the screening.

        Regeneration overwrites the screening's denormalized copy (the
        latest set is what HR sends); prior sets stay auditable on their
        own clarification rows. Touches NO state machine.
        """
        self.ensure_one()
        self.sudo().write({"questions_markdown": content})
        self.screening_id.sudo().write({
            "clarifying_questions_markdown": content,
        })
        self.candidate_id.message_post(body=_(
            "Clarifying questions generated for HOLD screening "
            "#%(attempt)s — review them before contacting the candidate.",
            attempt=self.screening_id.attempt or self.screening_id.id,
        ))

    def _llm_on_failure(self, msg):
        """Chatter only — the candidate stays on Hold, nothing reverts."""
        self.ensure_one()
        self.candidate_id.message_post(body=_(
            "Clarifying-question generation failed (%s). The candidate "
            "remains on Hold — regenerate from the screening.",
        ) % msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry_llm(self):
        """Re-enqueue a failed clarifying-question generation (guarded)."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_(
                "Only failed clarifying-question runs can be retried."
            ))
        self._llm_enqueue()
        return True
