from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class HrApplicantEvaluation(models.Model):
    _name = "hr.applicant.evaluation"
    _description = "Candidate Evaluation Workflow"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "candidate_id"

    candidate_id = fields.Many2one(
        "hr.applicant",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    evaluator_id = fields.Many2one("res.users", tracking=True)
    job_id = fields.Many2one(related="candidate_id.job_id", store=True, readonly=True)

    pi_score = fields.Float(
        compute="_compute_pi_score",
        store=True,
        digits=(4, 2),
        help="Auto-computed: average of calendar.event.overall_score across completed rounds × 10 (0-100 scale)",
    )
    pms_score = fields.Float(tracking=True, help="Post-hire performance rating 0-100 set by HR")

    recommendation = fields.Selection([
        ("pass", "Pass"),
        ("fail", "Fail"),
        ("maybe", "Maybe"),
    ], tracking=True)
    final_verdict = fields.Selection([
        ("hire", "Hire"),
        ("reject", "Reject"),
        ("hold", "On Hold"),
    ], tracking=True)
    status = fields.Selection([
        ("draft", "Draft"),
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("submitted", "Submitted"),
        ("completed", "Completed"),
        ("pi_bypassed", "PI Bypassed"),
    ], default="draft", tracking=True)

    notes = fields.Text(tracking=True)
    bypass_reason = fields.Text(tracking=True)
    completed_at = fields.Datetime(tracking=True)

    rounds_count = fields.Integer(compute="_compute_round_counts")
    rounds_completed = fields.Integer(compute="_compute_round_counts")

    _sql_constraints = [
        ("candidate_uniq", "unique(candidate_id)",
         "An evaluation already exists for this candidate."),
    ]

    def _compute_round_counts(self):
        Event = self.env["calendar.event"].sudo()
        for rec in self:
            if not rec.candidate_id:
                rec.rounds_count = 0
                rec.rounds_completed = 0
                continue
            rec.rounds_count = Event.search_count([("candidate_id", "=", rec.candidate_id.id)])
            rec.rounds_completed = Event.search_count([
                ("candidate_id", "=", rec.candidate_id.id),
                ("interview_status", "in", ["completed", "needs_evaluation"]),
            ])

    @api.depends("candidate_id")
    def _compute_pi_score(self):
        Event = self.env["calendar.event"].sudo()
        for rec in self:
            if not rec.candidate_id:
                rec.pi_score = 0.0
                continue
            events = Event.search([
                ("candidate_id", "=", rec.candidate_id.id),
                ("evaluation_submitted", "=", True),
            ])
            if not events:
                rec.pi_score = 0.0
                continue
            avg = sum(e.overall_score or 0 for e in events) / len(events)
            rec.pi_score = round(avg * 10, 2)

    @api.constrains("pms_score")
    def _check_score_ranges(self):
        for rec in self:
            if rec.pms_score and not (0 <= rec.pms_score <= 100):
                raise ValidationError(_("pms_score must be between 0 and 100."))


class HrApplicantPmsEvaluation(models.Model):
    _name = "hr.applicant.pms.evaluation"
    _description = "PMS Performance Evaluation (post-hire, HRMS-clone)"
    _inherit = ["mail.thread"]
    _order = "create_date desc"
    _rec_name = "employee_id"

    candidate_id = fields.Many2one("hr.applicant", index=True)
    employee_id = fields.Many2one("hr.employee", required=True, index=True)
    evaluator_id = fields.Many2one("res.users")

    verbal_clarity = fields.Integer(tracking=True)
    conciseness = fields.Integer(tracking=True)
    fluency = fields.Integer(tracking=True)
    vocabulary = fields.Integer(tracking=True)
    pronunciation = fields.Integer(tracking=True)
    nonverbal_confidence = fields.Integer(tracking=True)
    intro_background = fields.Integer(tracking=True)
    ethara_awareness = fields.Integer(tracking=True)
    current_affairs = fields.Integer(tracking=True)
    instagram_familiarity = fields.Integer(tracking=True)
    prompt_engineering = fields.Integer(tracking=True)
    video_editing = fields.Integer(tracking=True)

    metric_remarks = fields.Text(help="JSON per-metric remarks")
    total_score = fields.Float(compute="_compute_score", store=True, digits=(4, 2))
    average_score = fields.Float(compute="_compute_score", store=True, digits=(3, 2))
    overall_rating = fields.Selection([
        ("excellent", "Excellent"),
        ("good", "Good"),
        ("average", "Average"),
        ("below_average", "Below Average"),
        ("poor", "Poor"),
    ], tracking=True)
    remarks = fields.Text(tracking=True)
    submitted_at = fields.Datetime(tracking=True)

    METRIC_FIELDS = (
        "verbal_clarity", "conciseness", "fluency", "vocabulary",
        "pronunciation", "nonverbal_confidence", "intro_background",
        "ethara_awareness", "current_affairs", "instagram_familiarity",
        "prompt_engineering", "video_editing",
    )

    @api.depends(*METRIC_FIELDS)
    def _compute_score(self):
        for rec in self:
            values = [getattr(rec, f) or 0 for f in self.METRIC_FIELDS]
            rec.total_score = sum(values)
            filled = [v for v in values if v]
            rec.average_score = round(sum(filled) / len(filled), 2) if filled else 0

    @api.constrains(*METRIC_FIELDS)
    def _check_metric_range(self):
        for rec in self:
            for f in self.METRIC_FIELDS:
                v = getattr(rec, f)
                if v and not (1 <= v <= 10):
                    raise ValidationError(_(
                        "%(name)s must be between 1 and 10.",
                        name=self._fields[f].string,
                    ))


class HrApplicantAuditLog(models.Model):
    _name = "hr.applicant.audit.log"
    _description = "Evaluation Audit Log (HRMS-clone)"
    _order = "create_date desc"
    _rec_name = "action"

    entity_type = fields.Selection([
        ("evaluation", "Evaluation"),
        ("pi_round", "PI Round"),
        ("pms_evaluation", "PMS Evaluation"),
        ("candidate", "Candidate"),
    ], required=True, index=True)
    entity_id = fields.Integer(required=True, index=True)
    action = fields.Char(required=True)
    performed_by_id = fields.Many2one("res.users")
    performed_by_name = fields.Char()
    performed_by_role = fields.Char()
    candidate_id = fields.Many2one("hr.applicant", index=True)
    old_value = fields.Text()
    new_value = fields.Text()
    ip_address = fields.Char()
    user_agent = fields.Char()
