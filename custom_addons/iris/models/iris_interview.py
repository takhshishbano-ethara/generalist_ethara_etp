"""Iris interview — LLM-generated interview guide + interviewer notes.

The guide is generated from QUESTIONS.md (5-question steering ladders +
rubrics) against the candidate's resume text once the candidate is SHIPPED.
The interviewer then records terse notes (UI or REST API); submitting them
moves the candidate to ``interviewed`` and unlocks scorecard generation.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import prompt_loader, prompt_sanitizer

_logger = logging.getLogger(__name__)


class IrisInterview(models.Model):
    _name = "iris.interview"
    _description = "Iris Interview"
    _inherit = ["iris.llm.job.mixin", "mail.thread"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate",
        required=True, ondelete="cascade", index=True,
    )
    screening_id = fields.Many2one(
        "iris.screening", string="Source Screening", readonly=True,
        help="The SHIP screening this interview was generated from.",
    )
    guide_markdown = fields.Text(string="Interview Guide (Markdown)", copy=False)
    guide_html = fields.Html(
        string="Interview Guide", compute="_compute_guide_html",
        sanitize=False,
    )
    attachment_id = fields.Many2one(
        "ir.attachment", string="Guide Attachment", copy=False,
    )
    notes = fields.Text(
        string="Interviewer Notes",
        help="Terse notes recorded during/after the interview; converted "
             "to a scorecard by the LLM.",
    )
    interviewer_id = fields.Many2one(
        "res.users", string="Interviewer (User)",
        help="Internal interviewer, when applicable.",
    )
    interviewer_name = fields.Char(
        string="Interviewer Name",
        help="Free-text interviewer name for external interviewers.",
    )
    interviewed_at = fields.Datetime(string="Interviewed At", copy=False)
    notes_submitted = fields.Boolean(string="Notes Submitted", copy=False)
    scorecard_ids = fields.One2many(
        "iris.scorecard", "interview_id", string="Scorecards",
    )

    # ------------------------------------------------------------------
    # Display / computes
    # ------------------------------------------------------------------
    @api.depends("candidate_id.name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(candidate)s — Interview",
                candidate=rec.candidate_id.name or "?",
            )

    @api.depends("guide_markdown")
    def _compute_guide_html(self):
        for rec in self:
            rec.guide_html = rec._markdown_to_html(rec.guide_markdown)

    # ------------------------------------------------------------------
    # LLM template methods
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """QUESTIONS.md system prompt (role-aware) + the fenced resume.

        The TARGET ROLE / LEVEL header line is trusted scaffolding; the
        resume is untrusted candidate data and enters the prompt only
        through the sanitizer's nonce fence.
        """
        self.ensure_one()
        candidate = self.candidate_id
        system_prompt = prompt_loader.get_prompt(
            self.env, "questions", role=candidate.role_id,
        )
        user_text = "\n".join([
            f"TARGET ROLE / LEVEL:   {candidate.target_role or ''}",
            "",
            "CANDIDATE RESUME:",
            prompt_sanitizer.fence_untrusted("RESUME", candidate.resume_text),
        ])
        return system_prompt, user_text

    def _llm_on_success(self, content, meta):
        """Store the guide, attach it, move the candidate to interview_ready."""
        self.ensure_one()
        candidate = self.candidate_id
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"interview-{self._candidate_lastname()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({
            "guide_markdown": content,
            "attachment_id": attachment.id,
        })
        candidate.sudo().write({"state": "interview_ready"})
        candidate.message_post(body=_(
            "Interview guide generated (%s).") % filename)

    def _llm_on_failure(self, msg):
        """Guide generation failed → candidate stays/reverts to shipped."""
        self.ensure_one()
        candidate = self.candidate_id
        candidate.sudo().write({"state": "shipped"})
        candidate.message_post(body=_(
            "Interview guide generation failed (%s). Candidate remains "
            "Shipped — retry from the interview record.") % msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_submit_notes(self):
        """Submit interviewer notes → candidate ``interviewed``."""
        self.ensure_one()
        if not (self.notes or "").strip():
            raise UserError(_("Interviewer notes are empty — nothing to submit."))
        self.write({
            "notes_submitted": True,
            # Preserve a client-supplied timestamp (API PUT /notes); only
            # default to now when none was provided.
            "interviewed_at": self.interviewed_at or fields.Datetime.now(),
            "interviewer_id": self.interviewer_id.id or self.env.user.id,
        })
        self.candidate_id.sudo().write({"state": "interviewed"})
        self.candidate_id.message_post(body=_(
            "Interview notes submitted by %s.") % (
                self.interviewer_name or self.env.user.name))
        return True

    def action_generate_scorecard(self):
        """Create + enqueue the scorecard for this interview's notes."""
        self.ensure_one()
        if not self.notes_submitted or not (self.notes or "").strip():
            raise UserError(_(
                "Submit non-empty interviewer notes before generating a "
                "scorecard."
            ))
        scorecard = self.env["iris.scorecard"].create({
            "interview_id": self.id,
        })
        scorecard._llm_enqueue()
        return scorecard

    def action_retry_llm(self):
        """Re-enqueue a failed guide generation (guarded)."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_("Only failed guide generations can be retried."))
        self._llm_enqueue()
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _candidate_lastname(self):
        """Lowercased last whitespace token of the candidate name."""
        self.ensure_one()
        name = (self.candidate_id.name or "candidate").strip()
        return (name.split()[-1] if name.split() else "candidate").lower()
