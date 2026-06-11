# -*- coding: utf-8 -*-
"""Per-day session per candidate — drives the SCR-096 5-row breakdown.

WORKFLOW §7.5 (DaySession), §12.5 (data model). Each row holds the day status
pill, the submitted fraction, the day mean, and the per-type means rendered on
the day-row chips ('Eval 61 · Prompt 55 · BBox 49').
"""
from odoo import api, fields, models


class EtpAssessmentDaySession(models.Model):
    _name = "etp.assessment.day.session"
    _description = "Per-day session (one row per candidate per day)"
    _order = "assessment_id, evaluator_id, day_number"
    _sql_constraints = [
        (
            "uniq_evaluator_day",
            "UNIQUE(assessment_id, evaluator_id, day_number)",
            "Only one day session per candidate per day per assessment.",
        ),
    ]

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade", index=True
    )
    evaluator_id = fields.Many2one(
        "etp.assessment.evaluator", required=True, ondelete="cascade", index=True
    )
    employee_id = fields.Many2one(
        "hr.employee",
        related="evaluator_id.employee_id",
        store=True,
        readonly=True,
    )
    day_number = fields.Integer(required=True)
    day_date = fields.Date()

    submission_ids = fields.One2many(
        "etp.assessment.submission", "day_session_id", string="Submissions"
    )

    questions_per_day = fields.Integer(
        related="assessment_id.questions_per_day", store=False
    )
    submitted_count = fields.Integer(
        compute="_compute_aggregates", string="Submitted"
    )
    day_mean = fields.Float(
        compute="_compute_aggregates", string="Day mean", digits=(5, 2)
    )
    eval_mean = fields.Float(
        compute="_compute_aggregates", string="Eval mean", digits=(5, 2)
    )
    prompt_mean = fields.Float(
        compute="_compute_aggregates", string="Prompt mean", digits=(5, 2)
    )
    bbox_mean = fields.Float(
        compute="_compute_aggregates", string="BBox mean", digits=(5, 2)
    )

    status = fields.Selection(
        [
            ("locked", "Locked"),
            ("in_progress", "In progress"),
            ("submitted", "Submitted"),
            ("scored", "Scored"),
            ("passed", "Passed"),
            ("failed", "Failed"),
            ("incomplete", "Incomplete"),
        ],
        compute="_compute_status",
        store=False,
    )

    @api.depends(
        "submission_ids",
        "submission_ids.final_score",
        "submission_ids.state",
        "submission_ids.question_id.task_type",
    )
    def _compute_aggregates(self):
        for rec in self:
            graded = rec.submission_ids.filtered(
                lambda s: s.state in ("scored", "overridden") and s.final_score is not False
            )
            rec.submitted_count = len(rec.submission_ids.filtered(
                lambda s: s.state in ("submitted", "scored", "overridden")
            ))
            if graded:
                rec.day_mean = sum(s.final_score or 0 for s in graded) / len(graded)
            else:
                rec.day_mean = 0.0
            for task_type, attr in [
                ("eval_compare", "eval_mean"),
                ("prompt_writing", "prompt_mean"),
                ("bbox_labeling", "bbox_mean"),
            ]:
                bucket = graded.filtered(lambda s, t=task_type: s.question_id.task_type == t)
                if bucket:
                    setattr(rec, attr, sum(s.final_score or 0 for s in bucket) / len(bucket))
                else:
                    setattr(rec, attr, 0.0)

    @api.depends(
        "day_date",
        "submitted_count",
        "day_mean",
        "questions_per_day",
        "assessment_id.start_date",
        "assessment_id.end_date",
        "assessment_id.pass_threshold",
    )
    def _compute_status(self):
        now = fields.Datetime.now().date()
        for rec in self:
            day_date = rec.day_date
            expected = rec.questions_per_day or 0
            threshold = rec.assessment_id.pass_threshold or 70
            if day_date and day_date > now:
                rec.status = "locked"
                continue
            if day_date and day_date == now and rec.submitted_count < expected:
                rec.status = "in_progress"
                continue
            if rec.submitted_count == 0 and day_date and day_date < now:
                rec.status = "incomplete"
                continue
            if rec.submitted_count < expected:
                rec.status = "incomplete"
                continue
            if rec.day_mean >= threshold:
                rec.status = "passed"
            else:
                rec.status = "failed"
