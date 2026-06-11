"""Iris scorecard — LLM conversion of guide + interviewer notes to a verdict.

The interviewer notes are snapshotted into ``notes_snapshot`` at create time
(the live ``notes`` field on the interview may be edited later) and that
snapshot is what the LLM scores. The parsed recommendation drives the
candidate to ``scored``; an unparseable recommendation routes to
``needs_review`` with a manager manual-recommendation override.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import prompt_loader, prompt_sanitizer, verdict_parser

_logger = logging.getLogger(__name__)

_RECOMMENDATION_LABELS = {
    "strong_hire": "Strong Hire",
    "hire": "Hire",
    "no_hire": "No Hire",
    "strong_no_hire": "Strong No Hire",
}


class IrisScorecard(models.Model):
    _name = "iris.scorecard"
    _description = "Iris Scorecard"
    _inherit = ["iris.llm.job.mixin", "mail.thread"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    interview_id = fields.Many2one(
        "iris.interview", string="Interview",
        required=True, ondelete="cascade", index=True,
    )
    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate",
        related="interview_id.candidate_id", store=True, index=True,
    )
    notes_snapshot = fields.Text(
        string="Notes Snapshot", readonly=True, copy=False,
        help="Audit copy of the interviewer notes at scorecard creation; "
             "this is exactly what the LLM scored.",
    )
    scorecard_markdown = fields.Text(string="Scorecard (Markdown)", copy=False)
    scorecard_html = fields.Html(
        string="Scorecard", compute="_compute_scorecard_html",
        sanitize=False,
    )
    recommendation = fields.Selection(
        selection=[
            ("strong_hire", "Strong Hire"),
            ("hire", "Hire"),
            ("no_hire", "No Hire"),
            ("strong_no_hire", "Strong No Hire"),
        ],
        string="Recommendation", copy=False, index=True,
    )
    recommendation_manual = fields.Boolean(
        string="Manual Recommendation", copy=False,
        help="Set when a manager resolved this scorecard from Needs Review.",
    )
    attachment_id = fields.Many2one(
        "ir.attachment", string="Scorecard Attachment", copy=False,
    )

    # ------------------------------------------------------------------
    # CRUD / display
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Interview = self.env["iris.interview"]
        for vals in vals_list:
            if not vals.get("notes_snapshot") and vals.get("interview_id"):
                vals["notes_snapshot"] = Interview.browse(
                    vals["interview_id"]
                ).notes or ""
        return super().create(vals_list)

    @api.depends("candidate_id.name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(candidate)s — Scorecard",
                candidate=rec.candidate_id.name or "?",
            )

    @api.depends("scorecard_markdown")
    def _compute_scorecard_html(self):
        for rec in self:
            rec.scorecard_html = rec._markdown_to_html(rec.scorecard_markdown)

    # ------------------------------------------------------------------
    # LLM template methods
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """SCORECARD.md system prompt (role-aware) + fenced guide and notes.

        Both inputs are fenced as untrusted data: the guide is itself LLM
        output built from candidate-supplied resume text (2nd-order
        injection surface), and interviewer notes are free human text.
        """
        self.ensure_one()
        system_prompt = prompt_loader.get_prompt(
            self.env, "scorecard", role=self.candidate_id.role_id,
        )
        user_text = "\n".join([
            "INTERVIEW GUIDE USED:",
            prompt_sanitizer.fence_untrusted(
                "INTERVIEW GUIDE", self.interview_id.guide_markdown,
            ),
            "",
            "INTERVIEWER NOTES:",
            prompt_sanitizer.fence_untrusted(
                "INTERVIEWER NOTES", self.notes_snapshot,
            ),
        ])
        return system_prompt, user_text

    def _llm_on_success(self, content, meta):
        """Store the scorecard, parse the recommendation, drive the state."""
        self.ensure_one()
        candidate = self.candidate_id
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"scorecard-{self._candidate_lastname()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({
            "scorecard_markdown": content,
            "attachment_id": attachment.id,
        })

        recommendation = verdict_parser.parse_recommendation(content)
        if recommendation is None:
            # Candidate deliberately STAYS in `interviewed`: scorecard
            # review is artifact-level, not a candidate pipeline halt.
            self.sudo().write({"llm_status": "needs_review"})
            candidate.message_post(body=_(
                "Scorecard generated but the recommendation could not be "
                "parsed — manager review required on the scorecard."
            ))
            return

        self.sudo().write({"recommendation": recommendation})
        candidate.sudo().write({"state": "scored"})
        candidate.message_post(body=_(
            "Scorecard recommendation: %s") % _RECOMMENDATION_LABELS[recommendation])

    def _llm_on_failure(self, msg):
        """Scorecard generation failed → candidate stays/reverts to interviewed."""
        self.ensure_one()
        candidate = self.candidate_id
        candidate.sudo().write({"state": "interviewed"})
        candidate.message_post(body=_(
            "Scorecard generation failed (%s). Candidate remains "
            "Interviewed — retry from the scorecard record.") % msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_manual_recommendation(self):
        """Manager override when the recommendation could not be parsed.

        The manager picks the ``recommendation`` value on the form, then
        confirms with this button: it validates the manager group, marks
        the scorecard done + manual, and moves the candidate to ``scored``.
        """
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can set a manual recommendation."
            ))
        if self.llm_status != "needs_review":
            raise UserError(_(
                "Manual recommendations are only available from Needs Review."
            ))
        if not self.recommendation:
            raise UserError(_(
                "Select a recommendation before confirming."
            ))
        self.sudo().write({
            "recommendation_manual": True,
            "llm_status": "done",
        })
        self.candidate_id.sudo().write({"state": "scored"})
        self.candidate_id.message_post(body=_(
            "Manual scorecard recommendation set by %(user)s: %(rec)s",
            user=self.env.user.name,
            rec=_RECOMMENDATION_LABELS.get(self.recommendation, self.recommendation),
        ))
        return True

    def action_retry_llm(self):
        """Re-enqueue a failed scorecard generation (guarded)."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_("Only failed scorecards can be retried."))
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
