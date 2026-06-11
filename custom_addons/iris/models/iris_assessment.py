"""Iris assessment — take-home / FSD review as a SIDE artifact.

An assessment is a situational stage applied to one candidate. It carries
its OWN ``status`` lifecycle (draft → sent → submitted → reviewed) that is
fully independent of both ``candidate.state`` and ``llm_status``:

* the LLM produces a review DRAFT only (``action_generate_review_draft``) —
  it touches NEITHER ``status`` NOR ``candidate.state``;
* ``action_apply_draft`` deterministically copies draft sections into the
  structured feedback fields (empty fields only — human edits always win);
* only ``action_finalize_review`` (a human) moves the assessment to
  ``reviewed`` and renders the Feedback.md-shaped attachment.

Creation is guarded: the candidate must already be past screening
(shipped / interview_ready / interviewed / scored).
"""

import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import (
    prompt_loader,
    prompt_sanitizer,
    submission_extractor,
    verdict_parser,
)

_logger = logging.getLogger(__name__)

#: Candidate states in which an assessment may be created.
_ASSESSMENT_CANDIDATE_STATES = (
    "shipped", "interview_ready", "interviewed", "scored",
)

_RATING_LABELS = {
    "exceptional": "Exceptional",
    "above_average": "Above Average",
    "average": "Average",
    "below_average": "Below Average",
    "poor": "Poor",
}

_RECOMMENDATION_LABELS = {
    "hire": "Hire",
    "lean_hire": "Lean Hire",
    "lean_no_hire": "Lean No Hire",
    "no_hire": "No Hire",
}

#: Draft ``## Heading`` (lowercased) → structured field filled by
#: ``action_apply_draft``. The Recommendation section is matched by prefix
#: (its heading embeds the band, e.g. "Recommendation — Hire, with ...").
_DRAFT_SECTION_FIELDS = {
    "summary": "summary",
    "strengths": "strengths",
    "concerns": "concerns",
    "fit for current need": "fit_for_current_need",
}


class IrisAssessment(models.Model):
    _name = "iris.assessment"
    _description = "Iris Assessment"
    _inherit = ["iris.llm.job.mixin", "mail.thread"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate",
        required=True, ondelete="cascade", index=True,
    )
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("sent", "Sent"),
            ("submitted", "Submitted"),
            ("reviewed", "Reviewed"),
        ],
        string="Status",
        default="draft",
        tracking=True,
        copy=False,
        index=True,
        help="Assessment lifecycle — independent of the candidate pipeline "
             "state and of the LLM status.",
    )
    brief = fields.Text(
        string="Assessment Brief",
        help="The take-home / FSD brief sent to the candidate.",
    )
    sent_at = fields.Datetime(string="Sent At", readonly=True, copy=False)
    submission_file = fields.Binary(
        string="Submission File", attachment=True, copy=False,
    )
    submission_filename = fields.Char(string="Submission Filename", copy=False)
    submission_text = fields.Text(
        string="Submission Text", readonly=True, copy=False,
        help="Plain text extracted from the uploaded submission "
             "(PDF / docx / md / txt); this is what the LLM reads.",
    )
    submitted_at = fields.Datetime(string="Submitted At", readonly=True, copy=False)
    llm_draft_markdown = fields.Text(string="LLM Draft (Markdown)", copy=False)
    llm_draft_html = fields.Html(
        string="LLM Draft", compute="_compute_llm_draft_html", sanitize=False,
    )
    rating = fields.Selection(
        selection=[
            ("exceptional", "Exceptional"),
            ("above_average", "Above Average"),
            ("average", "Average"),
            ("below_average", "Below Average"),
            ("poor", "Poor"),
        ],
        string="Rating", copy=False, index=True,
    )
    summary = fields.Text(string="Summary", copy=False)
    strengths = fields.Text(string="Strengths", copy=False)
    concerns = fields.Text(string="Concerns", copy=False)
    fit_for_current_need = fields.Text(string="Fit for Current Need", copy=False)
    recommendation = fields.Selection(
        selection=[
            ("hire", "Hire"),
            ("lean_hire", "Lean Hire"),
            ("lean_no_hire", "Lean No Hire"),
            ("no_hire", "No Hire"),
        ],
        string="Recommendation", copy=False, index=True,
    )
    recommendation_conditions = fields.Text(
        string="Recommendation Conditions", copy=False,
    )
    reviewed_by = fields.Many2one(
        "res.users", string="Reviewed By", readonly=True, copy=False,
    )
    reviewed_at = fields.Datetime(string="Reviewed At", readonly=True, copy=False)
    attachment_id = fields.Many2one(
        "ir.attachment", string="Feedback Attachment", copy=False,
        help="Finalized Feedback.md-shaped render of the structured fields.",
    )
    draft_attachment_id = fields.Many2one(
        "ir.attachment", string="Draft Attachment", copy=False,
    )

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("candidate_id")
    def _check_candidate_state(self):
        for rec in self:
            if rec.candidate_id.state not in _ASSESSMENT_CANDIDATE_STATES:
                raise ValidationError(_(
                    "Assessments can only be created for candidates past "
                    "screening (Shipped / Interview Ready / Interviewed / "
                    "Scored). %(candidate)s is currently %(state)s.",
                    candidate=rec.candidate_id.name,
                    state=rec.candidate_id.state,
                ))

    # ------------------------------------------------------------------
    # CRUD / display
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec, vals in zip(records, vals_list):
            if vals.get("submission_file"):
                rec._process_submission(
                    vals["submission_file"], vals.get("submission_filename"),
                )
        return records

    def write(self, vals):
        if vals.get("submission_file") and any(
            rec.status == "reviewed" for rec in self
        ):
            raise UserError(_(
                "The submission cannot be replaced after the assessment "
                "has been reviewed."
            ))
        res = super().write(vals)
        if vals.get("submission_file"):
            for rec in self:
                rec._process_submission(
                    vals["submission_file"],
                    vals.get("submission_filename") or rec.submission_filename,
                )
        return res

    @api.depends("candidate_id.name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(candidate)s — Assessment",
                candidate=rec.candidate_id.name or "?",
            )

    @api.depends("llm_draft_markdown")
    def _compute_llm_draft_html(self):
        for rec in self:
            rec.llm_draft_html = rec._markdown_to_html(rec.llm_draft_markdown)

    # ------------------------------------------------------------------
    # Submission processing
    # ------------------------------------------------------------------
    def _process_submission(self, file_b64, filename):
        """Extract text from a freshly written submission file.

        From ``draft``/``sent`` the upload also moves the assessment to
        ``submitted``; a re-upload while ``submitted`` re-extracts in place.
        """
        self.ensure_one()
        if isinstance(file_b64, str):
            file_b64 = file_b64.encode("ascii")
        try:
            raw = base64.b64decode(file_b64)
        except Exception as exc:
            raise UserError(
                _("Could not decode the uploaded submission: %s") % exc
            ) from exc
        try:
            text = submission_extractor.extract_submission_text(
                raw, filename or "",
            )
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        vals = {
            "submission_text": text,
            "submitted_at": fields.Datetime.now(),
        }
        if self.status in ("draft", "sent"):
            vals["status"] = "submitted"
        self.sudo().write(vals)
        self.message_post(body=_(
            "Submission received (%s) — text extracted for review.",
        ) % (filename or _("unnamed file")))

    # ------------------------------------------------------------------
    # LLM template methods (draft-only: state machines untouched)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """ASSESSMENT_REVIEW prompt + brief / submission / resume inputs.

        The resume is included deliberately: the reviewer prompt calibrates
        claimed seniority against demonstrated level ("reads Senior,
        performs Mid"). All free-text inputs are fenced as untrusted data.
        """
        self.ensure_one()
        candidate = self.candidate_id
        system_prompt = prompt_loader.get_prompt(self.env, "assessment_review")
        today = fields.Date.context_today(self)
        lines = [
            f"TARGET ROLE / LEVEL:   {candidate.target_role or ''}",
            f"TODAY'S DATE:          {today.isoformat()}",
            "",
            "ASSESSMENT BRIEF:",
            prompt_sanitizer.fence_untrusted(
                "ASSESSMENT BRIEF", self.brief or "",
            ),
            "",
            "CANDIDATE SUBMISSION (extracted text — parsing artifacts possible):",
            prompt_sanitizer.fence_untrusted(
                "CANDIDATE SUBMISSION", self.submission_text or "",
            ),
            "",
            "CANDIDATE RESUME (context for seniority calibration):",
            prompt_sanitizer.fence_untrusted(
                "RESUME", candidate.resume_text or "",
            ),
        ]
        return system_prompt, "\n".join(lines)

    def _llm_on_success(self, content, meta):
        """Store the review DRAFT + attachment.

        Deliberately the simplest mixin user: touches NEITHER the
        assessment ``status`` NOR ``candidate.state`` — the LLM output is
        always a draft and only human copy/edit finalizes.
        """
        self.ensure_one()
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"assessment-draft-{self._candidate_lastname()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({
            "llm_draft_markdown": content,
            "draft_attachment_id": attachment.id,
        })
        self.message_post(body=_(
            "LLM review draft generated (%s). Copy it into the feedback "
            "fields, edit, then finalize.",
        ) % filename)

    def _llm_on_failure(self, msg):
        """Chatter only — no state machine is touched by a draft failure."""
        self.ensure_one()
        self.message_post(body=_(
            "LLM review draft generation failed (%s). The assessment is "
            "unchanged — retry from the Retry button.",
        ) % msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_send_brief(self):
        """Mark the brief as sent to the candidate (draft → sent)."""
        self.ensure_one()
        if self.status != "draft":
            raise UserError(_(
                "The brief can only be sent from Draft (current: %s)."
            ) % self.status)
        if not (self.brief or "").strip():
            raise UserError(_("Write the assessment brief before sending it."))
        self.write({"status": "sent", "sent_at": fields.Datetime.now()})
        self.message_post(body=_("Assessment brief sent to the candidate."))
        return True

    def action_generate_review_draft(self):
        """Queue the LLM review draft (submitted assessments only)."""
        self.ensure_one()
        if self.status != "submitted":
            raise UserError(_(
                "A review draft can only be generated once the submission "
                "is in (current status: %s)."
            ) % self.status)
        if not (self.submission_text or "").strip():
            raise UserError(_(
                "No submission text extracted — upload the candidate's "
                "submission file first."
            ))
        if self.llm_status in ("queued", "running"):
            raise UserError(_("A review draft is already being generated."))
        self._llm_enqueue()
        return True

    def action_apply_draft(self):
        """Deterministically copy draft sections into EMPTY feedback fields.

        Section bodies are split on the known ``## `` headings; the rating
        and recommendation are parsed via the verdict-parser helpers. A
        section or anchor that fails to parse simply fills nothing for
        that field — this action never raises on draft-format drift.
        """
        self.ensure_one()
        if self.status == "reviewed":
            raise UserError(_(
                "The feedback is locked — this assessment is already reviewed."
            ))
        md = self.llm_draft_markdown or ""
        if not md.strip():
            raise UserError(_(
                "No LLM draft to copy — generate the review draft first."
            ))

        vals = {}
        sections = self._split_draft_sections(md)
        for heading, field_name in _DRAFT_SECTION_FIELDS.items():
            if not (self[field_name] or "").strip() and sections.get(heading):
                vals[field_name] = sections[heading]
        if not (self.recommendation_conditions or "").strip():
            for heading, body in sections.items():
                if heading.startswith("recommendation") and body:
                    vals["recommendation_conditions"] = body
                    break
        if not self.rating:
            rating = verdict_parser.parse_assessment_rating(md)
            if rating:
                vals["rating"] = rating
        if not self.recommendation:
            recommendation = verdict_parser.parse_assessment_recommendation(md)
            if recommendation:
                vals["recommendation"] = recommendation

        if vals:
            self.write(vals)
            self.message_post(body=_(
                "Draft copied to feedback: %s filled (existing values were "
                "kept untouched).",
            ) % ", ".join(sorted(vals)))
        else:
            self.message_post(body=_(
                "Draft copy: nothing to fill — every feedback field already "
                "has a value or the draft sections could not be parsed."
            ))
        return True

    def action_finalize_review(self):
        """Human finalization: render Feedback.md, stamp reviewer, → reviewed."""
        self.ensure_one()
        if self.status != "submitted":
            raise UserError(_(
                "Only a Submitted assessment can be finalized (current: %s)."
            ) % self.status)
        if not self.rating or not self.recommendation:
            raise UserError(_(
                "Set both the Rating and the Recommendation before finalizing."
            ))
        if not (self.summary or "").strip():
            raise UserError(_("Write the Summary before finalizing."))

        feedback_md = self._render_feedback_markdown()
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"assessment-feedback-{self._candidate_lastname()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, feedback_md, self._name, self.id,
        )
        self.write({
            "status": "reviewed",
            "reviewed_by": self.env.user.id,
            "reviewed_at": fields.Datetime.now(),
            "attachment_id": attachment.id,
        })
        self.message_post(body=_(
            "Assessment review finalized (%s).") % filename)
        self.candidate_id.message_post(body=_(
            "Assessment reviewed: %(rating)s — %(recommendation)s.",
            rating=_RATING_LABELS.get(self.rating, self.rating),
            recommendation=_RECOMMENDATION_LABELS.get(
                self.recommendation, self.recommendation,
            ),
        ))
        return True

    def action_retry_llm(self):
        """Re-enqueue a failed review-draft generation (guarded)."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_("Only failed review drafts can be retried."))
        self._llm_enqueue()
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _split_draft_sections(self, md):
        """Split markdown on ``## `` headings → {lowercased heading: body}."""
        sections = {}
        current_key = None
        buf = []
        for line in (md or "").splitlines():
            if line.startswith("## "):
                if current_key is not None:
                    sections[current_key] = "\n".join(buf).strip()
                current_key = line[3:].strip().lower()
                buf = []
            elif current_key is not None:
                buf.append(line)
        if current_key is not None:
            sections[current_key] = "\n".join(buf).strip()
        return sections

    def _render_feedback_markdown(self):
        """Deterministic Feedback.md-shaped render of the structured fields."""
        self.ensure_one()
        date_str = fields.Date.context_today(self).isoformat()
        rating = _RATING_LABELS.get(self.rating, self.rating or "")
        band = _RECOMMENDATION_LABELS.get(
            self.recommendation, self.recommendation or "",
        )
        lines = [
            f"# Assessment Feedback — {self.candidate_id.name or ''}",
            "",
            f"- **Role:** {self.candidate_id.target_role or ''}",
            f"- **Date:** {date_str}",
            f"- **Rating:** {rating}",
            f"- **Recommendation:** **{band}**",
            "",
            "## Summary",
            "",
            self.summary or "",
            "",
            "## Strengths",
            "",
            self.strengths or "_None recorded._",
            "",
            "## Concerns",
            "",
            self.concerns or "_None recorded._",
            "",
            "## Fit for Current Need",
            "",
            self.fit_for_current_need or "_None recorded._",
            "",
            f"## Recommendation — {band}, with conditions",
            "",
            self.recommendation_conditions or "_No conditions recorded._",
            "",
        ]
        return "\n".join(lines)

    def _candidate_lastname(self):
        """Lowercased last whitespace token of the candidate name."""
        self.ensure_one()
        name = (self.candidate_id.name or "candidate").strip()
        return (name.split()[-1] if name.split() else "candidate").lower()
