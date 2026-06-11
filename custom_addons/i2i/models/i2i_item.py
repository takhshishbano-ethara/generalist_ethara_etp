from __future__ import annotations

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import credential_manager
from .i2i_project import PROJECT_TYPE_SELECTION

_logger = logging.getLogger(__name__)


EDIT_ONLY_SELECTION = [
    ("instruction_aligned", "Instruction Aligned"),
    ("no", "No"),
]
ALIGNED_SELECTION = [
    ("images_aligned", "Images Aligned"),
    ("no", "No"),
]
SLOP_SELECTION = [
    ("slop_free", "Slop Free"),
    ("no", "No"),
]


class I2IItem(models.Model):
    _name = "i2i.item"
    _description = "I2I Item (image-pair QC record)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(
        string="Reference",
        default=lambda self: self._default_name(),
        readonly=True,
        copy=False,
        tracking=True,
    )
    project_id = fields.Many2one(
        "i2i.project",
        string="Project",
        required=False,
        ondelete="restrict",
        index=True,
        tracking=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.user,
        required=True,
        index=True,
        tracking=True,
    )
    assigned_user_id = fields.Many2one(
        "res.users",
        string="Assigned To (legacy)",
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
        index=True,
    )

    project_type = fields.Selection(
        PROJECT_TYPE_SELECTION,
        string="Project Type",
        default=lambda self: self._default_project_type(),
        required=True,
        tracking=True,
    )

    @api.model
    def _default_project_type(self):
        project_id = self.env.context.get("default_project_id")
        if project_id:
            project = self.env["i2i.project"].browse(project_id).exists()
            if project and project.default_project_type:
                return project.default_project_type
        return "vs-1780675368-i2i-pixel-aligned-filter-image-edit-region"

    @api.onchange("project_id")
    def _onchange_project_id(self):
        if self.project_id and self.project_id.default_project_type:
            self.project_type = self.project_id.default_project_type
    instruction = fields.Text(
        string="Instruction",
        required=True,
        tracking=True,
        help="What the edit was supposed to accomplish.",
    )
    original_image_url = fields.Char(
        string="Original Image URL",
        required=True,
        tracking=True,
    )
    edited_image_url = fields.Char(
        string="Edited Image URL",
        required=True,
        tracking=True,
    )
    edit_only_instructed = fields.Selection(
        EDIT_ONLY_SELECTION,
        string="Does the edit make only the instructed change?",
        tracking=True,
        help="The edit does everything the instruction asks \u2014 and "
             "nothing it does not.",
    )
    images_aligned = fields.Selection(
        ALIGNED_SELECTION,
        string="Are the two images aligned?",
        tracking=True,
        help="Everything that should stay the same sits in the exact "
             "same place and size.",
    )
    free_of_ai_slop = fields.Selection(
        SLOP_SELECTION,
        string="Are both images free of AI slop?",
        tracking=True,
        help="AI slop includes melted fingers, warped faces, gibberish "
             "text, broken geometry, distorted shapes, etc.",
    )
    tasker_remarks = fields.Text(
        string="Tasker Remarks",
        tracking=True,
        help="Optional notes from the tasker about this item.",
    )

    llm_status = fields.Selection(
        [
            ("none", "Not Run"),
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="LLM Status",
        default="none",
        tracking=True,
        copy=False,
    )
    llm_edit_only_instructed = fields.Selection(
        EDIT_ONLY_SELECTION,
        string="LLM: Edit only instructed?",
        readonly=True,
        copy=False,
    )
    llm_images_aligned = fields.Selection(
        ALIGNED_SELECTION,
        string="LLM: Images aligned?",
        readonly=True,
        copy=False,
    )
    llm_free_of_ai_slop = fields.Selection(
        SLOP_SELECTION,
        string="LLM: Free of AI slop?",
        readonly=True,
        copy=False,
    )
    llm_reasoning = fields.Text(
        string="LLM Reasoning",
        readonly=True,
        copy=False,
    )
    llm_model_used = fields.Char(string="LLM Model", readonly=True, copy=False)
    llm_tokens_used = fields.Integer(string="LLM Tokens", readonly=True, copy=False)
    llm_cost_usd = fields.Float(
        string="LLM Cost (USD)",
        digits=(12, 6),
        readonly=True,
        copy=False,
    )
    llm_called_at = fields.Datetime(string="LLM Called At", readonly=True, copy=False)
    llm_error = fields.Text(string="LLM Error", readonly=True, copy=False)

    llm_decision = fields.Char(
        string="LLM Decision",
        readonly=True,
        copy=False,
    )
    llm_rubric_verdict = fields.Selection(
        [("pass", "PASS"), ("fail", "FAIL")],
        string="LLM Rubric Verdict",
        readonly=True,
        copy=False,
    )
    llm_q1_fail_code = fields.Char(
        string="LLM Q1 Fail Code",
        readonly=True,
        copy=False,
    )
    llm_q1_evidence = fields.Text(
        string="LLM Q1 Evidence",
        readonly=True,
        copy=False,
    )
    llm_q2_fail_code = fields.Char(
        string="LLM Q2 Fail Code",
        readonly=True,
        copy=False,
    )
    llm_q2_evidence = fields.Text(
        string="LLM Q2 Evidence",
        readonly=True,
        copy=False,
    )
    llm_q3_fail_code = fields.Char(
        string="LLM Q3 Fail Code",
        readonly=True,
        copy=False,
    )
    llm_q3_failed_image = fields.Selection(
        [("none", "None"), ("original", "Original"), ("edited", "Edited"), ("both", "Both")],
        string="LLM Q3 Failed Image",
        readonly=True,
        copy=False,
    )
    llm_q3_evidence = fields.Text(
        string="LLM Q3 Evidence",
        readonly=True,
        copy=False,
    )
    llm_findings = fields.Text(
        string="LLM Findings",
        readonly=True,
        copy=False,
    )
    llm_findings_count = fields.Integer(
        string="LLM Findings Count",
        readonly=True,
        copy=False,
    )

    disagree_edit_only = fields.Boolean(
        string="Disagree: Edit Only",
        compute="_compute_disagreements",
        store=True,
    )
    disagree_aligned = fields.Boolean(
        string="Disagree: Aligned",
        compute="_compute_disagreements",
        store=True,
    )
    disagree_slop = fields.Boolean(
        string="Disagree: Slop",
        compute="_compute_disagreements",
        store=True,
    )
    has_disagreement = fields.Boolean(
        string="Disagreement",
        compute="_compute_disagreements",
        store=True,
        index=True,
    )
    submitted_at = fields.Datetime(
        string="Submitted At",
        readonly=True,
        copy=False,
        tracking=True,
    )
    submitted_at_display = fields.Char(
        string="Submitted At",
        compute="_compute_submitted_at_display",
    )

    @api.depends("submitted_at")
    def _compute_submitted_at_display(self):
        for rec in self:
            if rec.submitted_at:
                local = fields.Datetime.context_timestamp(rec, rec.submitted_at)
                rec.submitted_at_display = local.strftime("%b %d, %I:%M:%S %p")
            else:
                rec.submitted_at_display = ""

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("human_qc", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="QC State",
        default="draft",
        tracking=True,
        index=True,
        copy=False,
    )
    qc_verdict = fields.Selection(
        [("approved", "Approved"), ("rejected", "Rejected")],
        string="Verdict",
        readonly=True,
        copy=False,
        tracking=True,
    )
    qc_remark = fields.Text(string="QC Remark", tracking=True)
    qc_reviewer_id = fields.Many2one(
        "res.users",
        string="QC Reviewer",
        readonly=True,
        copy=False,
        tracking=True,
    )
    qc_date = fields.Datetime(string="QC Date", readonly=True, copy=False)

    @api.depends(
        "edit_only_instructed", "llm_edit_only_instructed",
        "images_aligned", "llm_images_aligned",
        "free_of_ai_slop", "llm_free_of_ai_slop",
        "llm_status",
    )
    def _compute_disagreements(self):
        for rec in self:
            if rec.llm_status != "done":
                rec.disagree_edit_only = False
                rec.disagree_aligned = False
                rec.disagree_slop = False
                rec.has_disagreement = False
                continue
            rec.disagree_edit_only = bool(
                rec.edit_only_instructed
                and rec.llm_edit_only_instructed
                and rec.edit_only_instructed != rec.llm_edit_only_instructed
            )
            rec.disagree_aligned = bool(
                rec.images_aligned
                and rec.llm_images_aligned
                and rec.images_aligned != rec.llm_images_aligned
            )
            rec.disagree_slop = bool(
                rec.free_of_ai_slop
                and rec.llm_free_of_ai_slop
                and rec.free_of_ai_slop != rec.llm_free_of_ai_slop
            )
            rec.has_disagreement = (
                rec.disagree_edit_only
                or rec.disagree_aligned
                or rec.disagree_slop
            )

    def _default_name(self):
        return self.env["ir.sequence"].next_by_code("i2i.item") or _("New")

    @api.constrains("original_image_url", "edited_image_url")
    def _check_urls(self):
        for rec in self:
            for field, label in (
                ("original_image_url", _("Original Image URL")),
                ("edited_image_url", _("Edited Image URL")),
            ):
                value = (rec[field] or "").strip()
                if not value:
                    raise ValidationError(_("%s is required.") % label)
                if not (value.startswith("http://") or value.startswith("https://")):
                    raise ValidationError(
                        _("%s must be an http:// or https:// URL.") % label
                    )

    def _schedule_llm_qc(self):
        self.ensure_one()
        if self.llm_status in ("none", "failed"):
            self.sudo().write({"llm_status": "pending"})
            cron = self.env.ref(
                "i2i.cron_i2i_run_pending_llm_qc",
                raise_if_not_found=False,
            )
            if cron:
                cron.sudo()._trigger()

    def action_run_llm_qc(self):
        for rec in self:
            rec.sudo().write({"llm_status": "none", "llm_error": False})
            rec._schedule_llm_qc()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Quality Check Queued"),
                "message": _("Quality check is running in background. Results will appear shortly."),
                "type": "success",
                "sticky": False,
            },
        }

    def _run_llm_qc_sync(self):
        self.ensure_one()
        from ..services import llm_qc_client

        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            self.sudo().write({
                "llm_status": "failed",
                "llm_error": _("OpenRouter API key not configured. Set it in Settings > I2I."),
            })
            return

        ICP = self.env["ir.config_parameter"].sudo()
        model = ICP.get_param("i2i.default_model", "google/gemini-3.5-flash")
        http_referer = ICP.get_param("i2i.http_referer") or None
        app_title = ICP.get_param("i2i.app_title") or "Ethara I2I"
        try:
            max_retries = int(ICP.get_param("i2i.openrouter_max_retries", "3"))
        except (TypeError, ValueError):
            max_retries = 3
        try:
            usd_per_mtoken = float(ICP.get_param("i2i.usd_per_mtoken", "0.20"))
        except (TypeError, ValueError):
            usd_per_mtoken = 0.20

        self.sudo().write({"llm_status": "processing", "llm_error": False})

        try:
            result = llm_qc_client.review_image_pair(
                api_key=api_key,
                model=model,
                instruction=self.instruction or "",
                original_url=self.original_image_url,
                edited_url=self.edited_image_url,
                http_referer=http_referer,
                app_title=app_title,
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("I2I LLM call failed for %s", self.display_name)
            self.sudo().write({
                "llm_status": "failed",
                "llm_error": str(exc),
            })
            return

        tokens = int(result.get("tokens", 0) or 0)
        cost = (tokens / 1_000_000.0) * usd_per_mtoken if tokens else 0.0

        rubric_raw = (result.get("rubric_verdict") or "").strip().lower()
        rubric_val = rubric_raw if rubric_raw in ("pass", "fail") else False
        failed_image_raw = (result.get("q3_failed_image") or "").strip().lower()
        failed_image_val = failed_image_raw if failed_image_raw in ("none", "original", "edited", "both") else False

        vals = {
            "llm_status": "done",
            "llm_model_used": model,
            "llm_tokens_used": tokens,
            "llm_cost_usd": cost,
            "llm_called_at": fields.Datetime.now(),
            "llm_edit_only_instructed": result.get("edit_only_instructed"),
            "llm_images_aligned": result.get("images_aligned"),
            "llm_free_of_ai_slop": result.get("free_of_ai_slop"),
            "llm_reasoning": result.get("reasoning") or "",
            "llm_error": False,
            "llm_decision": (result.get("decision") or "")[:500] or False,
            "llm_rubric_verdict": rubric_val,
            "llm_q1_fail_code": (result.get("q1_fail_code") or "")[:100] or False,
            "llm_q1_evidence": result.get("q1_evidence") or False,
            "llm_q2_fail_code": (result.get("q2_fail_code") or "")[:100] or False,
            "llm_q2_evidence": result.get("q2_evidence") or False,
            "llm_q3_fail_code": (result.get("q3_fail_code") or "")[:100] or False,
            "llm_q3_failed_image": failed_image_val,
            "llm_q3_evidence": result.get("q3_evidence") or False,
            "llm_findings": result.get("findings") or False,
            "llm_findings_count": int(result.get("findings_count") or 0),
        }
        self.sudo().write(vals)
        self.message_post(body=_(
            "LLM QC completed. Edit-only: %(e)s | Aligned: %(a)s | Free of slop: %(s)s"
        ) % {
            "e": result.get("edit_only_instructed") or "?",
            "a": result.get("images_aligned") or "?",
            "s": result.get("free_of_ai_slop") or "?",
        })

    @api.model
    def _cron_run_pending_llm_qc(self, batch_size: int = 20):
        pending = self.search([("llm_status", "=", "pending")], limit=batch_size)
        for rec in pending:
            try:
                rec._run_llm_qc_sync()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001
                _logger.exception("I2I cron: LLM QC failed for %s", rec.id)
                self.env.cr.rollback()

    def action_send_to_human_qc(self):
        for rec in self:
            if rec.state in ("approved", "rejected"):
                raise UserError(_(
                    "Item %s is already in a terminal state."
                ) % rec.display_name)
            if rec.llm_status in ("none", "failed"):
                rec._schedule_llm_qc()
            rec.submitted_at = fields.Datetime.now()
            rec.state = "human_qc"

    def action_approve(self):
        for rec in self:
            rec._set_verdict("approved")
        return True

    def action_reject(self):
        for rec in self:
            rec._set_verdict("rejected")
        return True

    def _set_verdict(self, verdict: str):
        self.ensure_one()
        if verdict not in ("approved", "rejected"):
            raise UserError(_("Invalid verdict: %s") % verdict)
        if verdict == "rejected" and not (self.qc_remark or "").strip():
            raise UserError(_(
                "QC Remark is required when rejecting an item."
            ))
        self.write({
            "qc_verdict": verdict,
            "state": verdict,
            "qc_reviewer_id": self.env.user.id,
            "qc_date": fields.Datetime.now(),
        })
        self.message_post(body=_(
            "QC %(v)s by %(u)s. Remark: %(r)s"
        ) % {
            "v": verdict.title(),
            "u": self.env.user.display_name,
            "r": self.qc_remark or "\u2014",
        })

    def action_reset_to_draft(self):
        for rec in self:
            rec.write({
                "state": "draft",
                "qc_verdict": False,
                "qc_reviewer_id": False,
                "qc_date": False,
            })
