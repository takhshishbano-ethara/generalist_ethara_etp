import json
import logging
import random
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_MEDIA_ERROR_STEPS = frozenset({
    "recorder-unsupported", "clip-empty", "presign-null", "presign-error",
    "s3-post-error", "s3-post-failed", "commit-error", "capture-error",
    "snapshot-not-ready", "snapshot-empty", "snapshot-not-stored", "snapshot-error",
    "detector-error", "consent-error",
})
_MEDIA_ERROR_CAP = 50


class EtpApplicantAssessment(models.Model):
    _name = "etp.applicant.assessment"
    _description = "Applicant Assessment"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)

    applicant_id = fields.Many2one(
        "hr.applicant", required=True, ondelete="cascade", index=True,
    )
    job_id = fields.Many2one(
        "hr.job", required=True, ondelete="restrict", index=True,
    )
    template_id = fields.Many2one(
        "etp.applicant.assessment.template",
        required=True, ondelete="restrict",
    )
    company_id = fields.Many2one(
        "res.company",
        related="applicant_id.company_id",
        string="Company",
        readonly=True,
        help="Referenced by the invitation mail template (email_from).",
    )

    access_token = fields.Char(
        default=lambda self: uuid.uuid4().hex,
        copy=False, index=True, readonly=True, required=True,
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("sent", "Sent"),
            ("in_progress", "In Progress"),
            ("submitted", "Submitted"),
            ("scored", "Scored"),
            ("cancelled", "Cancelled"),
        ],
        default="draft", required=True, index=True, tracking=True,
    )
    result = fields.Selection(
        [("pending", "Pending"), ("pass", "Pass"), ("fail", "Fail")],
        compute="_compute_result", store=True, default="pending",
    )

    started_at = fields.Datetime(readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    deadline_at = fields.Datetime(readonly=True, copy=False)

    duration_minutes = fields.Integer(readonly=True)
    pass_mark_percent = fields.Float(readonly=True)
    warning_cap = fields.Integer(readonly=True)
    max_score = fields.Integer(readonly=True, string="Total Marks")
    require_webcam = fields.Boolean(readonly=True)
    require_mic = fields.Boolean(readonly=True)
    require_fullscreen = fields.Boolean(readonly=True)
    block_copy_paste = fields.Boolean(readonly=True)
    block_right_click = fields.Boolean(readonly=True)
    detect_window_switch = fields.Boolean(readonly=True)
    detect_no_face = fields.Boolean(readonly=True)
    detect_other_person = fields.Boolean(readonly=True)
    detect_look_away = fields.Boolean(readonly=True)
    detect_lip_movement = fields.Boolean(readonly=True)
    detect_mobile_phone = fields.Boolean(readonly=True)
    shuffle_questions = fields.Boolean(readonly=True)

    section_ids = fields.One2many(
        "etp.applicant.assessment.section", "assessment_id",
    )
    question_ids = fields.One2many(
        "etp.applicant.assessment.question", "assessment_id",
    )
    answer_ids = fields.One2many(
        "etp.applicant.assessment.answer", "assessment_id",
    )
    warning_ids = fields.One2many(
        "etp.applicant.assessment.warning", "assessment_id",
    )
    snapshot_ids = fields.One2many(
        "etp.applicant.assessment.snapshot", "assessment_id",
    )

    consent_at = fields.Datetime(readonly=True, copy=False)
    consent_version = fields.Char(readonly=True, copy=False)
    media_errors_json = fields.Text(readonly=True, copy=False, default="[]")

    question_count = fields.Integer(compute="_compute_counts", store=True)
    answered_count = fields.Integer(compute="_compute_counts", store=True)
    warning_count = fields.Integer(compute="_compute_counts", store=True)
    snapshot_count = fields.Integer(compute="_compute_counts", store=True)
    media_error_count = fields.Integer(compute="_compute_media_errors")

    objective_score = fields.Float(
        compute="_compute_scores", store=True, string="Objective (%)",
    )
    warning_penalty = fields.Float(
        compute="_compute_scores", store=True, string="Warning Penalty (%)",
    )
    final_score = fields.Float(
        compute="_compute_scores", store=True, string="Final (%)",
    )
    has_pending_review = fields.Boolean(
        compute="_compute_scores", store=True,
        help="At least one text/long-text answer is awaiting HR review.",
    )

    portal_url = fields.Char(compute="_compute_portal_url")

    _sql_constraints = [
        ("access_token_uniq", "unique(access_token)", "Access token must be unique."),
    ]

    @api.depends("applicant_id.partner_name", "job_id.name")
    def _compute_display_name(self):
        for rec in self:
            appl = rec.applicant_id.partner_name or _("Unknown")
            rec.display_name = (
                f"{appl} - {rec.job_id.name}" if rec.job_id else appl
            )

    @api.depends(
        "question_ids",
        "answer_ids", "answer_ids.is_answered",
        "warning_ids",
        "snapshot_ids",
    )
    def _compute_counts(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)
            rec.answered_count = len(rec.answer_ids.filtered("is_answered"))
            rec.warning_count = len(rec.warning_ids)
            rec.snapshot_count = len(rec.snapshot_ids)

    @api.depends("media_errors_json")
    def _compute_media_errors(self):
        for rec in self:
            try:
                rec.media_error_count = len(json.loads(rec.media_errors_json or "[]"))
            except (ValueError, TypeError):
                rec.media_error_count = 0

    def record_consent(self, version="v1"):
        self.ensure_one()
        if self.state != "in_progress":
            return {"at": self.consent_at, "version": self.consent_version}
        if not self.consent_at:
            self.write({
                "consent_at": fields.Datetime.now(),
                "consent_version": (version or "v1")[:16],
            })
        return {"at": self.consent_at, "version": self.consent_version}

    def record_media_error(self, step, status_code=None, message=""):
        self.ensure_one()
        if self.state != "in_progress":
            return False
        try:
            existing = json.loads(self.media_errors_json or "[]")
            if not isinstance(existing, list):
                existing = []
        except (ValueError, TypeError):
            existing = []
        try:
            code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            code = None
        existing.append({
            "step": step if step in _MEDIA_ERROR_STEPS else "other",
            "status": code,
            "message": (message or "")[:200],
            "at": fields.Datetime.now().isoformat(),
        })
        self.media_errors_json = json.dumps(existing[-_MEDIA_ERROR_CAP:])
        return True

    @api.depends(
        "answer_ids.score", "answer_ids.needs_review",
        "warning_ids.penalty_percent",
        "max_score", "state",
    )
    def _compute_scores(self):
        for rec in self:
            earned = sum(rec.answer_ids.mapped("score"))
            total = rec.max_score or sum(rec.question_ids.mapped("marks")) or 0
            obj = (earned / total * 100.0) if total else 0.0
            penalty = min(100.0, sum(rec.warning_ids.mapped("penalty_percent")))
            rec.objective_score = obj
            rec.warning_penalty = penalty
            rec.final_score = max(0.0, obj - penalty)
            rec.has_pending_review = any(a.needs_review for a in rec.answer_ids)

    @api.depends("state", "final_score", "pass_mark_percent")
    def _compute_result(self):
        for rec in self:
            if rec.state != "scored":
                rec.result = "pending"
                continue
            threshold = rec.pass_mark_percent or 0.0
            rec.result = "pass" if rec.final_score >= threshold else "fail"

    def _compute_portal_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            rec.portal_url = (
                f"{base}/applicant-assessment/{rec.access_token}"
                if rec.access_token else ""
            )

    def _snapshot_from_template(self):
        Section = self.env["etp.applicant.assessment.section"]
        Question = self.env["etp.applicant.assessment.question"]
        Option = self.env["etp.applicant.assessment.option"]
        for rec in self:
            rec.section_ids.unlink()
            rec.question_ids.unlink()
            t = rec.template_id
            template_sections = t.section_ids.sorted("sequence")
            counter = 10

            if template_sections:
                buckets = []
                for t_sec in template_sections:
                    buckets.append(
                        (t_sec, list(t_sec.question_ids.sorted("sequence")))
                    )
                orphans = t.question_ids.filtered(lambda q: not q.section_id)
                if orphans:
                    buckets.append((None, list(orphans.sorted("sequence"))))
            else:
                buckets = [(None, list(t.question_ids.sorted("sequence")))]

            for t_sec, questions in buckets:
                if t_sec:
                    sec_name = t_sec.name
                    sec_seq = t_sec.sequence
                    sec_desc = t_sec.description or ""
                else:
                    sec_name = "Questions"
                    sec_seq = counter
                    sec_desc = ""
                a_sec = Section.create({
                    "assessment_id": rec.id,
                    "sequence": sec_seq,
                    "name": sec_name,
                    "description": sec_desc,
                })
                if t.shuffle_questions and questions:
                    questions = list(questions)
                    random.shuffle(questions)
                for tq in questions:
                    q = Question.create({
                        "assessment_id": rec.id,
                        "section_id": a_sec.id,
                        "sequence": counter,
                        "prompt": tq.prompt,
                        "question_type": tq.question_type,
                        "marks": tq.marks,
                        "negative_marks": tq.negative_marks,
                    })
                    counter += 10
                    for opt in tq.option_ids.sorted("sequence"):
                        Option.create({
                            "question_id": q.id,
                            "sequence": opt.sequence,
                            "label": opt.label,
                            "is_correct": opt.is_correct,
                        })

            rec.write({
                "duration_minutes": t.duration_minutes,
                "pass_mark_percent": t.pass_mark_percent,
                "warning_cap": t.warning_cap,
                "require_webcam": t.require_webcam,
                "require_mic": t.require_mic,
                "require_fullscreen": t.require_fullscreen,
                "block_copy_paste": t.block_copy_paste,
                "block_right_click": t.block_right_click,
                "detect_window_switch": t.detect_window_switch,
                "detect_no_face": t.detect_no_face,
                "detect_other_person": t.detect_other_person,
                "detect_look_away": t.detect_look_away,
                "detect_lip_movement": t.detect_lip_movement,
                "detect_mobile_phone": t.detect_mobile_phone,
                "shuffle_questions": t.shuffle_questions,
                "max_score": sum(tq.marks for tq in t.question_ids),
            })

    def action_send(self):
        for rec in self:
            if rec.state not in ("draft", "sent"):
                raise UserError(
                    _("Cannot send assessment in state '%s'.") % rec.state
                )
            if not rec.template_id:
                raise UserError(_("No assessment template on this record."))
            if not rec.question_ids:
                rec._snapshot_from_template()
            rec.state = "sent"
            rec._send_invitation_email()
        return True

    def _send_invitation_email(self):
        template = self.env.ref(
            "etp_applicant_assessment.mail_template_applicant_assessment",
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "Applicant-assessment invitation template not found for %s",
                self.ids,
            )
            return
        for rec in self:
            recipient = rec.applicant_id.email_from
            if not recipient:
                _logger.warning(
                    "Applicant %s has no email_from; assessment %s email skipped",
                    rec.applicant_id.id, rec.id,
                )
                continue
            try:
                template.send_mail(rec.id, force_send=True, raise_exception=False)
            except Exception:
                _logger.exception(
                    "Failed to send assessment invitation email for %s", rec.id
                )

    def action_begin(self):
        self.ensure_one()
        if self.state not in ("sent", "in_progress"):
            raise UserError(
                _("Cannot begin assessment in state '%s'.") % self.state
            )
        if not self.started_at:
            self.started_at = fields.Datetime.now()
            if self.duration_minutes and self.duration_minutes > 0:
                self.deadline_at = self.started_at + timedelta(
                    minutes=self.duration_minutes
                )
        if self.state == "sent":
            self.state = "in_progress"
        return True

    def action_submit(self):
        for rec in self:
            if rec.state not in ("sent", "in_progress"):
                continue
            rec.submitted_at = fields.Datetime.now()
            rec.state = "submitted"
            rec.action_score()
        return True

    def action_score(self):
        for rec in self:
            if rec.state != "submitted":
                continue
            if rec.has_pending_review:
                continue
            rec.state = "scored"
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state in ("scored", "cancelled"):
                continue
            rec.state = "cancelled"
        return True

    def action_reset_draft(self):
        for rec in self:
            if rec.state != "cancelled":
                raise UserError(
                    _("Only cancelled assessments can be reset to draft.")
                )
            rec.state = "draft"
        return True

    def action_open_portal_link(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": self.portal_url,
            "target": "new",
        }

    def _check_time_expired(self):
        self.ensure_one()
        if not self.deadline_at:
            return False
        return fields.Datetime.now() > self.deadline_at

    def _maybe_auto_submit(self):
        for rec in self:
            if rec.state != "in_progress":
                continue
            cap = rec.warning_cap
            if cap and rec.warning_count >= cap:
                _logger.info(
                    "Auto-submit assessment %s (warning cap %s reached)",
                    rec.id, cap,
                )
                rec.action_submit()

    @api.model
    def _cron_expire_stale_assessments(self):
        stale = self.search([
            ("state", "=", "in_progress"),
            ("deadline_at", "!=", False),
            ("deadline_at", "<", fields.Datetime.now()),
        ])
        for rec in stale:
            try:
                rec.action_submit()
            except Exception:
                _logger.exception(
                    "Auto-submit failed for stale assessment %s", rec.id
                )
        return True
