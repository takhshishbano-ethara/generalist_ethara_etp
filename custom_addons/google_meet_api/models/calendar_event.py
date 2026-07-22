from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

MAX_INTERVIEW_ROUNDS = 5

WEIGHTS = {
    "technical_skills_score": 0.30,
    "communication_score": 0.20,
    "problem_solving_score": 0.20,
    "cultural_fit_score": 0.15,
    "attitude_motivation_score": 0.15,
}


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    candidate_id = fields.Many2one(
        "hr.applicant",
        string="Candidate",
        index=True,
        help="Interview candidate (hr.applicant). Enables per-round evaluation.",
    )
    interview_round = fields.Integer(
        compute="_compute_interview_round",
        store=True,
        help="Ordinal 1-5, computed by chronological start order for this candidate.",
    )
    technical_skills_score = fields.Integer(string="Technical Skills (30%)")
    communication_score = fields.Integer(string="Communication (20%)")
    problem_solving_score = fields.Integer(string="Problem Solving (20%)")
    cultural_fit_score = fields.Integer(string="Cultural Fit (15%)")
    attitude_motivation_score = fields.Integer(string="Attitude & Motivation (15%)")
    evaluation_notes = fields.Text()
    overall_score = fields.Float(
        compute="_compute_overall_score",
        store=True,
        digits=(3, 2),
        help="Weighted average out of 10.",
    )
    evaluation_submitted = fields.Boolean(
        compute="_compute_overall_score",
        store=True,
        help="True when at least one score is set.",
    )
    rescheduled_count = fields.Integer(default=0)
    interview_status = fields.Selection([
        ("upcoming", "Upcoming"),
        ("in_progress", "In Progress"),
        ("needs_evaluation", "Needs Evaluation"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ], compute="_compute_interview_status", store=True)

    @api.depends("active", "start", "stop", "evaluation_submitted")
    def _compute_interview_status(self):
        now = fields.Datetime.now()
        for rec in self:
            if not rec.active:
                rec.interview_status = "cancelled"
                continue
            if not rec.start or not rec.stop:
                rec.interview_status = "upcoming"
                continue
            if rec.start > now:
                rec.interview_status = "upcoming"
            elif rec.stop < now:
                rec.interview_status = "completed" if rec.evaluation_submitted else "needs_evaluation"
            else:
                rec.interview_status = "in_progress"

    @api.depends("candidate_id", "start")
    def _compute_interview_round(self):
        for rec in self:
            if not rec.candidate_id:
                rec.interview_round = 0
                continue
            siblings = self.search(
                [("candidate_id", "=", rec.candidate_id.id)],
                order="start asc, id asc",
            )
            for idx, sibling in enumerate(siblings, start=1):
                if sibling.id == rec.id:
                    rec.interview_round = idx
                    break
            else:
                rec.interview_round = len(siblings) + 1

    @api.depends(
        "technical_skills_score",
        "communication_score",
        "problem_solving_score",
        "cultural_fit_score",
        "attitude_motivation_score",
    )
    def _compute_overall_score(self):
        for rec in self:
            total = 0.0
            has_any = False
            for field_name, weight in WEIGHTS.items():
                value = getattr(rec, field_name) or 0
                if value:
                    has_any = True
                total += value * weight
            rec.overall_score = round(total, 2)
            rec.evaluation_submitted = has_any

    @api.constrains("candidate_id")
    def _check_max_interview_rounds(self):
        for rec in self:
            if not rec.candidate_id:
                continue
            count = self.search_count([
                ("candidate_id", "=", rec.candidate_id.id),
            ])
            if count > MAX_INTERVIEW_ROUNDS:
                raise ValidationError(_(
                    "Candidate '%(name)s' already has %(max)d interview rounds "
                    "(maximum allowed).",
                    name=rec.candidate_id.partner_name or rec.candidate_id.name,
                    max=MAX_INTERVIEW_ROUNDS,
                ))

    @api.constrains(
        "technical_skills_score",
        "communication_score",
        "problem_solving_score",
        "cultural_fit_score",
        "attitude_motivation_score",
    )
    def _check_score_range(self):
        for rec in self:
            for field_name in WEIGHTS:
                value = getattr(rec, field_name)
                if value and not (1 <= value <= 10):
                    raise ValidationError(_(
                        "%(field)s must be between 1 and 10.",
                        field=self._fields[field_name].string,
                    ))
