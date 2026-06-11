# -*- coding: utf-8 -*-
"""Extension of etp.assessment.evaluator (Candidate Assignment).

Adds the §16.2 cohort batch label, the §12.3 last_activity_at, and the §6.6
overall mean / pass-band fields the per-candidate drill-in (SCR-096) needs.
"""
from odoo import api, fields, models


class EtpAssessmentEvaluator(models.Model):
    _inherit = "etp.assessment.evaluator"

    cohort_batch = fields.Char(
        string="Cohort batch",
        help="Batch label for the candidate (e.g. 'Batch A', 'Batch B').",
    )
    last_activity_at = fields.Datetime(
        string="Last activity",
        help="Last submission/login timestamp shown in the drawer header.",
    )

    day_session_ids = fields.One2many(
        "etp.assessment.day.session",
        "evaluator_id",
        string="Day sessions",
    )
    submission_ids = fields.One2many(
        "etp.assessment.submission",
        "evaluator_id",
        string="Submissions",
    )

    overall_mean = fields.Float(
        compute="_compute_overall_mean",
        string="Overall mean",
        digits=(5, 2),
        help="Mean of final_score across this candidate's scored submissions. WORKFLOW §6.6.",
    )
    submitted_total = fields.Integer(
        compute="_compute_overall_mean",
        string="Submitted total",
    )
    submissions_expected = fields.Integer(
        compute="_compute_overall_mean",
        string="Submissions expected",
    )
    is_at_risk = fields.Boolean(
        compute="_compute_overall_mean",
        string="At risk",
        help="True when overall mean < pass threshold or window started + nothing submitted.",
    )
    review_count = fields.Integer(
        compute="_compute_overall_mean",
        string="Submissions to review",
        help="Count of own low-confidence submissions — drives the '{n} to review' jump pill.",
    )

    @api.depends(
        "submission_ids",
        "submission_ids.final_score",
        "submission_ids.low_confidence",
        "submission_ids.state",
        "assessment_id.pass_threshold",
        "assessment_id.period_days",
        "assessment_id.questions_per_day",
    )
    def _compute_overall_mean(self):
        for rec in self:
            graded = rec.submission_ids.filtered(
                lambda s: s.state in ("scored", "overridden") and s.final_score is not False
            )
            count = len(graded)
            if count:
                rec.overall_mean = sum(s.final_score or 0 for s in graded) / count
            else:
                rec.overall_mean = 0.0
            rec.submitted_total = len(rec.submission_ids.filtered(
                lambda s: s.state in ("submitted", "scored", "overridden")
            ))
            assessment = rec.assessment_id
            expected = 0
            if assessment:
                expected = (assessment.period_days or 0) * (assessment.questions_per_day or 0)
            rec.submissions_expected = expected
            rec.review_count = len(rec.submission_ids.filtered("low_confidence"))
            threshold = (rec.assessment_id.pass_threshold or 70) if rec.assessment_id else 70
            rec.is_at_risk = bool(
                (count > 0 and rec.overall_mean < threshold)
                or (rec.submitted_total < expected and assessment and assessment.state == "in_progress")
            )
