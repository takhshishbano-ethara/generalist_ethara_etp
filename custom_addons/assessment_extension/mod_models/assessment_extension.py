import math

from odoo import _, api, fields, models


_PROMPT_PEN_TYPES = ("eval_compare", "prompt_writing", "bbox_labeling")


class ModAssessmentExtension(models.Model):
    _inherit = "etp.assessment"

    questions_locked = fields.Boolean(
        string="Questions Locked",
        default=False,
        copy=False,
        help=(
            "When True, the question bank for this assessment is frozen "
            "(MOD-Lock-Confirm). Review actions are disabled."
        ),
    )
    questions_locked_at = fields.Datetime(
        string="Locked At",
        readonly=True,
        copy=False,
    )
    questions_locked_by_id = fields.Many2one(
        "res.users",
        string="Locked By",
        readonly=True,
        copy=False,
    )
    daily_start_time = fields.Float(
        string="Daily Start (h)",
        default=0.0,
        help="Hour-of-day (0-24) the portal opens each day.",
    )
    daily_end_time = fields.Float(
        string="Daily End (h)",
        default=0.0,
        help="Hour-of-day (0-24) the portal closes each day.",
    )
    question_review_ids = fields.One2many(
        "etp.assessment.question.review",
        "assessment_id",
        string="Question Reviews",
    )
    pending_review_count = fields.Integer(
        string="Pending Reviews",
        compute="_compute_pending_review_count",
        store=False,
    )

    @api.depends(
        "question_ids",
        "question_review_ids",
        "question_review_ids.review_state",
    )
    def _compute_pending_review_count(self):
        for rec in self:
            approved = sum(
                1 for r in rec.question_review_ids
                if r.review_state == "approved"
            )
            total = len(rec.question_ids)
            rec.pending_review_count = max(0, total - approved)

    def _sync_question_reviews(self):
        """Idempotently create a review row for every linked question.

        Day/sequence assignment uses linear slot allocation over the
        assessment's day_span (start/end date delta + 1; defaults to 1).
        The SQL UNIQUE on (assessment_id, day_number, day_sequence)
        guards against races; we wrap the batch create in a savepoint
        and fall back to per-row create if the bulk write fails.
        """
        self.ensure_one()
        Review = self.env["etp.assessment.question.review"].sudo()

        existing_qids = set(self.question_review_ids.mapped("question_id.id"))
        questions = self.question_ids.sorted(
            key=lambda q: (q.sequence or 10, q.id)
        )
        missing = [q for q in questions if q.id not in existing_qids]
        if not missing:
            return

        day_span = max(1, self._compute_day_span())
        total_questions = len(questions)
        per_day = max(1, math.ceil(total_questions / day_span))

        used_slots = {
            (r.day_number or 1, r.day_sequence or 1)
            for r in self.question_review_ids
        }

        rows_to_create = []
        for q in missing:
            slot = None
            for d in range(1, day_span + 1):
                for s in range(1, per_day + 1):
                    if (d, s) not in used_slots:
                        slot = (d, s)
                        break
                if slot:
                    break
            if slot is None:
                max_day = max((s[0] for s in used_slots), default=day_span)
                slot = (max_day + 1, 1)
            used_slots.add(slot)
            rows_to_create.append({
                "assessment_id": self.id,
                "question_id": q.id,
                "review_state": "draft",
                "day_number": slot[0],
                "day_sequence": slot[1],
            })

        if not rows_to_create:
            return
        try:
            with self.env.cr.savepoint():
                Review.create(rows_to_create)
        except Exception:
            # Per-row retry so a single UNIQUE conflict cannot
            # poison the whole batch under concurrent sync.
            self.invalidate_recordset(["question_review_ids"])
            still_existing = set(
                self.question_review_ids.mapped("question_id.id")
            )
            for vals in rows_to_create:
                if vals["question_id"] in still_existing:
                    continue
                try:
                    with self.env.cr.savepoint():
                        Review.create([vals])
                except Exception:
                    continue

    def _can_lock(self):
        self.ensure_one()
        reasons = []
        if self.questions_locked:
            reasons.append({
                "code": "ALREADY_LOCKED",
                "message": "This assessment is already locked.",
            })
        if self.state != "draft":
            reasons.append({
                "code": "INVALID_STATE",
                "message": (
                    "Only draft assessments can be locked "
                    "(current state: %s)." % self.state
                ),
            })
        self._sync_question_reviews()
        pending = self.pending_review_count
        if pending > 0:
            reasons.append({
                "code": "REVIEW_PENDING",
                "message": (
                    "%d question(s) are not yet approved." % pending
                ),
                "details": {"pending_count": pending},
            })
        return (not reasons, reasons)

    def _can_send(self):
        self.ensure_one()
        reasons = []
        if self.state in ("in_progress", "done"):
            reasons.append({
                "code": "ALREADY_SENT",
                "message": "This assessment has already been sent.",
            })
        if not self.questions_locked:
            reasons.append({
                "code": "NOT_LOCKED_YET",
                "message": "Lock the assessment before scheduling.",
            })
        if not self.evaluator_ids:
            reasons.append({
                "code": "NO_CANDIDATES",
                "message": "Assign at least one candidate before sending.",
            })
        missing_fields = []
        if not self.start_date:
            missing_fields.append("start_date")
        if not self.end_date:
            missing_fields.append("end_date")
        if not self.daily_start_time:
            missing_fields.append("daily_start_time")
        if not self.daily_end_time:
            missing_fields.append("daily_end_time")
        if missing_fields:
            reasons.append({
                "code": "MISSING_SCHEDULE",
                "message": (
                    "Schedule fields missing: %s." % ", ".join(missing_fields)
                ),
                "details": {"missing": missing_fields},
            })
        if self.start_date:
            today = fields.Date.context_today(self)
            start_date_only = self.start_date.date()
            if start_date_only < today:
                reasons.append({
                    "code": "START_IN_PAST",
                    "message": "Start date is in the past.",
                    "details": {
                        "start_date": start_date_only.isoformat(),
                        "today": today.isoformat(),
                    },
                })
        return (not reasons, reasons)

    def _compute_day_span(self):
        self.ensure_one()
        if not (self.start_date and self.end_date):
            return 1
        delta = (self.end_date.date() - self.start_date.date()).days + 1
        return max(1, delta)

    def _compute_derived_state(self):
        self.ensure_one()
        if self.state == "draft":
            return "questions_locked" if self.questions_locked else "draft"
        if self.state == "in_progress":
            today = fields.Date.context_today(self)
            start = self.start_date.date() if self.start_date else None
            end = self.end_date.date() if self.end_date else None
            if start and today < start:
                return "scheduled"
            if start and end and start <= today <= end:
                return "in_progress"
            return "in_progress"
        return self.state

    def _compute_summary_parts(self):
        self.ensure_one()
        by_type = {t: 0 for t in _PROMPT_PEN_TYPES}
        for q in self.question_ids:
            if q.question_type in by_type:
                by_type[q.question_type] += 1
        return {
            "question_count": len(self.question_ids),
            "day_span": self._compute_day_span(),
            "candidate_count": len(self.evaluator_ids),
            "start_date": (
                self.start_date.date().isoformat()
                if self.start_date else None
            ),
            "end_date": (
                self.end_date.date().isoformat()
                if self.end_date else None
            ),
            "daily_start_time": self.daily_start_time or 0.0,
            "daily_end_time": self.daily_end_time or 0.0,
            "daily_start_label": _format_hhmm(self.daily_start_time),
            "daily_end_label": _format_hhmm(self.daily_end_time),
            "by_type": by_type,
        }

    def _stamp_questions_locked(self, user=None):
        self.ensure_one()
        self.write({
            "questions_locked": True,
            "questions_locked_at": fields.Datetime.now(),
            "questions_locked_by_id": (
                user.id if user else self.env.uid
            ),
        })


def _format_hhmm(value):
    if not value:
        return "00:00"
    hours = int(value)
    minutes = int(round((value - hours) * 60))
    if minutes >= 60:
        hours += 1
        minutes -= 60
    return "%02d:%02d" % (hours % 24, minutes)
