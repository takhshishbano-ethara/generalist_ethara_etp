"""Quarterly calibration sessions (iris v1.1, P2-7) — lean.

Every participant screens the same anonymized reference resumes; their
verdicts are compared against each other (and against the LLM's own
verdict) to surface screener drift. Three deliberately slim models — NOT
reusing ``iris.candidate``/``iris.screening`` (no pipeline state, no S3,
no HOLD lifecycle, no attachments):

* ``iris.calibration.session`` — anchor; ``action_start`` creates the
  participant×resume task matrix and queues one LLM reference screening
  per task; ``action_close`` posts the divergence summary to chatter.
* ``iris.calibration.resume`` — anonymized/synthetic reference resume
  (Binary + extracted text via ``pdf_extractor``); never a real candidate.
* ``iris.calibration.task`` — one screener × one resume. Inherits
  ``iris.llm.job.mixin`` ONLY (no chatter) and rides the existing 2-min
  LLM queue cron (``iris.calibration.task`` is registered in
  ``_LLM_QUEUE_MODELS``). The screener's own verdict is recorded
  independently of the LLM's.
"""

import base64
import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import pdf_extractor, prompt_loader, prompt_sanitizer, verdict_parser

_logger = logging.getLogger(__name__)

_VERDICT_SELECTION = [("ship", "Ship"), ("hold", "Hold"), ("block", "Block")]
_VERDICT_LABELS = {"ship": "SHIP", "hold": "HOLD", "block": "BLOCK"}


class IrisCalibrationSession(models.Model):
    _name = "iris.calibration.session"
    _description = "Iris Calibration Session"
    _inherit = ["mail.thread"]
    _order = "session_date desc, id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(string="Session Name", required=True, tracking=True)
    session_date = fields.Date(
        string="Session Date", default=fields.Date.context_today, tracking=True,
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
        ],
        string="State", default="draft", tracking=True, copy=False, index=True,
    )
    resume_ids = fields.One2many(
        "iris.calibration.resume", "session_id", string="Reference Resumes",
    )
    participant_ids = fields.Many2many(
        "res.users", string="Participants",
        help="Screeners taking part in this calibration round (min. 2).",
    )
    task_ids = fields.One2many(
        "iris.calibration.task", "session_id", string="Tasks",
    )
    task_count = fields.Integer(compute="_compute_task_count")
    divergent = fields.Boolean(
        string="Divergent", compute="_compute_divergence",
        help="Set when at least one reference resume received more than "
             "one distinct submitted screener verdict.",
    )
    divergence_summary = fields.Text(
        string="Divergence Summary", compute="_compute_divergence",
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("task_ids")
    def _compute_task_count(self):
        for session in self:
            session.task_count = len(session.task_ids)

    @api.depends(
        "resume_ids", "task_ids.resume_ref_id",
        "task_ids.screener_verdict", "task_ids.llm_verdict",
    )
    def _compute_divergence(self):
        for session in self:
            divergent = False
            lines = []
            for resume in session.resume_ids:
                tasks = session.task_ids.filtered(
                    lambda t, r=resume: t.resume_ref_id == r
                )
                submitted = tasks.filtered("screener_verdict")
                distinct = sorted({t.screener_verdict for t in submitted})
                llm_distinct = sorted(
                    {t.llm_verdict for t in tasks if t.llm_verdict}
                )
                llm_note = ""
                if llm_distinct:
                    llm_note = " (LLM: %s)" % ", ".join(
                        _VERDICT_LABELS[v] for v in llm_distinct
                    )
                if not submitted:
                    lines.append(_(
                        "%(resume)s: no screener verdicts submitted yet%(llm)s",
                        resume=resume.name, llm=llm_note,
                    ))
                elif len(distinct) > 1:
                    divergent = True
                    detail = "; ".join(
                        "%s: %s" % (
                            t.screener_id.name,
                            _VERDICT_LABELS[t.screener_verdict],
                        )
                        for t in submitted
                    )
                    lines.append(_(
                        "%(resume)s: DIVERGENT — %(detail)s%(llm)s",
                        resume=resume.name, detail=detail, llm=llm_note,
                    ))
                else:
                    lines.append(_(
                        "%(resume)s: aligned — %(verdict)s ×%(count)s%(llm)s",
                        resume=resume.name,
                        verdict=_VERDICT_LABELS[distinct[0]],
                        count=len(submitted), llm=llm_note,
                    ))
            session.divergent = divergent
            session.divergence_summary = "\n".join(lines)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_start(self):
        """Create the participant×resume task matrix and queue the LLM runs."""
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_("Only draft calibration sessions can be started."))
        if len(self.resume_ids) < 2:
            raise UserError(_(
                "A calibration session needs at least two reference resumes."
            ))
        missing_text = self.resume_ids.filtered(
            lambda r: not (r.resume_text or "").strip()
        )
        if missing_text:
            raise UserError(_(
                "These reference resumes have no extracted text: %s"
            ) % ", ".join(missing_text.mapped("name")))
        if len(self.participant_ids) < 2:
            raise UserError(_(
                "A calibration session needs at least two participants."
            ))

        tasks = self.env["iris.calibration.task"].create([
            {
                "session_id": self.id,
                "resume_ref_id": resume.id,
                "screener_id": user.id,
            }
            for resume in self.resume_ids
            for user in self.participant_ids
        ])
        self.write({"state": "in_progress"})
        self.message_post(body=_(
            "Calibration started: %(tasks)s task(s) created "
            "(%(resumes)s resumes × %(users)s participants); LLM reference "
            "screenings queued.",
            tasks=len(tasks),
            resumes=len(self.resume_ids),
            users=len(self.participant_ids),
        ))
        for task in tasks:
            task._llm_enqueue()
        return True

    def action_close(self):
        """Close the session and post the divergence summary to chatter."""
        self.ensure_one()
        if self.state != "in_progress":
            raise UserError(_(
                "Only in-progress calibration sessions can be closed."
            ))
        missing = self.task_ids.filtered(lambda t: not t.screener_verdict)
        if missing:
            raise UserError(_(
                "Every task needs a submitted screener verdict before the "
                "session can be closed (%s still missing)."
            ) % len(missing))
        self.write({"state": "done"})
        summary = self.divergence_summary or ""
        body = Markup("<p><strong>%s</strong></p><p>%s</p>") % (
            _("Calibration session closed — divergence summary:"),
            Markup("<br/>").join(escape(line) for line in summary.splitlines()),
        )
        self.message_post(body=body)
        return True

    def action_open_tasks(self):
        """Open this session's tasks in a full list/form action."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Calibration Tasks"),
            "res_model": "iris.calibration.task",
            "view_mode": "list,form",
            "domain": [("session_id", "=", self.id)],
            "context": {"default_session_id": self.id},
        }


class IrisCalibrationResume(models.Model):
    _name = "iris.calibration.resume"
    _description = "Iris Calibration Reference Resume"
    _order = "id"

    session_id = fields.Many2one(
        "iris.calibration.session", string="Session",
        required=True, ondelete="cascade", index=True,
    )
    name = fields.Char(
        string="Reference Name", required=True,
        help='Anonymized label, e.g. "Reference A" — never a real name.',
    )
    file = fields.Binary(
        string="Resume (PDF)", attachment=True, required=True,
        help="Upload anonymized or synthetic resumes only — never real "
             "candidate materials.",
    )
    filename = fields.Char(string="Filename")
    resume_text = fields.Text(
        string="Resume Text", readonly=True, copy=False,
        help="Plain text extracted from the PDF via PyMuPDF; this is what "
             "the LLM and the screeners actually read.",
    )
    target_role = fields.Char(string="Target Role / Level", required=True)

    # ------------------------------------------------------------------
    # CRUD — text extraction (no S3, no candidate linkage)
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec, vals in zip(records, vals_list):
            if vals.get("file"):
                rec._extract_resume_text(vals["file"], vals.get("filename"))
        return records

    def write(self, vals):
        res = super().write(vals)
        if "file" in vals:
            if vals["file"]:
                for rec in self:
                    rec._extract_resume_text(
                        vals["file"],
                        vals.get("filename") or rec.filename,
                    )
            else:
                super().write({"resume_text": False, "filename": False})
        return res

    def _extract_resume_text(self, file_b64, filename):
        """Extract the reference resume text (blocking on failure)."""
        self.ensure_one()
        if isinstance(file_b64, str):
            file_b64 = file_b64.encode("ascii")
        try:
            raw = base64.b64decode(file_b64)
        except Exception as exc:
            raise UserError(
                _("Could not decode the uploaded reference resume: %s") % exc
            ) from exc
        try:
            text = pdf_extractor.extract_text_from_pdf(raw, filename or "")
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        self.sudo().write({"resume_text": text})


class IrisCalibrationTask(models.Model):
    _name = "iris.calibration.task"
    _description = "Iris Calibration Task"
    _inherit = ["iris.llm.job.mixin"]
    _order = "resume_ref_id, id"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    session_id = fields.Many2one(
        "iris.calibration.session", string="Session",
        required=True, ondelete="cascade", index=True,
    )
    resume_ref_id = fields.Many2one(
        "iris.calibration.resume", string="Reference Resume",
        required=True, ondelete="cascade", index=True,
    )
    screener_id = fields.Many2one(
        "res.users", string="Screener", required=True, index=True,
    )
    markdown_record = fields.Text(
        string="LLM Screening Record (Markdown)", copy=False,
    )
    markdown_html = fields.Html(
        string="LLM Screening Record", compute="_compute_markdown_html",
        sanitize=False,
    )
    llm_verdict = fields.Selection(
        selection=_VERDICT_SELECTION, string="LLM Verdict",
        readonly=True, copy=False,
        help="Verdict parsed from the LLM reference screening. Parse "
             "failure routes the task's LLM status to Needs Review — the "
             "record stays readable for manual comparison.",
    )
    screener_verdict = fields.Selection(
        selection=_VERDICT_SELECTION, string="Screener Verdict", copy=False,
        help="Your independent verdict on this reference resume — decide "
             "before reading the LLM record.",
    )
    screener_notes = fields.Text(string="Screener Notes", copy=False)

    # ------------------------------------------------------------------
    # Computes / display
    # ------------------------------------------------------------------
    @api.depends("resume_ref_id.name", "screener_id.name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(resume)s — %(screener)s",
                resume=rec.resume_ref_id.name or "?",
                screener=rec.screener_id.name or "?",
            )

    @api.depends("markdown_record")
    def _compute_markdown_html(self):
        for rec in self:
            rec.markdown_html = rec._markdown_to_html(rec.markdown_record)

    # ------------------------------------------------------------------
    # LLM template methods (iris.llm.job.mixin contract)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """SCREENING prompt + shared sanitized INPUTS builder.

        Calibration is role-agnostic in v1.1: the screening prompt is
        loaded WITHOUT a role profile. The reference resume text is fenced
        as untrusted data and the central tech-date table is injected as
        trusted text by ``prompt_sanitizer.build_screening_inputs``.
        """
        self.ensure_one()
        resume = self.resume_ref_id
        system_prompt = prompt_loader.get_prompt(self.env, "screening")
        today = fields.Date.context_today(self)
        tech_table_md = self.env["iris.tech.date.reference"].get_reference_markdown()
        user_text = prompt_sanitizer.build_screening_inputs(
            resume.target_role or "",
            today.isoformat(),
            tech_table_md,
            resume.resume_text or "",
        )
        return system_prompt, user_text

    def _llm_on_success(self, content, meta):
        """Store the record and parse the LLM verdict (hardened parser).

        A parse failure routes the task to ``needs_review`` instead of
        guessing — there is no candidate state machine to drive here.
        """
        self.ensure_one()
        vals = {"markdown_record": content}
        verdict = verdict_parser.parse_screening_verdict(content)
        if verdict is None:
            vals["llm_status"] = "needs_review"
        else:
            vals["llm_verdict"] = verdict
        self.sudo().write(vals)

    def _llm_on_failure(self, msg):
        """No-op: calibration tasks have no pipeline state to revert."""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_submit_verdict(self):
        """Record the screener's verdict (owner or manager only)."""
        self.ensure_one()
        if not self.screener_verdict:
            raise UserError(_("Select your verdict before submitting."))
        if (
            self.env.user != self.screener_id
            and not self.env.user.has_group("iris.group_iris_manager")
        ):
            raise UserError(_(
                "Only the assigned screener (or an Iris Manager) can "
                "submit the verdict on this calibration task."
            ))
        # sudo: plain screeners have read-only ACL on the session, but
        # logging their own submission is a system note, not a write grant.
        self.session_id.sudo().message_post(body=_(
            "%(screener)s submitted %(verdict)s on %(resume)s.",
            screener=self.screener_id.name,
            verdict=_VERDICT_LABELS[self.screener_verdict],
            resume=self.resume_ref_id.name,
        ))
        return True
