# -*- coding: utf-8 -*-
"""Extension of etp.assessment for the monitoring screens (SCR-095..099).

Adds the fields that ASSESSMENT-WORKFLOW.md §12.1 (Assessment) and §6 (Scoring)
require but the base etp_assessment module does not carry yet — pass threshold,
multi-day window, low-confidence threshold, and the §6.4 override delta gate.
"""
from odoo import api, fields, models


class EtpAssessment(models.Model):
    _inherit = "etp.assessment"

    code = fields.Char(
        string="Assessment Code",
        help="Stable monospace id shown on every drill-in (e.g. ASM-0042).",
        copy=False,
        index=True,
    )
    cohort_label = fields.Char(
        string="Cohort Label",
        help="Display label for the cohort (e.g. 'New-Hire Generalist — June Cohort').",
    )

    pass_threshold = fields.Integer(
        string="Pass Threshold",
        default=70,
        help="Score (0-100) at or above which a submission is 'Passed'. WORKFLOW §6.6.",
    )
    period_days = fields.Integer(
        string="Period (days)",
        default=5,
        help="Length of the assessment window in days. WORKFLOW §4.",
    )
    questions_per_day = fields.Integer(
        string="Questions per day",
        default=25,
        help="Number of questions in each day-test. WORKFLOW §4.",
    )
    low_confidence_threshold = fields.Float(
        string="Low-confidence threshold",
        default=0.6,
        help="confidence < this value flags a submission low-confidence. WORKFLOW §6.3.",
    )
    override_delta_threshold = fields.Integer(
        string="Override delta threshold",
        default=10,
        help="|override - llm_score| above which a reason is required. WORKFLOW §6.4.",
    )

    day_session_ids = fields.One2many(
        "etp.assessment.day.session",
        "assessment_id",
        string="Day sessions",
    )
    submission_ids = fields.One2many(
        "etp.assessment.submission",
        "assessment_id",
        string="Submissions",
    )

    monitor_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("locked", "Questions Locked"),
            ("scheduled", "Scheduled"),
            ("live", "Live"),
            ("completed", "Completed"),
            ("archived", "Archived"),
        ],
        compute="_compute_monitor_state",
        store=False,
        help="UI-facing monitor lifecycle status. WORKFLOW §7.1.",
    )

    at_risk_count = fields.Integer(
        compute="_compute_at_risk_count",
        string="At-risk candidates",
    )

    @api.depends("state", "start_date", "end_date")
    def _compute_monitor_state(self):
        now = fields.Datetime.now()
        for rec in self:
            base = rec.state
            if base == "draft":
                rec.monitor_state = "draft"
            elif base == "cancelled":
                rec.monitor_state = "archived"
            elif base == "done":
                rec.monitor_state = "completed"
            else:
                if rec.start_date and rec.start_date > now:
                    rec.monitor_state = "scheduled"
                elif rec.start_date and rec.start_date <= now and (
                    not rec.end_date or rec.end_date >= now
                ):
                    rec.monitor_state = "live"
                else:
                    rec.monitor_state = "completed"

    @api.depends("assessment_evaluator_ids", "submission_ids")
    def _compute_at_risk_count(self):
        for rec in self:
            count = 0
            for evaluator in rec.assessment_evaluator_ids:
                if evaluator.is_at_risk:
                    count += 1
            rec.at_risk_count = count

    def get_pass_band_color(self, score):
        """Return the colour key for a score band per the design alignment.

        ≥80 success / 70-79 info / 60-69 warning / <60 destructive / None -> muted.
        Used by both API responses and OWL components to stay consistent.
        """
        if score is None:
            return "muted"
        if score >= 80:
            return "success"
        if score >= self.pass_threshold:
            return "info"
        if score >= 60:
            return "warning"
        return "destructive"
