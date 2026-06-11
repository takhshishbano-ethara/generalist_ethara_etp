# -*- coding: utf-8 -*-
"""One Submission per (candidate, question) — the heart of SCR-096/097.

WORKFLOW §12.4 (data model), §6 (scoring). Mirrors the spec's field set:
llm_score, confidence, low_confidence, override_score/by/at/reason,
final_score, llm_rationale, sub_scores, answer_payload. The Submission
lifecycle (§7.4) Submitted → Scored → Overridden is mapped 1:1.
"""
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EtpAssessmentSubmission(models.Model):
    _name = "etp.assessment.submission"
    _description = "Assessment Submission (per candidate, per question)"
    _order = "create_date desc"
    _sql_constraints = [
        (
            "uniq_evaluator_question",
            "UNIQUE(evaluator_id, question_id)",
            "Only one submission per candidate per question.",
        ),
    ]

    code = fields.Char(string="Submission Code", help="Mono id (SUB-######).", copy=False, index=True)

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade", index=True
    )
    evaluator_id = fields.Many2one(
        "etp.assessment.evaluator", required=True, ondelete="cascade", index=True
    )
    employee_id = fields.Many2one(
        "hr.employee", related="evaluator_id.employee_id", store=True, readonly=True
    )
    day_session_id = fields.Many2one(
        "etp.assessment.day.session", ondelete="set null", index=True
    )
    day_number = fields.Integer(related="day_session_id.day_number", store=True)
    question_id = fields.Many2one(
        "etp.assessment.question", required=True, ondelete="restrict", index=True
    )
    task_type = fields.Selection(
        related="question_id.task_type", store=True
    )

    state = fields.Selection(
        [
            ("submitted", "Submitted"),
            ("scored", "Scored"),
            ("overridden", "Overridden"),
            ("not_submitted", "Not submitted"),
        ],
        default="not_submitted",
        required=True,
    )

    answer_payload = fields.Text(
        string="Answer payload (JSON)",
        help="The candidate's submitted answer — type-specific. JSON-encoded.",
    )
    answer_summary = fields.Char(
        string="Answer summary",
        help="One-line gist rendered on each row of the day's question list.",
    )

    llm_score = fields.Integer(string="LLM score", help="0-100. WORKFLOW §6.1.")
    confidence = fields.Float(
        string="Confidence",
        digits=(4, 2),
        help="0-1. Below low_confidence_threshold flags the row. §6.3.",
    )
    low_confidence = fields.Boolean(
        compute="_compute_low_confidence",
        store=True,
        string="Low confidence",
    )
    llm_rationale = fields.Text(string="LLM rationale")
    sub_scores = fields.Text(
        string="Sub-scores (JSON)",
        help='Per-type breakdown e.g. {"detection": 29, "functionality": 30}. §6.2.',
    )

    override_score = fields.Integer(string="Override score", help="0-100. §6.4.")
    override_by = fields.Many2one("res.users", string="Overridden by")
    override_at = fields.Datetime(string="Overridden at")
    override_reason = fields.Selection(
        [
            ("llm_synonym", "LLM penalized a correct synonym"),
            ("misdetected_boxes", "Mis-detected boxes"),
            ("justification_valid", "Justification valid"),
            ("other", "Other"),
        ],
        string="Override reason",
    )
    override_note = fields.Text(string="Override note")
    item_result = fields.Selection(
        [
            ("auto", "Auto"),
            ("pass", "Pass"),
            ("fail", "Fail"),
        ],
        default="auto",
        help="Optional explicit pass/fail tag from MOD-Score-Override §3.5.",
    )

    final_score = fields.Integer(
        compute="_compute_final_score",
        store=True,
        string="Final score",
        help="override_score when set, else llm_score. WORKFLOW §6.1.",
    )

    submitted_at = fields.Datetime(string="Submitted at")
    scored_at = fields.Datetime(string="Scored at")

    @api.depends("confidence", "assessment_id.low_confidence_threshold")
    def _compute_low_confidence(self):
        for rec in self:
            threshold = rec.assessment_id.low_confidence_threshold if rec.assessment_id else 0.6
            rec.low_confidence = bool(rec.confidence and rec.confidence < threshold)

    @api.depends("llm_score", "override_score", "state")
    def _compute_final_score(self):
        for rec in self:
            if rec.state == "overridden" and rec.override_score:
                rec.final_score = rec.override_score
            else:
                rec.final_score = rec.llm_score or 0

    def parsed_answer_payload(self):
        self.ensure_one()
        if not self.answer_payload:
            return None
        try:
            return json.loads(self.answer_payload)
        except (TypeError, ValueError):
            return self.answer_payload

    def parsed_sub_scores(self):
        self.ensure_one()
        if not self.sub_scores:
            return {}
        try:
            return json.loads(self.sub_scores)
        except (TypeError, ValueError):
            return {}

    def apply_override(
        self,
        new_score,
        reason,
        note=None,
        item_result=None,
        actor_user=None,
    ):
        """Commit an override directly (non-self-case path). WORKFLOW §6.4.

        Returns the recomputed final_score. Caller is responsible for the §6.5
        self-case guard — when the candidate reports to the PL the override
        request must be routed through the override-request queue instead.
        """
        self.ensure_one()
        if new_score is None:
            raise UserError(_("Override score is required."))
        try:
            new_score = int(new_score)
        except (TypeError, ValueError):
            raise UserError(_("Override score must be an integer 0-100."))
        if new_score < 0 or new_score > 100:
            raise ValidationError(_("Score must be 0-100."))
        threshold = self.assessment_id.override_delta_threshold or 10
        if abs(new_score - (self.llm_score or 0)) > threshold and not reason:
            raise UserError(_("A reason is required for a change this large."))

        self.write(
            {
                "override_score": new_score,
                "override_by": (actor_user or self.env.user).id,
                "override_at": fields.Datetime.now(),
                "override_reason": reason or False,
                "override_note": note or False,
                "item_result": item_result or "auto",
                "state": "overridden",
            }
        )
        return self.final_score

    @api.model
    def is_self_case(self, candidate_employee, actor_user):
        """True when the PL trying to override is the candidate's direct manager.

        WORKFLOW §6.5. HR Admin / CTO never trip the guard; for them this is
        always False so they can commit directly.
        """
        if not candidate_employee or not actor_user:
            return False
        if actor_user.has_group("base.group_system"):
            return False
        if actor_user.has_group("assessment_extension.group_assessment_cto"):
            return False
        if actor_user.has_group("assessment_extension.group_assessment_hr_admin"):
            return False
        manager = candidate_employee.parent_id
        if not manager:
            return False
        return manager.user_id and manager.user_id.id == actor_user.id
