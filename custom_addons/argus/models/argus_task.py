# -*- coding: utf-8 -*-
"""Argus video review task.

Each record pairs an Instagram **Input Video URL** (original reel)
with an **Output Video URL** (the AI-generated rendition).  A single
prompt, two human reviewers (Project Lead + Quality Lead), an owner
email, and three independent status fields (task / QC / final
decision) capture the full review lifecycle.

Three statuses?
---------------
Yes: ``task_status`` tracks where the work is in the pipeline,
``qc_status`` is the quality reviewer's verdict at a point in time,
and ``final_decision`` is the manager's sign-off after QC has run.
Keeping them separate lets a QC verdict change ("rejected → approved
after revision") without retroactively rewriting the rest of the
history.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


# Same Instagram URL regex used by instagram_video_qc_manager —
# duplicated rather than imported to keep Argus standalone.
_INSTAGRAM_URL_RE = re.compile(
    r"^https?://(www\.)?instagram\.com/(reel|p|tv|reels)/(?P<code>[\w\-]+)/?",
    re.IGNORECASE,
)

# RFC-5322-light: a Char field is never going to be a true email
# validator, but this catches the bulk of typos (missing @, missing
# TLD, whitespace).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _extract_shortcode(url):
    """Return the Instagram shortcode (e.g. ``DX9JJWQDeL9``) or ``""``."""
    if not url:
        return ""
    match = _INSTAGRAM_URL_RE.match(url.strip())
    return match.group("code") if match else ""


class ArgusTask(models.Model):
    _name = "argus.task"
    _description = "Argus Video Review Task"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "timestamp desc, id desc"
    _rec_name = "name"

    # ------------------------------------------------------------------
    # Identification + lifecycle
    # ------------------------------------------------------------------
    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
        tracking=True,
    )
    timestamp = fields.Datetime(
        string="Time Stamp",
        default=fields.Datetime.now,
        readonly=True,
        required=True,
        index=True,
        tracking=True,
    )
    active = fields.Boolean(default=True)
    color = fields.Integer(string="Kanban Color")

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------
    pl_user_id = fields.Many2one(
        "res.users",
        string="Project Lead (PL)",
        index=True,
        tracking=True,
        domain=[("share", "=", False)],
    )
    ql_user_id = fields.Many2one(
        "res.users",
        string="Quality Lead (QL)",
        index=True,
        tracking=True,
        domain=[("share", "=", False)],
    )
    email = fields.Char(
        string="Owner Email",
        required=True,
        tracking=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    input_video_url = fields.Char(
        string="Input Video URL",
        required=True,
        tracking=True,
        help="Original Instagram reel / post / TV URL.",
    )
    output_video_url = fields.Char(
        string="Output Video URL",
        required=True,
        tracking=True,
        help="AI-generated Instagram reel / post / TV URL.",
    )
    prompt = fields.Text(
        string="Prompt",
        required=True,
        tracking=True,
        help="Video generation or reaction instruction sent to the model.",
    )

    # Shortcodes — useful for dedup, deep-linking, and audit.
    input_shortcode = fields.Char(
        string="Input Shortcode",
        compute="_compute_shortcodes",
        store=True,
        index=True,
    )
    output_shortcode = fields.Char(
        string="Output Shortcode",
        compute="_compute_shortcodes",
        store=True,
        index=True,
    )
    input_video_embed_url = fields.Char(
        string="Input Embed URL",
        compute="_compute_embed_urls",
        help="Instagram's official embed URL — drop into an iframe to "
             "render the reel inline.",
    )
    output_video_embed_url = fields.Char(
        string="Output Embed URL",
        compute="_compute_embed_urls",
    )

    # ------------------------------------------------------------------
    # Status fields — three independent dimensions
    # ------------------------------------------------------------------
    task_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("in_progress", "In Progress"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("needs_revision", "Needs Revision"),
        ],
        string="Task Status",
        default="pending",
        required=True,
        tracking=True,
        index=True,
    )
    qc_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("needs_revision", "Needs Revision"),
        ],
        string="QC Status",
        default="pending",
        required=True,
        tracking=True,
        index=True,
    )
    final_decision = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Final Decision",
        default="pending",
        required=True,
        tracking=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Remarks
    # ------------------------------------------------------------------
    ql_remarks = fields.Text(string="QL Remarks", tracking=True)
    tasker_remarks = fields.Text(string="Tasker Remarks", tracking=True)

    # ------------------------------------------------------------------
    # Prompt QC (Kimi K2.5 grammar / quality check)
    #
    # ``action_qc_check_prompt`` fills these in by calling the
    # ``argus.grammar.checker`` service.  The score + level + feedback
    # are surfaced as form fields; the raw_json column preserves the
    # exact upstream payload for audit.  QC verdict is auto-set:
    #   score >= threshold -> qc_status='approved'
    #   else               -> qc_status='rejected'
    # ------------------------------------------------------------------
    prompt_grammar_score = fields.Integer(
        string="Accuracy %",
        readonly=True,
        copy=False,
        help="Grammar score (0-100) returned by Kimi K2.5.",
    )
    prompt_grammar_level = fields.Selection(
        [
            ("unchecked", "Not Yet Checked"),
            ("poor", "Poor"),
            ("fair", "Fair"),
            ("good", "Good"),
            ("excellent", "Excellent"),
        ],
        string="Grammar Level",
        default="unchecked",
        readonly=True,
        copy=False,
        tracking=True,
    )
    prompt_grammar_feedback = fields.Text(
        string="Grammar Feedback",
        readonly=True,
        copy=False,
        help="One-paragraph verdict Kimi returned along with the score.",
    )
    prompt_grammar_response_json = fields.Text(
        string="Kimi Response (JSON)",
        readonly=True,
        copy=False,
        help="Verbatim JSON payload returned by Kimi K2.5 — kept for audit.",
    )
    prompt_grammar_checked_on = fields.Datetime(
        string="Grammar Check Run At",
        readonly=True,
        copy=False,
    )

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------
    duplicate_task_ids = fields.Many2many(
        "argus.task",
        string="Other tasks with the same URL pair",
        compute="_compute_duplicates",
    )
    duplicate_count = fields.Integer(
        string="# Duplicates",
        compute="_compute_duplicates",
    )
    is_duplicate = fields.Boolean(
        string="Has Duplicates",
        compute="_compute_duplicates",
        store=True,
        help="Flagged when another active task carries the SAME "
             "(input shortcode, output shortcode) pair.",
    )

    # ==================================================================
    # Compute
    # ==================================================================
    @api.depends("input_video_url", "output_video_url")
    def _compute_shortcodes(self):
        for rec in self:
            rec.input_shortcode = _extract_shortcode(rec.input_video_url)
            rec.output_shortcode = _extract_shortcode(rec.output_video_url)

    @api.depends("input_shortcode", "output_shortcode")
    def _compute_embed_urls(self):
        for rec in self:
            rec.input_video_embed_url = (
                f"https://www.instagram.com/reel/{rec.input_shortcode}/embed/"
                if rec.input_shortcode else ""
            )
            rec.output_video_embed_url = (
                f"https://www.instagram.com/reel/{rec.output_shortcode}/embed/"
                if rec.output_shortcode else ""
            )

    @api.depends("input_shortcode", "output_shortcode")
    def _compute_duplicates(self):
        for rec in self:
            if not rec.input_shortcode or not rec.output_shortcode:
                rec.duplicate_task_ids = self.env["argus.task"]
                rec.duplicate_count = 0
                rec.is_duplicate = False
                continue
            domain = [
                ("id", "!=", rec.id or False),
                ("input_shortcode", "=", rec.input_shortcode),
                ("output_shortcode", "=", rec.output_shortcode),
            ]
            matches = self.with_context(active_test=False).search(domain)
            # Only count *active* duplicates as a flag (archived rows
            # don't compete with live work) — but expose them in the
            # m2m so the user can still navigate to them.
            rec.duplicate_task_ids = matches
            active_count = len(matches.filtered("active"))
            rec.duplicate_count = active_count
            rec.is_duplicate = active_count > 0

    # ==================================================================
    # Validation
    # ==================================================================
    @api.constrains("input_video_url", "output_video_url")
    def _check_instagram_urls(self):
        for rec in self:
            for label, url in (
                ("Input Video URL", rec.input_video_url),
                ("Output Video URL", rec.output_video_url),
            ):
                if not url:
                    continue
                if not _INSTAGRAM_URL_RE.match(url.strip()):
                    raise ValidationError(_(
                        "%(label)s does not look like a valid Instagram "
                        "reel / post URL:\n  %(url)s"
                    ) % {"label": label, "url": url})

    @api.constrains("email")
    def _check_email(self):
        for rec in self:
            if rec.email and not _EMAIL_RE.match(rec.email.strip()):
                raise ValidationError(_(
                    "Owner Email %r doesn't look like a valid email "
                    "address."
                ) % rec.email)

    @api.constrains("prompt")
    def _check_prompt_not_empty(self):
        for rec in self:
            if rec.prompt is not None and not rec.prompt.strip():
                raise ValidationError(_("Prompt cannot be empty."))

    @api.constrains("input_video_url", "output_video_url")
    def _check_urls_distinct(self):
        for rec in self:
            if (
                rec.input_video_url
                and rec.output_video_url
                and rec.input_shortcode
                and rec.input_shortcode == rec.output_shortcode
            ):
                raise ValidationError(_(
                    "Input and Output URL point at the same Instagram "
                    "reel (%s).  They must be different reels."
                ) % rec.input_shortcode)

    # ==================================================================
    # ORM
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "argus.task"
                ) or _("New")
        return super().create(vals_list)

    def copy(self, default=None):
        default = dict(default or {})
        default.setdefault("name", _("New"))
        default.setdefault("task_status", "pending")
        default.setdefault("qc_status", "pending")
        default.setdefault("final_decision", "pending")
        return super().copy(default)

    # ==================================================================
    # Workflow transitions — keep the state machine in one place so the
    # form view's header buttons + the controller (future) call the
    # same code path.
    # ==================================================================
    def action_start(self):
        for rec in self:
            if rec.task_status != "pending":
                continue
            rec.task_status = "in_progress"
        return True

    def action_submit_to_qc(self):
        for rec in self:
            if rec.task_status not in ("in_progress", "needs_revision"):
                continue
            rec.qc_status = "pending"
            rec.task_status = "in_progress"
            if rec.ql_user_id:
                rec.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=_("QC review for %s") % rec.name,
                    user_id=rec.ql_user_id.id,
                )
        return True

    def action_qc_approve(self):
        """Mark the task approved across all three status dimensions.

        The simplified form exposes only two human verdict buttons —
        Approve / Reject — so each one needs to propagate to
        ``task_status``, ``qc_status``, AND ``final_decision`` in a
        single click.  No more multi-step Submit-to-QC then Final-Approve
        flow.
        """
        for rec in self:
            rec.qc_status = "approved"
            rec.task_status = "approved"
            rec.final_decision = "approved"
        return True

    def action_qc_reject(self):
        for rec in self:
            rec.qc_status = "rejected"
            rec.task_status = "rejected"
            rec.final_decision = "rejected"
        return True

    def action_qc_needs_revision(self):
        for rec in self:
            rec.qc_status = "needs_revision"
            rec.task_status = "needs_revision"
        return True

    def action_final_approve(self):
        for rec in self:
            rec.final_decision = "approved"
        return True

    def action_final_reject(self):
        for rec in self:
            rec.final_decision = "rejected"
        return True

    def action_reset_to_pending(self):
        for rec in self:
            rec.task_status = "pending"
            rec.qc_status = "pending"
            rec.final_decision = "pending"
        return True

    # ------------------------------------------------------------------
    # Prompt QC (Kimi K2.5 grammar check)
    # ------------------------------------------------------------------
    def action_qc_check_prompt(self):
        """Run the Kimi K2.5 grammar check against ``self.prompt``.

        Side-effects (per record):

        * Writes ``prompt_grammar_score`` / ``prompt_grammar_level`` /
          ``prompt_grammar_feedback`` / ``prompt_grammar_response_json``
          / ``prompt_grammar_checked_on``.
        * Auto-sets ``qc_status`` based on the configured threshold
          (``argus.grammar_score_threshold``, default 70).
        * Mirrors the verdict onto ``task_status`` (Approved /
          Rejected) so the kanban and dashboards stay in sync.
        * Posts a chatter message with the score, level, and the
          first line of the feedback for an instant audit trail.

        Returns a ``soft_reload`` client action so the form view
        re-fetches the record right after the check completes — the
        operator sees the freshly-written score, level, feedback
        banner, and status badges without having to manually refresh
        the page.
        """
        checker = self.env["argus.grammar.checker"]
        threshold = checker.get_threshold()

        for rec in self:
            if not (rec.prompt or "").strip():
                raise UserError(_(
                    "Cannot QC-check task %s: the Prompt field is empty."
                ) % rec.name)

            result = checker.check(rec.prompt)
            verdict = (
                "approved" if result["score"] >= threshold else "rejected"
            )
            # Cascade the verdict to ALL THREE status fields in one
            # write — the simplified form has no manual Approve /
            # Reject / Final-Approve buttons anymore, so the QC
            # Prompt result IS the final decision.  Reviewer can
            # always re-run the check to flip the verdict.
            rec.write({
                "prompt_grammar_score": result["score"],
                "prompt_grammar_level": result["level"],
                "prompt_grammar_feedback": result["feedback"],
                "prompt_grammar_response_json": result["raw_json"],
                "prompt_grammar_checked_on": fields.Datetime.now(),
                "qc_status": verdict,
                "task_status": verdict,
                "final_decision": verdict,
            })
            rec.message_post(
                body=_(
                    "<b>Prompt QC — Kimi K2.5</b><br/>"
                    "Score: <b>%(score)s</b> / 100 — "
                    "Level: <b>%(level)s</b> — "
                    "Threshold: %(threshold)s<br/>"
                    "Verdict: <b>%(verdict)s</b><br/>"
                    "<i>%(feedback)s</i>"
                ) % {
                    "score": result["score"],
                    "level": result["level"].capitalize(),
                    "threshold": threshold,
                    "verdict": verdict.upper(),
                    "feedback": result["feedback"] or _("(no feedback)"),
                },
                subtype_xmlid="mail.mt_note",
            )

        # ``soft_reload`` re-executes the current action, which for
        # the task form means re-fetching the record's fields.  This
        # is what makes the new grammar score, level badge, feedback
        # banner, and updated status statusbar appear immediately
        # after the QC check finishes — without it, the user would
        # have to manually refresh the browser to see the result of
        # the write() above.
        return {
            "type": "ir.actions.client",
            "tag": "soft_reload",
        }

    # ==================================================================
    # Cancel + preview actions used by the simplified form
    # ==================================================================
    def action_cancel_task(self):
        """Cancel the task — flips every status field to rejected.

        The simplified form exposes only TWO header buttons:
        **QC Prompt** (which auto-decides via Kimi) and this one.
        ``action_cancel_task`` is the "no" verdict in one click.
        """
        for rec in self:
            rec.write({
                "task_status": "rejected",
                "qc_status": "rejected",
                "final_decision": "rejected",
            })
            rec.message_post(
                body=_(
                    "<b>Task cancelled</b> by %s."
                ) % self.env.user.display_name,
                subtype_xmlid="mail.mt_note",
            )
        return True

    def action_preview_input(self):
        """Render the input reel inside a same-page modal dialog."""
        self.ensure_one()
        if not self.input_video_url:
            raise UserError(_("No input video URL set on this task."))
        return self._open_preview(
            title=_("Input Video — %s") % (self.name or ""),
            url=self.input_video_url,
            embed_url=self.input_video_embed_url,
        )

    def action_preview_output(self):
        """Render the output reel inside a same-page modal dialog."""
        self.ensure_one()
        if not self.output_video_url:
            raise UserError(_("No output video URL set on this task."))
        return self._open_preview(
            title=_("Output Video — %s") % (self.name or ""),
            url=self.output_video_url,
            embed_url=self.output_video_embed_url,
        )

    def _open_preview(self, title, url, embed_url):
        """Build the act_window that pops the preview wizard.

        The iframe inside the wizard does NOT point at Instagram's
        ``/embed/`` URL directly — that endpoint intentionally
        redirects on click and never plays inline.  Instead we point
        it at our own ``/argus/preview/<shortcode>`` controller which:

        1. Fetches the public Instagram page server-side.
        2. Extracts the direct ``.mp4`` URL from the page's ``og:video``
           / ``video_url`` JSON.
        3. Returns a minimal HTML page with an autoplaying HTML5
           ``<video controls>`` element pointed at that URL.

        That lets the operator actually watch the reel inside the
        Odoo modal instead of getting bounced out to instagram.com.

        Extraction can fail (Instagram rate limiting, login wall);
        when it does the controller renders a fallback iframe so the
        wizard always has *something* to show.

        ``target="new"`` on the action makes Odoo render the wizard
        as a modal dialog — the "popup" UX from the spec.
        """
        # Pick the shortcode from whichever URL we received — fall
        # back to the raw URL inside an iframe if we couldn't parse
        # a shortcode out of it (the validation @api.constrains
        # should have already rejected such records, but defence in
        # depth never hurts).
        shortcode = _extract_shortcode(url)
        if shortcode:
            # url-encode the title + source URL so they survive the
            # query string round-trip.  ``werkzeug.urls.url_encode``
            # would be heavier than necessary here — manual %-encode
            # is fine for these short values.
            from urllib.parse import urlencode
            qs = urlencode({"title": title or "", "source_url": url or ""})
            src = f"/argus/preview/{shortcode}?{qs}"
        else:
            # No shortcode → no controller URL; render the raw URL
            # in an iframe so the user at least sees something.
            src = embed_url or url
        iframe_html = (
            '<div class="o_argus_preview_frame" '
            'style="display:flex;justify-content:center;'
            'background:#000;border-radius:8px;overflow:hidden;">'
            '<iframe src="%s" '
            'width="540" height="720" '
            'frameborder="0" scrolling="no" '
            'allowtransparency="true" '
            'allow="autoplay; encrypted-media; picture-in-picture" '
            'style="border:0;max-width:100%%;display:block;">'
            '</iframe>'
            '</div>'
        ) % src
        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": "argus.video.preview.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_title": title,
                "default_video_url": url,
                "default_preview_html": iframe_html,
            },
        }

    # ==================================================================
    # Smart-button navigation
    # ==================================================================
    def action_view_duplicates(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Duplicate tasks"),
            "res_model": "argus.task",
            "view_mode": "list,form",
            "domain": [("id", "in", self.duplicate_task_ids.ids)],
            "context": {"active_test": False},
        }
