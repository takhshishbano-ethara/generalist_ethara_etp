"""Iris JD pre-stage — critique + rewrite of a job description BEFORE screening.

Two models:

* ``iris.job.description`` — the anchor record (mirrors ``iris.candidate``,
  NO LLM mixin). Carries the raw JD text (pasted or PDF-extracted), the
  human-curated ``final_jd``, and the state machine::

      draft ──action_critique──▶ critiquing ──done──▶ critiqued
      critiqued ──action_rewrite──▶ rewriting ──done──▶ rewritten
      rewritten ──action_approve (manager)──▶ approved
      approved ──action_reopen (manager)──▶ rewritten
      re-runs: action_critique from {draft, critiqued, rewritten};
               action_rewrite from {critiqued, rewritten}
      LLM failure reverts: critique → draft; rewrite → critiqued

* ``iris.jd.artifact`` — one LLM operation per row (``operation`` =
  critique / rewrite), mirrors ``iris.screening``. The FIRST parse-free
  mixin user: the markdown IS the artifact, so ``done`` is terminal and
  there is no ``needs_review`` path.

``action_approve`` hard-raises while ``[FILL-IN`` placeholders remain in
``final_jd`` — the mechanical enforcement of "don't publish placeholders".
"""

import base64
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import pdf_extractor, prompt_loader, prompt_sanitizer

_logger = logging.getLogger(__name__)

#: States action_critique may be triggered from (each run = a NEW artifact).
_CRITIQUE_FROM_STATES = ("draft", "critiqued", "rewritten")
#: States action_rewrite may be triggered from.
_REWRITE_FROM_STATES = ("critiqued", "rewritten")

_OPERATION_LABELS = {"critique": "Critique", "rewrite": "Rewrite"}


class IrisJobDescription(models.Model):
    _name = "iris.job.description"
    _description = "Iris Job Description"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(string="Role Title", required=True, tracking=True)
    company_name = fields.Char(
        string="Company",
        help="Optional — passed verbatim to the critique/rewrite prompts.",
    )
    raw_jd = fields.Text(
        string="Original JD (Text)",
        help="The job description text under review. Filled automatically "
             "from the uploaded PDF when left empty.",
    )
    source_file = fields.Binary(
        string="Source File (PDF)", attachment=True, copy=False,
    )
    source_filename = fields.Char(string="Source Filename", copy=False)
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("critiquing", "Critiquing"),
            ("critiqued", "Critiqued"),
            ("rewriting", "Rewriting"),
            ("rewritten", "Rewritten"),
            ("approved", "Approved"),
        ],
        string="State",
        default="draft",
        tracking=True,
        copy=False,
        index=True,
    )
    artifact_ids = fields.One2many(
        "iris.jd.artifact", "jd_id", string="LLM Artifacts",
    )
    current_critique_id = fields.Many2one(
        "iris.jd.artifact", string="Current Critique",
        compute="_compute_current_artifacts",
        help="Latest completed critique artifact.",
    )
    current_rewrite_id = fields.Many2one(
        "iris.jd.artifact", string="Current Rewrite",
        compute="_compute_current_artifacts",
        help="Latest completed rewrite artifact.",
    )
    critique_html = fields.Html(
        string="Critique", compute="_compute_artifact_html", sanitize=False,
    )
    rewrite_html = fields.Html(
        string="Rewrite", compute="_compute_artifact_html", sanitize=False,
    )
    final_jd = fields.Text(
        string="Final JD",
        copy=False,
        help="Seeded from the rewrite output; HR edits it here to resolve "
             "every [FILL-IN: ...] placeholder before approval.",
    )
    has_fillins = fields.Boolean(
        string="Has FILL-IN Placeholders", compute="_compute_has_fillins",
        help="True while the final JD still contains [FILL-IN placeholders "
             "(approval is blocked).",
    )
    approved_by = fields.Many2one(
        "res.users", string="Approved By", readonly=True, copy=False,
    )
    approved_at = fields.Datetime(string="Approved At", readonly=True, copy=False)
    active = fields.Boolean(default=True)
    artifact_count = fields.Integer(compute="_compute_artifact_count")
    last_llm_failed = fields.Boolean(
        string="Last LLM Run Failed", compute="_compute_last_llm_failure",
        help="True when the most recent LLM artifact for this JD failed "
             "(drives the form failure banner).",
    )
    last_llm_error = fields.Text(
        string="Last LLM Error", compute="_compute_last_llm_failure",
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("artifact_ids.llm_status", "artifact_ids.operation")
    def _compute_current_artifacts(self):
        for rec in self:
            done = rec.artifact_ids.filtered(lambda a: a.llm_status == "done")
            critiques = done.filtered(
                lambda a: a.operation == "critique"
            ).sorted("id")
            rewrites = done.filtered(
                lambda a: a.operation == "rewrite"
            ).sorted("id")
            rec.current_critique_id = critiques[-1:].id or False
            rec.current_rewrite_id = rewrites[-1:].id or False

    @api.depends(
        "artifact_ids.markdown_result", "artifact_ids.llm_status",
        "artifact_ids.operation",
    )
    def _compute_artifact_html(self):
        Artifact = self.env["iris.jd.artifact"]
        for rec in self:
            rec.critique_html = Artifact._markdown_to_html(
                rec.current_critique_id.markdown_result,
            )
            rec.rewrite_html = Artifact._markdown_to_html(
                rec.current_rewrite_id.markdown_result,
            )

    @api.depends("final_jd")
    def _compute_has_fillins(self):
        for rec in self:
            rec.has_fillins = "[FILL-IN" in (rec.final_jd or "")

    @api.depends("artifact_ids")
    def _compute_artifact_count(self):
        for rec in self:
            rec.artifact_count = len(rec.artifact_ids)

    @api.depends("artifact_ids.llm_status", "artifact_ids.llm_error")
    def _compute_last_llm_failure(self):
        for rec in self:
            last = rec.artifact_ids.sorted("id")[-1:]
            failed = bool(last) and last.llm_status == "failed"
            rec.last_llm_failed = failed
            rec.last_llm_error = last.llm_error if failed else False

    # ------------------------------------------------------------------
    # CRUD + source-file extraction
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("source_file") and not (vals.get("raw_jd") or "").strip():
                vals["raw_jd"] = self._extract_source_text(
                    vals["source_file"], vals.get("source_filename"),
                )
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        if vals.get("source_file"):
            for rec in self:
                if not (rec.raw_jd or "").strip():
                    rec.write({
                        "raw_jd": self._extract_source_text(
                            vals["source_file"],
                            vals.get("source_filename") or rec.source_filename,
                        ),
                    })
        return res

    @api.model
    def _extract_source_text(self, file_b64, filename):
        """Decode + extract the uploaded PDF; UserError on any failure."""
        if isinstance(file_b64, str):
            file_b64 = file_b64.encode("ascii")
        try:
            raw = base64.b64decode(file_b64)
        except Exception as exc:
            raise UserError(
                _("Could not decode the uploaded JD file: %s") % exc
            ) from exc
        try:
            return pdf_extractor.extract_text_from_pdf(raw, filename or "")
        except ValueError as exc:
            raise UserError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_critique(self):
        """Run (or re-run) the LLM critique of the original JD."""
        self.ensure_one()
        if self.state not in _CRITIQUE_FROM_STATES:
            raise UserError(_(
                "A critique can only be started from Draft, Critiqued or "
                "Rewritten (current: %s)."
            ) % self.state)
        if not (self.raw_jd or "").strip():
            raise UserError(_(
                "Provide the original JD text (paste it or upload a PDF) "
                "before running the critique."
            ))
        artifact = self.env["iris.jd.artifact"].create({
            "jd_id": self.id,
            "operation": "critique",
        })
        self.write({"state": "critiquing"})
        artifact._llm_enqueue()
        return artifact

    def action_rewrite(self):
        """Run (or re-run) the LLM rewrite, fed by the completed critique."""
        self.ensure_one()
        if self.state not in _REWRITE_FROM_STATES:
            raise UserError(_(
                "A rewrite can only be started from Critiqued or Rewritten "
                "(current: %s)."
            ) % self.state)
        if not self.current_critique_id:
            raise UserError(_(
                "No completed critique found — run the critique first "
                "(the rewrite is derived from it)."
            ))
        artifact = self.env["iris.jd.artifact"].create({
            "jd_id": self.id,
            "operation": "rewrite",
        })
        self.write({"state": "rewriting"})
        artifact._llm_enqueue()
        return artifact

    def action_approve(self):
        """Manager approval — hard-blocked while [FILL-IN placeholders remain."""
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can approve a job description."
            ))
        if self.state != "rewritten":
            raise UserError(_(
                "Only a Rewritten job description can be approved "
                "(current: %s)."
            ) % self.state)
        if not (self.final_jd or "").strip():
            raise UserError(_(
                "The Final JD is empty — it is seeded from the rewrite and "
                "must be completed before approval."
            ))
        if "[FILL-IN" in self.final_jd:
            raise UserError(_(
                "The Final JD still contains [FILL-IN: ...] placeholders. "
                "Resolve every placeholder before approving — placeholders "
                "must never be published."
            ))
        self.write({
            "state": "approved",
            "approved_by": self.env.user.id,
            "approved_at": fields.Datetime.now(),
        })
        self.message_post(body=_(
            "Job description approved by %s.") % self.env.user.name)
        return True

    def action_reopen(self):
        """Manager reopen: approved → rewritten (approval rescinded)."""
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can reopen an approved job description."
            ))
        if self.state != "approved":
            raise UserError(_(
                "Only an Approved job description can be reopened."
            ))
        previous_approver = self.approved_by.name or "?"
        self.write({
            "state": "rewritten",
            "approved_by": False,
            "approved_at": False,
        })
        self.message_post(body=_(
            "Job description reopened by %(user)s (previous approval by "
            "%(approver)s rescinded).",
            user=self.env.user.name, approver=previous_approver,
        ))
        return True


class IrisJdArtifact(models.Model):
    _name = "iris.jd.artifact"
    _description = "Iris JD Artifact"
    _inherit = ["iris.llm.job.mixin", "mail.thread"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    jd_id = fields.Many2one(
        "iris.job.description", string="Job Description",
        required=True, ondelete="cascade", index=True,
    )
    operation = fields.Selection(
        selection=[("critique", "Critique"), ("rewrite", "Rewrite")],
        string="Operation", required=True, readonly=True,
    )
    attempt = fields.Integer(
        string="Attempt", readonly=True,
        help="1-based attempt number per (job description, operation).",
    )
    markdown_result = fields.Text(string="Result (Markdown)", copy=False)
    markdown_html = fields.Html(
        string="Result", compute="_compute_markdown_html", sanitize=False,
    )
    attachment_id = fields.Many2one(
        "ir.attachment", string="Result Attachment", copy=False,
    )

    # ------------------------------------------------------------------
    # CRUD / display
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("attempt") and vals.get("jd_id") and vals.get("operation"):
                vals["attempt"] = self.search_count([
                    ("jd_id", "=", vals["jd_id"]),
                    ("operation", "=", vals["operation"]),
                ]) + 1
        return super().create(vals_list)

    @api.depends("jd_id.name", "operation", "attempt")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(jd)s — %(operation)s #%(attempt)s",
                jd=rec.jd_id.name or "?",
                operation=_OPERATION_LABELS.get(rec.operation, rec.operation or "?"),
                attempt=rec.attempt or rec.id,
            )

    @api.depends("markdown_result")
    def _compute_markdown_html(self):
        for rec in self:
            rec.markdown_html = rec._markdown_to_html(rec.markdown_result)

    # ------------------------------------------------------------------
    # LLM template methods (parse-free: the markdown IS the artifact)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """Branch on ``operation``: critique vs rewrite prompt + inputs.

        The JD text (and for rewrites, the critique document derived from
        it) is fenced as untrusted data via the prompt sanitizer.
        """
        self.ensure_one()
        jd = self.jd_id
        today = fields.Date.context_today(self)
        header = [
            f"JOB TITLE:      {jd.name or ''}",
            f"COMPANY:        {jd.company_name or 'not provided'}",
            f"TODAY'S DATE:   {today.isoformat()}",
            "",
        ]
        if self.operation == "critique":
            system_prompt = prompt_loader.get_prompt(self.env, "jd_critique")
            lines = header + [
                "JOB DESCRIPTION TEXT (may be parsed from PDF — formatting "
                "artifacts possible):",
                prompt_sanitizer.fence_untrusted(
                    "JOB DESCRIPTION", jd.raw_jd or "",
                ),
            ]
        else:
            critique = jd.current_critique_id
            if not critique or not (critique.markdown_result or "").strip():
                raise UserError(_(
                    "No completed critique found — run the critique before "
                    "the rewrite."
                ))
            system_prompt = prompt_loader.get_prompt(self.env, "jd_rewrite")
            lines = header + [
                "ORIGINAL JOB DESCRIPTION:",
                prompt_sanitizer.fence_untrusted(
                    "ORIGINAL JOB DESCRIPTION", jd.raw_jd or "",
                ),
                "",
                "CRITIQUE DOCUMENT:",
                prompt_sanitizer.fence_untrusted(
                    "CRITIQUE DOCUMENT", critique.markdown_result,
                ),
            ]
        return system_prompt, "\n".join(lines)

    def _llm_on_success(self, content, meta):
        """Store the markdown, attach it, advance the JD state.

        No verdict parsing — ``done`` is terminal for JD artifacts. On a
        rewrite, ``final_jd`` is (re)seeded with the output; prior manual
        edits are overwritten with a chatter note.
        """
        self.ensure_one()
        jd = self.jd_id
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"jd-{self.operation}-{self._jd_slug()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({
            "markdown_result": content,
            "attachment_id": attachment.id,
        })

        if self.operation == "critique":
            jd.sudo().write({"state": "critiqued"})
            jd.message_post(body=_(
                "JD critique #%(attempt)s completed (%(filename)s).",
                attempt=self.attempt, filename=filename,
            ))
        else:
            had_edits = bool((jd.final_jd or "").strip())
            jd.sudo().write({"state": "rewritten", "final_jd": content})
            jd.message_post(body=_(
                "JD rewrite #%(attempt)s completed (%(filename)s). The "
                "Final JD was seeded from the rewrite output.",
                attempt=self.attempt, filename=filename,
            ))
            if had_edits:
                jd.message_post(body=_(
                    "Note: the previous Final JD content (including any "
                    "manual edits) was overwritten by rewrite #%s.",
                ) % self.attempt)

    def _llm_on_failure(self, msg):
        """Deterministic revert: critique → draft; rewrite → critiqued."""
        self.ensure_one()
        jd = self.jd_id
        revert_state = "draft" if self.operation == "critique" else "critiqued"
        jd.sudo().write({"state": revert_state})
        jd.message_post(body=_(
            "JD %(operation)s #%(attempt)s failed (%(error)s). Job "
            "description reverted to %(state)s.",
            operation=_OPERATION_LABELS.get(self.operation, self.operation),
            attempt=self.attempt, error=msg, state=revert_state,
        ))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry_llm(self):
        """Re-enqueue a failed artifact, restoring the in-flight JD state."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_("Only failed JD artifacts can be retried."))
        in_flight = "critiquing" if self.operation == "critique" else "rewriting"
        self.jd_id.sudo().write({"state": in_flight})
        self._llm_enqueue()
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _jd_slug(self):
        """Lowercased hyphen-slug of the JD role title (for filenames)."""
        self.ensure_one()
        slug = re.sub(r"[^a-z0-9]+", "-", (self.jd_id.name or "").lower())
        return slug.strip("-") or "jd"
