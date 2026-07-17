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

_LLM_QC_TARGET_TYPES = frozenset({
    "text", "long_text", "fill_blank", "numeric", "code",
    "file_upload", "url", "matching", "ordering", "rating",
    "date", "consent",
})
_LLM_QC_ANSWER_TRUNCATE = 4000
_LLM_QC_MAX_ATTEMPTS = 3
_LLM_QC_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_LLM_QC_DEFAULT_MODEL = "llama-3.3-70b-versatile"
_LLM_QC_DEFAULT_TIMEOUT = 60
_LLM_QC_DEFAULT_SEED_PROMPT = (
    "You are a strict evaluator for candidate assessment answers. "
    "For each question given, judge how well the candidate's answer "
    "meets the intent of the question and return a percentage 0-100 "
    "for how correct/complete the answer is. Use 0 for irrelevant or "
    "empty answers, 100 for perfect answers."
)


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

    llm_qc_state = fields.Selection(
        [
            ("skipped", "Skipped"),
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("partial", "Partial"),
            ("failed", "Failed"),
        ],
        default="skipped", copy=False, index=True, tracking=True,
        string="LLM QC State",
    )
    llm_qc_error = fields.Text(readonly=True, copy=False, string="LLM QC Error")
    llm_qc_ran_at = fields.Datetime(readonly=True, copy=False, string="LLM QC Ran At")
    llm_qc_attempts = fields.Integer(default=0, copy=False, readonly=True)
    llm_qc_result_email_sent = fields.Boolean(default=False, copy=False, readonly=True)

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

    @api.model
    def create_bulk_for_template(self, template_id, applicant_ids):
        Template = self.env["etp.applicant.assessment.template"]
        Applicant = self.env["hr.applicant"]

        try:
            template_id = int(template_id)
        except (TypeError, ValueError):
            raise UserError(_("template_id must be an integer."))
        template = Template.browse(template_id).exists()
        if not template:
            raise UserError(
                _("Assessment template %d does not exist.") % template_id
            )

        if not isinstance(applicant_ids, (list, tuple)) or not applicant_ids:
            raise UserError(_("applicant_ids must be a non-empty list."))

        cleaned_ids = []
        bad_ids = []
        for raw in applicant_ids:
            try:
                cleaned_ids.append(int(raw))
            except (TypeError, ValueError):
                bad_ids.append(raw)
        if bad_ids:
            raise UserError(
                _("applicant_ids must contain integers only; bad entries: %s")
                % (bad_ids,)
            )

        applicants = Applicant.browse(cleaned_ids)
        existing_ids = set(applicants.exists().ids)

        created = []
        reused = []
        skipped = []
        seen = set()

        for applicant_id in cleaned_ids:
            if applicant_id in seen:
                continue
            seen.add(applicant_id)

            if applicant_id not in existing_ids:
                skipped.append({
                    "applicant_id": applicant_id,
                    "reason": "applicant_not_found",
                })
                continue

            applicant = Applicant.browse(applicant_id)
            job = applicant.job_id

            active = self.search([
                ("applicant_id", "=", applicant_id),
                ("template_id", "=", template.id),
                ("state", "in", ("draft", "sent", "in_progress")),
            ], limit=1)
            if active:
                reused.append({
                    "applicant_id": applicant_id,
                    "assessment_id": active.id,
                    "state": active.state,
                })
                continue

            if not job:
                skipped.append({
                    "applicant_id": applicant_id,
                    "reason": "applicant_has_no_job",
                })
                continue

            try:
                record = self.create({
                    "applicant_id": applicant_id,
                    "job_id": job.id,
                    "template_id": template.id,
                })
                record.action_send()
            except Exception as exc:
                _logger.exception(
                    "Bulk assessment: failed for applicant %s / template %s",
                    applicant_id, template.id,
                )
                skipped.append({
                    "applicant_id": applicant_id,
                    "reason": "create_or_send_failed",
                    "detail": str(exc)[:200],
                })
                continue

            created.append({
                "applicant_id": applicant_id,
                "assessment_id": record.id,
                "email": applicant.email_from or "",
                "email_sent": bool(applicant.email_from),
            })

        return {
            "template_id": template.id,
            "template_name": template.name,
            "requested_count": len(cleaned_ids),
            "created": created,
            "reused": reused,
            "skipped": skipped,
        }

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
                if rec.applicant_id:
                    rec.applicant_id.sudo().status = "pending_assessment"
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
            if self.applicant_id:
                self.applicant_id.sudo().status = "assessment_in_progress"
        return True

    def action_submit(self):
        cron = self.env.ref(
            "etp_applicant_assessment.cron_process_pending_llm_qc",
            raise_if_not_found=False,
        )
        need_trigger = False
        for rec in self:
            if rec.state not in ("sent", "in_progress"):
                continue
            rec.submitted_at = fields.Datetime.now()
            rec.state = "submitted"
            if rec.applicant_id:
                # A stale stage DB missing the new hr.applicant.status
                # selection values, or a downstream write hook that
                # raises, would otherwise surface as a 500 on /submit
                # and lose the candidate's session. Never block the
                # state transition on the applicant sync.
                applicant = rec.applicant_id.sudo()
                try:
                    applicant.write({"status": "assessment_pending_review"})
                    applicant.flush_recordset(["status"])
                    _logger.info(
                        "action_submit: applicant=%s status set to "
                        "assessment_pending_review for assessment=%s",
                        applicant.id, rec.id,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to set applicant status to "
                        "assessment_pending_review for applicant=%s "
                        "assessment=%s (current status=%s)",
                        applicant.id, rec.id, applicant.status,
                    )
            try:
                has_llm_targets = any(
                    a.question_type in _LLM_QC_TARGET_TYPES and a.is_answered
                    for a in rec.answer_ids
                )
            except Exception:
                _logger.exception(
                    "LLM-QC target detection failed for assessment=%s; "
                    "treating as no LLM targets",
                    rec.id,
                )
                has_llm_targets = False
            if has_llm_targets:
                rec.llm_qc_state = "pending"
                need_trigger = True
            else:
                rec.llm_qc_state = "skipped"
                try:
                    rec._finalize_scoring_after_llm_qc()
                except Exception:
                    _logger.exception(
                        "_finalize_scoring_after_llm_qc failed for "
                        "assessment=%s; record left in submitted state "
                        "for manual review",
                        rec.id,
                    )
        if need_trigger and cron:
            try:
                cron.sudo()._trigger()
            except Exception:
                _logger.exception("Failed to trigger LLM QC cron")
        return True

    def action_score(self):
        for rec in self:
            if rec.state != "submitted":
                continue
            if rec.has_pending_review:
                continue
            rec.state = "scored"
            applicant = rec.applicant_id
            if applicant:
                passed = (rec.final_score or 0.0) >= (rec.pass_mark_percent or 0.0)
                applicant.status = (
                    "assessment_passed" if passed
                    else "assessment_rejected"
                )
            rec._send_result_email()
        return True

    def _send_result_email(self):
        template = self.env.ref(
            "etp_applicant_assessment.mail_template_applicant_assessment_result",
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "Applicant-assessment result template not found for %s",
                self.ids,
            )
            return
        for rec in self:
            if rec.state != "scored" or rec.result not in ("pass", "fail"):
                continue
            recipient = rec.applicant_id.email_from
            if not recipient:
                _logger.warning(
                    "Applicant %s has no email_from; assessment %s result email skipped",
                    rec.applicant_id.id, rec.id,
                )
                continue
            try:
                template.send_mail(rec.id, force_send=True, raise_exception=False)
            except Exception:
                _logger.exception(
                    "Failed to send assessment result email for %s", rec.id
                )

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

    def _llm_qc_settings(self):
        ICP = self.env["ir.config_parameter"].sudo()
        get = ICP.get_param
        try:
            timeout = int(get("ethara_hrms.llm_timeout", _LLM_QC_DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = _LLM_QC_DEFAULT_TIMEOUT
        return {
            "prompt": get(
                "etp_applicant_assessment.llm_qc_prompt",
                _LLM_QC_DEFAULT_SEED_PROMPT,
            ) or _LLM_QC_DEFAULT_SEED_PROMPT,
            "base_url": (get("ethara_hrms.llm_base_url", _LLM_QC_DEFAULT_BASE_URL)
                         or _LLM_QC_DEFAULT_BASE_URL),
            "key": (get("ethara_hrms.llm_api_key", "") or "").strip(),
            "model": (get("ethara_hrms.llm_model", _LLM_QC_DEFAULT_MODEL)
                      or _LLM_QC_DEFAULT_MODEL),
            "timeout": max(5, timeout),
        }

    def _serialize_answer_for_llm(self, answer):
        marks = answer.question_id.marks or 0
        qtype = answer.question_type
        payload = {
            "id": answer.id,
            "question_type": qtype,
            "prompt": (answer.question_id.prompt or "")[:2000],
            "marks": marks,
        }
        if qtype in ("text", "long_text", "fill_blank",
                     "numeric", "code", "url", "date"):
            payload["candidate_answer"] = (answer.text_answer or "")[
                :_LLM_QC_ANSWER_TRUNCATE
            ]
        elif qtype in ("matching", "ordering", "rating"):
            payload["candidate_answer"] = [
                {"id": o.id, "label": o.label}
                for o in answer.selected_option_ids.sorted("sequence")
            ]
        elif qtype == "consent":
            payload["candidate_answer"] = bool(
                answer.selected_option_ids or answer.is_answered
            )
        elif qtype == "file_upload":
            payload["candidate_answer"] = [
                {"name": a.name, "size": a.file_size}
                for a in answer.answer_attachment_ids
            ]
        else:
            payload["candidate_answer"] = (answer.text_answer or "")[
                :_LLM_QC_ANSWER_TRUNCATE
            ]
        return payload

    def _build_llm_qc_messages(self, seed_prompt, questions_payload):
        schema_note = (
            "\n\nRespond ONLY with valid JSON of shape "
            '{"results": [{"id": <answer_id_int>, "percent": <number 0-100>, '
            '"reasoning": "<one short sentence>"}]}. '
            "Do not include any text outside the JSON object. "
            "Every input id must appear in results exactly once."
        )
        return [
            {"role": "system", "content": seed_prompt + schema_note},
            {"role": "user", "content": json.dumps(
                {"questions": questions_payload}, ensure_ascii=False,
            )},
        ]

    def _call_llm_qc_api(self, cfg, messages):
        import requests
        base = (cfg["base_url"] or _LLM_QC_DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/chat/completions"
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        }
        resp = requests.post(
            url, headers=headers, json=payload, timeout=cfg["timeout"],
        )
        resp.raise_for_status()
        body = resp.json()
        choices = body.get("choices") or []
        if not choices:
            raise ValueError("LLM response has no choices")
        content = (choices[0].get("message") or {}).get("content") or ""
        parsed = json.loads(content)
        results = parsed.get("results")
        if not isinstance(results, list):
            raise ValueError("LLM response missing 'results' list")
        return results

    def _run_llm_qc(self):
        self.ensure_one()
        cfg = self._llm_qc_settings()
        if not cfg["key"]:
            self.write({
                "llm_qc_state": "failed",
                "llm_qc_error": "LLM QC API key is not configured.",
                "llm_qc_ran_at": fields.Datetime.now(),
                "llm_qc_attempts": (self.llm_qc_attempts or 0) + 1,
            })
            return False

        targets = self.answer_ids.filtered(
            lambda a: a.question_type in _LLM_QC_TARGET_TYPES and a.is_answered
        )
        if not targets:
            self.write({
                "llm_qc_state": "skipped",
                "llm_qc_ran_at": fields.Datetime.now(),
            })
            self._finalize_scoring_after_llm_qc()
            return True

        self.write({
            "llm_qc_state": "running",
            "llm_qc_attempts": (self.llm_qc_attempts or 0) + 1,
        })
        # Flush + commit the running flag so other cron runs don't double-fire.
        self.env.cr.commit()

        questions_payload = [
            self._serialize_answer_for_llm(a) for a in targets
        ]
        messages = self._build_llm_qc_messages(cfg["prompt"], questions_payload)

        try:
            results = self._call_llm_qc_api(cfg, messages)
        except Exception as exc:
            _logger.exception("LLM QC call failed for assessment %s", self.id)
            self.write({
                "llm_qc_state": "failed",
                "llm_qc_error": str(exc)[:2000],
                "llm_qc_ran_at": fields.Datetime.now(),
            })
            return False

        target_by_id = {a.id: a for a in targets}
        scored_ids = set()
        now = fields.Datetime.now()
        for entry in results:
            try:
                aid = int(entry.get("id"))
                pct = float(entry.get("percent"))
            except (TypeError, ValueError):
                continue
            answer = target_by_id.get(aid)
            if not answer:
                continue
            pct = max(0.0, min(100.0, pct))
            answer.write({
                "llm_qc_percent": pct,
                "llm_qc_reasoning": (entry.get("reasoning") or "")[:2000],
                "llm_qc_set": True,
                "llm_qc_at": now,
                "llm_qc_error": False,
            })
            scored_ids.add(aid)

        missing = [aid for aid in target_by_id if aid not in scored_ids]
        state = "done" if not missing else "partial"
        self.write({
            "llm_qc_state": state,
            "llm_qc_error": (
                f"LLM did not return scores for answer ids: {missing}"
                if missing else False
            ),
            "llm_qc_ran_at": now,
        })
        self._finalize_scoring_after_llm_qc()
        return True

    def _finalize_scoring_after_llm_qc(self):
        for rec in self:
            if rec.state != "submitted":
                continue
            if rec.has_pending_review:
                continue
            rec.state = "scored"
            applicant = rec.applicant_id
            if applicant:
                passed = (
                    (rec.final_score or 0.0) >= (rec.pass_mark_percent or 0.0)
                )
                applicant.sudo().status = (
                    "assessment_passed" if passed
                    else "assessment_rejected"
                )
            if not rec.llm_qc_result_email_sent:
                rec._send_llm_qc_result_email()
        return True

    def _send_llm_qc_result_email(self):
        template = self.env.ref(
            "etp_applicant_assessment.mail_template_applicant_assessment_result",
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "Result mail template not found for assessment %s", self.ids,
            )
            return
        for rec in self:
            recipient = rec.applicant_id.email_from if rec.applicant_id else None
            if not recipient:
                _logger.warning(
                    "Assessment %s has no candidate email; result mail skipped",
                    rec.id,
                )
                continue
            try:
                template.send_mail(
                    rec.id, force_send=True, raise_exception=False,
                )
                rec.llm_qc_result_email_sent = True
            except Exception:
                _logger.exception(
                    "Failed to send result email for assessment %s", rec.id,
                )
        return True

    def action_rerun_llm_qc(self):
        self.ensure_one()
        if self.state not in ("submitted", "scored"):
            raise UserError(
                _("Cannot re-run LLM QC in state '%s'.") % self.state
            )
        self.write({
            "llm_qc_state": "pending",
            "llm_qc_error": False,
            "llm_qc_result_email_sent": False,
        })
        cron = self.env.ref(
            "etp_applicant_assessment.cron_process_pending_llm_qc",
            raise_if_not_found=False,
        )
        if cron:
            try:
                cron.sudo()._trigger()
            except Exception:
                _logger.exception("Failed to trigger LLM QC cron")
        return True

    @api.model
    def _cron_process_pending_llm_qc(self, limit=20):
        pending = self.search(
            [
                ("state", "=", "submitted"),
                ("llm_qc_state", "=", "pending"),
                ("llm_qc_attempts", "<", _LLM_QC_MAX_ATTEMPTS),
            ],
            order="id asc",
            limit=limit,
        )
        for rec in pending:
            try:
                rec._run_llm_qc()
            except Exception:
                _logger.exception(
                    "Unhandled error running LLM QC for assessment %s", rec.id,
                )
                try:
                    rec.write({
                        "llm_qc_state": "failed",
                        "llm_qc_error": "Unhandled error \u2014 see server log.",
                        "llm_qc_ran_at": fields.Datetime.now(),
                    })
                except Exception:
                    _logger.exception(
                        "Also failed to mark assessment %s as failed", rec.id,
                    )
        return True
