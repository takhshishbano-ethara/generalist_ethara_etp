# -*- coding: utf-8 -*-
"""Modal wizard used by the QC reviewer to approve/reject/rework a version.

Two grammar-check panels are wired in via the ``video.qc.grammar.checker``
service: one for the version's stored ``prompt_text`` and one for the
reviewer's ``next_prompt`` suggestion.  Clicking the "Check Grammar"
button calls Kimi K2.5 for whichever panel has content; the resulting
score, summary, issues, and corrected text are stored on the wizard
and rendered in the form.

When the grammar score for the prompt that's about to be approved
(i.e. the version's ``prompt_text``) is below the configured
threshold (``video_qc.grammar_score_threshold``, default 70), the
``Approve`` verdict is blocked: the reviewer either fixes the prompt
via Attach Prompt and re-runs the check, or picks Reject / Request
Rework instead.
"""

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

    # ------------------------------------------------------------------
    # Grammar-check (Kimi K2.5)
    #
    # Two panels: one grades the version's existing prompt (the text
    # that's about to be approved/rejected); the other grades the
    # reviewer's suggested rewrite.  Approve is blocked when the
    # CURRENT prompt's score is below ``grammar_score_threshold``.
    # ------------------------------------------------------------------
    grammar_check_done = fields.Boolean(
        string="Grammar Check Run",
        readonly=True,
        help="Set automatically the first time the wizard runs the "
             "Kimi grammar check.  Used by the form to hide the "
             "default 'awaiting check' banner once results are in.",
    )
    grammar_score_threshold = fields.Integer(
        string="Approve Threshold",
        compute="_compute_grammar_threshold",
        help="Approve is blocked when the prompt's grammar score is "
             "below this value (default 70).",
    )
    is_approve_blocked = fields.Boolean(
        compute="_compute_is_approve_blocked",
        help="True when Approve must be hidden because the prompt's "
             "grammar score is below the configured threshold.",
    )

    prompt_text_preview = fields.Text(
        string="Version Prompt",
        related="version_id.prompt_text",
        readonly=True,
    )

    # --- Current prompt panel
    prompt_grammar_score = fields.Integer(string="Prompt Score", readonly=True)
    prompt_grammar_summary = fields.Text(string="Prompt Summary", readonly=True)
    prompt_grammar_issues = fields.Text(
        string="Prompt Issues",
        readonly=True,
        help="One issue per line as reported by Kimi.",
    )
    prompt_grammar_corrected = fields.Text(
        string="Prompt — Corrected",
        readonly=True,
        help="Kimi's rewrite of the version prompt with grammar fixes applied.",
    )

    # --- Next-prompt panel
    next_prompt_grammar_score = fields.Integer(string="Next-Prompt Score", readonly=True)
    next_prompt_grammar_summary = fields.Text(string="Next-Prompt Summary", readonly=True)
    next_prompt_grammar_issues = fields.Text(string="Next-Prompt Issues", readonly=True)
    next_prompt_grammar_corrected = fields.Text(
        string="Next-Prompt — Corrected",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Defaults / computes
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if active_model == "video.task.version" and active_id:
            values["version_id"] = active_id
        return values

    @api.depends_context("uid")
    def _compute_grammar_threshold(self):
        threshold = self.env["video.qc.grammar.checker"].get_threshold()
        for rec in self:
            rec.grammar_score_threshold = threshold

    @api.depends(
        "decision",
        "grammar_check_done",
        "prompt_grammar_score",
        "grammar_score_threshold",
    )
    def _compute_is_approve_blocked(self):
        for rec in self:
            if rec.decision != "approved":
                rec.is_approve_blocked = False
                continue
            # Require a grammar check to have run before approval.
            # If the check returned a low score, block.  If it
            # returned a passing score (or hasn't been run yet),
            # don't preemptively block — the form's
            # ``invisible`` modifier on the button will fire once
            # the user clicks Check Grammar and a low score lands.
            if not rec.grammar_check_done:
                rec.is_approve_blocked = False
            else:
                rec.is_approve_blocked = (
                    rec.prompt_grammar_score < rec.grammar_score_threshold
                )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_run_grammar_check(self):
        """Run Kimi grammar check on whichever panels have input.

        Always returns ``{type: ir.actions.act_window_close: False}``
        (i.e. keep the wizard open) — the reviewer reads the results
        in-place and then clicks Apply.
        """
        self.ensure_one()
        checker = self.env["video.qc.grammar.checker"]
        if not checker.is_configured():
            raise UserError(_(
                "Kimi API key is not configured.\n\n"
                "Set ``video_qc.kimi_api_key`` in Settings → Technical → "
                "Parameters → System Parameters before running the "
                "grammar check."
            ))

        vals = {"grammar_check_done": True}

        current_prompt = (self.version_id.prompt_text or "").strip()
        if current_prompt:
            result = checker.check(current_prompt)
            vals.update({
                "prompt_grammar_score": result["score"],
                "prompt_grammar_summary": result["summary"],
                "prompt_grammar_issues": "\n".join(
                    f"• {issue}" for issue in result["issues"]
                ),
                "prompt_grammar_corrected": result["corrected_text"],
            })
        else:
            vals.update({
                "prompt_grammar_score": 0,
                "prompt_grammar_summary": _(
                    "Version has no prompt text yet — nothing to grade."
                ),
                "prompt_grammar_issues": "",
                "prompt_grammar_corrected": "",
            })

        next_prompt = (self.next_prompt or "").strip()
        if next_prompt:
            result = checker.check(next_prompt)
            vals.update({
                "next_prompt_grammar_score": result["score"],
                "next_prompt_grammar_summary": result["summary"],
                "next_prompt_grammar_issues": "\n".join(
                    f"• {issue}" for issue in result["issues"]
                ),
                "next_prompt_grammar_corrected": result["corrected_text"],
            })
        else:
            vals.update({
                "next_prompt_grammar_score": 0,
                "next_prompt_grammar_summary": "",
                "next_prompt_grammar_issues": "",
                "next_prompt_grammar_corrected": "",
            })

        self.write(vals)

        # Keep the wizard open so the reviewer can read the results
        # without having to re-open it.
        return {
            "type": "ir.actions.act_window",
            "name": _("QC Review"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_apply(self):
        self.ensure_one()
        if not self.version_id:
            raise UserError(_("No version selected for QC."))
        if self.decision == "approved":
            # Enforce the grammar gate.  We re-check the gate here in
            # addition to the form's invisible modifier on the button
            # so a sufficiently determined reviewer can't bypass it
            # by submitting the wizard via RPC.
            if not self.grammar_check_done:
                raise UserError(_(
                    "Run the Kimi grammar check before approving — "
                    "Approve is gated on the prompt's grammar score."
                ))
            if self.prompt_grammar_score < self.grammar_score_threshold:
                raise UserError(_(
                    "Approve is blocked: the prompt's grammar score is "
                    "%(score)s, below the configured threshold of "
                    "%(threshold)s.  Fix the prompt via Attach Prompt, "
                    "re-run the grammar check, or use Reject / Request "
                    "Rework instead."
                ) % {
                    "score": self.prompt_grammar_score,
                    "threshold": self.grammar_score_threshold,
                })
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
