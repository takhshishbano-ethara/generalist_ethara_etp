# -*- coding: utf-8 -*-
"""Multi-day plan: day rows + per-candidate per-day sessions.

Greenfield against brief §2.2 + §3.1 + §4. Day binds a *skill* (not a
category, unlike the legacy single-mode flow): the question pool for the
day is ``skill.question_ids`` (active subset), and skill artifacts
(question_count, time_minutes) default the day's count/duration via
onchange. Manual mode lets the admin pick a subset of the skill's
questions explicitly.

Per-candidate-per-day execution lives on :class:`EtpAssessmentDaySession`:
state machine ``locked -> available -> in_progress -> submitted/scored``
(plus ``missed`` for expired-without-submit), driven by the candidate
portal actions and the two crons in data/cron.xml.
"""
import json
import logging
import random
import uuid
from datetime import timedelta

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class EtpAssessmentDay(models.Model):
    _name = "etp.assessment.day"
    _description = "Assessment Day (plan row)"
    _order = "assessment_id, sequence, id"

    sequence = fields.Integer(default=10, required=True)
    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade", index=True)

    # skill_id is the day's binding to the question bank. NOT required at
    # the model level so that action_scaffold_days can create blank rows
    # before the admin assigns skills; enforced in action_generate_plan
    # via a manual check (brief §9.2).
    # Multi-skill: a day can draw from one or more skill banks. ``skill_ids``
    # is the source of truth; ``skill_id`` is a stored compute pinned to the
    # first skill so every existing read site (templates, related fields,
    # mail template, candidate views) keeps working with no edits.
    skill_ids = fields.Many2many(
        "etp.assessment.skill",
        relation="etp_assessment_day_skill_rel",
        column1="day_id", column2="skill_id",
        string="Skills",
        help="One or more skills whose question banks fuel this day. "
             "The question pool is the union of all selected skills' "
             "active questions.")
    skill_id = fields.Many2one(
        "etp.assessment.skill", string="Primary Skill",
        compute="_compute_skill_id", store=True, readonly=True,
        ondelete="restrict",
        help="First selected skill, used as the primary label everywhere "
             "a single skill is displayed (kanban, list, emails). Derived "
             "from skill_ids[:1].")
    category_id = fields.Many2one(
        "etp.assessment.category", string="Secondary Category Filter",
        ondelete="restrict",
        help="Optional secondary filter. Skill is the primary binding; "
             "category here is informational unless a future flow re-uses it.")

    question_count = fields.Integer(string="Question Count", default=0)
    available_question_count = fields.Integer(
        compute="_compute_available_question_count",
        string="Available in Bank")
    duration_minutes = fields.Integer(string="Duration (Minutes)", default=0)
    scheduled_start = fields.Datetime(string="Scheduled Start")

    question_source = fields.Selection(
        [("skill_pool", "Skill Pool (auto-shuffle)"),
         ("manual", "Manual Pick")],
        default="skill_pool",
        required=True,
        string="Question Source",
    )
    question_ids = fields.Many2many(
        "etp.assessment.question",
        relation="etp_assessment_day_question_rel",
        column1="day_id", column2="question_id",
        string="Manual Questions",
        help="Used only when question_source='manual'. Each question must "
             "belong to the day's skill (validated in _check_manual_questions).")

    name = fields.Char(compute="_compute_name", store=True)
    day_session_ids = fields.One2many(
        "etp.assessment.day.session", "day_id", string="Day Sessions")

    @api.depends("skill_ids")
    def _compute_skill_id(self):
        for rec in self:
            rec.skill_id = rec.skill_ids[:1]

    @api.depends("sequence", "skill_ids", "skill_ids.name")
    def _compute_name(self):
        for rec in self:
            skills = rec.skill_ids
            if not skills:
                label = "Unassigned Skill"
            elif len(skills) == 1:
                label = skills.name
            else:
                label = f"{skills[0].name} +{len(skills) - 1}"
            rec.name = f"Day {rec.sequence}: {label}"

    @api.depends("skill_ids", "skill_ids.question_ids", "skill_ids.question_ids.active")
    def _compute_available_question_count(self):
        for rec in self:
            pool = rec.skill_ids.question_ids.filtered(lambda q: q.active)
            rec.available_question_count = len(pool)

    @api.onchange("skill_ids")
    def _onchange_skill_ids(self):
        # Auto-default count + duration from the FIRST skill the first time
        # one is picked; admin can still override either field.
        first = self.skill_ids[:1]
        if first:
            if not self.question_count:
                self.question_count = first.question_count or 0
            if not self.duration_minutes:
                self.duration_minutes = first.time_minutes or 0

    @staticmethod
    def _floor_to_midnight(dt):
        # A day "opens" at the START of its scheduled calendar day, not at an
        # arbitrary time. Snap any scheduled_start down to 00:00:00 so the
        # day becomes available at midnight of that date.
        if not dt:
            return dt
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("scheduled_start"):
                dt = fields.Datetime.to_datetime(vals["scheduled_start"])
                vals["scheduled_start"] = self._floor_to_midnight(dt)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("scheduled_start"):
            dt = fields.Datetime.to_datetime(vals["scheduled_start"])
            vals["scheduled_start"] = self._floor_to_midnight(dt)
        return super().write(vals)

    @api.constrains("question_source", "skill_ids", "question_ids")
    def _check_manual_questions(self):
        for rec in self:
            if rec.question_source != "manual":
                continue
            if not rec.skill_ids:
                continue
            allowed_qids = set(rec.skill_ids.question_ids.ids)
            bad = [q.id for q in rec.question_ids if q.id not in allowed_qids]
            if bad:
                names = ", ".join(rec.skill_ids.mapped("name"))
                raise ValidationError(
                    f"Manual questions {bad} are not part of the selected "
                    f"skills ({names}). Either change the skill set or only "
                    f"pick from those banks.")

    def _resolve_question_ids(self):
        """Return a shuffled list of question.ids for one candidate's session.

        Skill-pool mode: union of ``skill_ids[].question_ids`` filtered to
        active, deduplicated, shuffled, capped at ``question_count``
        (0 = unlimited).
        Manual mode: uses the admin's hand-picked ``question_ids``.
        """
        self.ensure_one()
        if self.question_source == "manual":
            pool = self.question_ids.ids
        else:
            pool = self.skill_ids.question_ids.filtered(lambda q: q.active).ids
        if not pool:
            return []
        order = list(dict.fromkeys(pool))
        random.shuffle(order)
        if self.question_count and len(order) > self.question_count:
            order = order[:self.question_count]
        return order


class EtpAssessmentDaySession(models.Model):
    _name = "etp.assessment.day.session"
    _description = "Candidate × Day Session"
    _order = "assessment_id, day_id, id"
    _rec_name = "display_name"

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade", index=True)
    day_id = fields.Many2one(
        "etp.assessment.day", required=True, ondelete="cascade", index=True)
    evaluator_id = fields.Many2one(
        "etp.assessment.evaluator", required=True, ondelete="cascade",
        index=True)

    employee_id = fields.Many2one(
        related="evaluator_id.employee_id", store=True, readonly=True)
    skill_id = fields.Many2one(
        related="day_id.skill_id", store=True, readonly=True)
    day_sequence = fields.Integer(
        related="day_id.sequence", store=True, readonly=True)
    scheduled_start = fields.Datetime(
        related="day_id.scheduled_start", store=True, readonly=True)

    access_token = fields.Char(
        string="Access Token", index=True, copy=False,
        default=lambda self: str(uuid.uuid4()))
    question_order = fields.Text(string="Shuffled Question Order (JSON)")
    total_questions = fields.Integer()
    answered_count = fields.Integer(
        compute="_compute_progress", store=True, string="Answered")
    started_at = fields.Datetime(string="Started At")
    deadline_datetime = fields.Datetime(
        string="Deadline", compute="_compute_deadline_datetime", store=True)

    state = fields.Selection(
        [
            ("locked", "Locked"),
            ("available", "Available"),
            ("in_progress", "In Progress"),
            ("submitted", "Submitted"),
            ("scored", "Scored"),
            ("missed", "Missed"),
        ],
        default="locked",
        required=True,
        index=True,
    )

    response_ids = fields.One2many(
        "etp.assessment.response", "day_session_id", string="Responses")
    score = fields.Integer(
        compute="_compute_score", store=True, string="Day Score")
    max_score = fields.Integer(
        compute="_compute_score", store=True, string="Day Max")

    # ---- Candidate "My Assessments" helpers -------------------------------
    # The candidate hub groups day sessions into three buckets. Computing the
    # bucket (stored) lets the kanban/list group by it cleanly.
    duration_minutes = fields.Integer(
        related="day_id.duration_minutes", string="Duration (Minutes)")
    results_released = fields.Boolean(
        related="evaluator_id.results_released", store=True,
        string="Results Released")
    bucket = fields.Selection(
        [("available", "Available"),
         ("upcoming", "Upcoming"),
         ("past", "Past")],
        compute="_compute_bucket", store=True, string="Bucket", index=True)
    score_display = fields.Char(
        compute="_compute_score_display", string="Score")

    @api.depends("state")
    def _compute_bucket(self):
        for rec in self:
            if rec.state in ("available", "in_progress"):
                rec.bucket = "available"
            elif rec.state in ("submitted", "scored", "missed"):
                rec.bucket = "past"
            else:  # locked
                rec.bucket = "upcoming"

    @api.depends("state", "score", "max_score", "results_released")
    def _compute_score_display(self):
        for rec in self:
            if rec.state == "missed":
                rec.score_display = "Missed"
            elif rec.state not in ("submitted", "scored"):
                rec.score_display = ""
            elif not rec.results_released:
                rec.score_display = "Awaiting results"
            else:
                rec.score_display = "%s / %s" % (rec.score, rec.max_score)

    def action_open_test(self):
        """Candidate launches/resumes this day's test in the proctored
        runner. Returns the tokenized portal URL (full-screen, chrome-free)."""
        self.ensure_one()
        if self.state not in ("available", "in_progress"):
            raise UserError(
                "This day is not open. Available days can be started; "
                "locked days open on schedule or after the previous day.")
        return {
            "type": "ir.actions.act_url",
            "url": "/assessment/day/%s" % self.access_token,
            "target": "self",
        }

    display_name = fields.Char(
        compute="_compute_display_name", store=True)

    @api.depends("employee_id", "day_id.sequence", "skill_id.name")
    def _compute_display_name(self):
        for rec in self:
            emp = rec.employee_id.name or "?"
            sk = rec.skill_id.name or "?"
            rec.display_name = f"{emp} — Day {rec.day_id.sequence} ({sk})"

    @api.depends("response_ids.state")
    def _compute_progress(self):
        for rec in self:
            rec.answered_count = len(
                rec.response_ids.filtered(lambda r: r.state == "submitted"))

    @api.depends("response_ids.state", "response_ids.score",
                 "response_ids.max_score", "response_ids.llm_score",
                 "response_ids.llm_max_score")
    def _compute_score(self):
        for rec in self:
            submitted = rec.response_ids.filtered(
                lambda r: r.state == "submitted")
            rec.score = (sum(submitted.mapped("score"))
                         + sum(submitted.mapped("llm_score")))
            rec.max_score = (sum(submitted.mapped("max_score"))
                             + sum(submitted.mapped("llm_max_score")))

    @api.depends("started_at", "day_id.duration_minutes")
    def _compute_deadline_datetime(self):
        for rec in self:
            if rec.started_at and rec.day_id.duration_minutes > 0:
                rec.deadline_datetime = rec.started_at + timedelta(
                    minutes=rec.day_id.duration_minutes)
            else:
                rec.deadline_datetime = False

    def _send_day_invitation(self):
        tpl = self.env.ref(
            "etp_assessment.mail_template_day_invitation",
            raise_if_not_found=False)
        if not tpl:
            return
        for rec in self:
            emp = rec.evaluator_id.employee_id
            to_email = (emp.work_email or
                        (emp.user_id.email if emp.user_id else "") or "")
            if not to_email:
                _logger.warning(
                    "No email for candidate %r on assessment %r — "
                    "saving mail.mail as cancelled with portal link.",
                    emp.name, rec.assessment_id.name)
                base = self.env["ir.config_parameter"].sudo().get_param(
                    "web.base.url") or "http://localhost:8069"
                link = f"{base}/assessment/day/{rec.access_token}"
                self.env["mail.mail"].sudo().create({
                    "subject": f"[NO EMAIL] {rec.assessment_id.name} Day {rec.day_id.sequence}",
                    "body_html": tpl._render_field("body_html", rec.ids)[rec.id],
                    "email_to": "",
                    "state": "cancel",
                    "failure_reason": f"No email on candidate {emp.name!r}. "
                                      f"Portal link: {link}",
                    "auto_delete": False,
                })
                continue
            tpl.send_mail(rec.id, force_send=False)

    def action_start_day(self):
        for rec in self:
            if rec.state not in ("available",):
                raise UserError(
                    f"Day session is in state '{rec.state}', cannot start. "
                    f"It must be 'available'.")
            rec.write({
                "state": "in_progress",
                "started_at": fields.Datetime.now(),
            })

    def action_submit_day(self):
        for rec in self:
            if rec.state not in ("in_progress",):
                raise UserError(
                    f"Day session is in state '{rec.state}', cannot submit.")
            rec._ensure_unanswered_placeholders()
            rec.write({"state": "submitted"})
            rec._rollup_day_score()
            rec._unlock_next_day()
            rec.evaluator_id._rollup_overall_from_days()

    def _ensure_unanswered_placeholders(self):
        # Zero-score submitted rows for any unanswered question so the day's
        # max_score denominator reflects ALL assigned questions, not just the
        # ones answered (else answering 3/10 perfectly reads as 100%).
        self.ensure_one()
        question_order = json.loads(self.question_order or "[]")
        Response = self.env["etp.assessment.response"]
        for q_id in question_order:
            existing = Response.search([
                ("day_session_id", "=", self.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue
            draft = Response.search([
                ("day_session_id", "=", self.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted",
                             "llm_state": "not_needed"})
                continue
            Response.create({
                "assessment_id": self.assessment_id.id,
                "assessment_evaluator_id": self.evaluator_id.id,
                "day_session_id": self.id,
                "evaluator_id": self.evaluator_id.employee_id.id,
                "question_id": q_id,
                "justification": "[Auto-submitted: not answered]",
                "state": "submitted",
                "llm_state": "not_needed",
            })

    def _rollup_day_score(self):
        # Flip state to ``scored`` only when every subjective response is
        # either non-applicable or already LLM-scored; otherwise stay in
        # ``submitted`` so the cron drainer (or admin button) finishes it.
        for rec in self:
            pending_subj = rec.response_ids.filtered(
                lambda r: r.needs_llm
                and r.llm_state in ("pending", "queued", "failed"))
            if not pending_subj:
                rec.write({"state": "scored"})

    def _unlock_next_day(self):
        # Sequential mode: unlock day N+1 for THIS evaluator once day N is
        # submitted — BUT only if day N+1's scheduled_start has already
        # arrived (or it has none). A day scheduled for a future date stays
        # LOCKED until that date; the open-scheduled cron flips it at midnight.
        # Parallel mode: all days were already available — nothing to do.
        for rec in self:
            assessment = rec.assessment_id
            if not assessment.sequential_days:
                continue
            next_day = self.env["etp.assessment.day"].search([
                ("assessment_id", "=", assessment.id),
                ("sequence", ">", rec.day_id.sequence),
            ], order="sequence", limit=1)
            if not next_day:
                continue
            next_session = self.search([
                ("evaluator_id", "=", rec.evaluator_id.id),
                ("day_id", "=", next_day.id),
            ], limit=1)
            if not next_session or next_session.state != "locked":
                continue
            # Honor a future scheduled_start — leave the day LOCKED for the
            # open-scheduled cron to pick up at the right date. Completing the
            # prior day must never open a day before its scheduled start.
            now = fields.Datetime.now()
            if next_day.scheduled_start and next_day.scheduled_start > now:
                continue
            next_session.write({"state": "available"})

    @api.model
    def _cron_open_scheduled_days(self):
        """Flip ``locked`` to ``available`` when the day's scheduled_start
        has arrived AND (sequential: the prior day is finished, OR
        parallel: always)."""
        now = fields.Datetime.now()
        locked = self.search([("state", "=", "locked")])
        for sess in locked:
            day = sess.day_id
            assessment = sess.assessment_id
            if day.scheduled_start and day.scheduled_start > now:
                continue
            first_day = self.env["etp.assessment.day"].search(
                [("assessment_id", "=", day.assessment_id.id)],
                order="sequence", limit=1)
            is_first = (day.id == first_day.id)
            if assessment.sequential_days and not is_first:
                prior_day = self.env["etp.assessment.day"].search([
                    ("assessment_id", "=", assessment.id),
                    ("sequence", "<", day.sequence),
                ], order="sequence desc", limit=1)
                if prior_day:
                    prior_session = self.search([
                        ("evaluator_id", "=", sess.evaluator_id.id),
                        ("day_id", "=", prior_day.id),
                    ], limit=1)
                    if prior_session and prior_session.state not in (
                            "submitted", "scored", "missed"):
                        continue
            sess.write({"state": "available"})

    @api.model
    def _cron_mark_missed(self):
        """Auto-submit an in_progress session whose deadline has passed.

        The portal already auto-submits on expiry, but this cron rescues
        candidates who simply closed the tab — their session would
        otherwise sit in_progress forever.
        """
        now = fields.Datetime.now()
        stale = self.search([
            ("state", "=", "in_progress"),
            ("deadline_datetime", "!=", False),
            ("deadline_datetime", "<", now),
        ])
        for sess in stale:
            try:
                sess.action_submit_day()
            except Exception:
                _logger.exception(
                    "_cron_mark_missed: auto-submit failed for session %s",
                    sess.id)
                sess.write({"state": "missed"})


class EtpAssessmentMultiDayActions(models.Model):
    """Re-open etp.assessment to contribute the multi-day actions.

    Keeps the day-aware code colocated with the day models. The actions
    declared here are referenced as buttons in views/assessment_views.xml.
    """
    _inherit = "etp.assessment"

    def action_scaffold_days(self):
        # Reconciles ``day_ids`` to exactly ``num_days`` rows:
        #   1. Drop extras whose sequence > num_days (only if they have no
        #      generated day_sessions — safe to delete pre-plan rows).
        #   2. Create the missing sequence numbers up to num_days.
        # Existing day rows with sequence ≤ num_days are kept so re-running
        # won't blow away admin-tuned skills/count/duration.
        created_total = 0
        dropped_total = 0
        for rec in self:
            if rec.num_days <= 0:
                raise UserError(
                    "Set 'Number of Days' to a positive value before "
                    "scaffolding.")
            extras = rec.day_ids.filtered(lambda d: d.sequence > rec.num_days)
            blocking = extras.filtered(lambda d: d.day_session_ids)
            if blocking:
                raise UserError(
                    "Cannot drop day(s) "
                    f"{', '.join(str(d.sequence) for d in blocking)}: "
                    "candidate sessions already exist for them. "
                    "Increase 'Number of Days' or cancel the assessment "
                    "and start over.")
            if extras:
                dropped_total += len(extras)
                extras.unlink()
            existing_seqs = set(rec.day_ids.mapped("sequence"))
            Day = self.env["etp.assessment.day"]
            # Each day opens at 00:00 of a consecutive calendar date, counting
            # from the assessment's Start Date (or today if unset). Day 1 =
            # start date midnight, Day 2 = next day midnight, etc. Admin can
            # still edit any day's scheduled_start afterwards.
            base = rec.start_date or fields.Datetime.now()
            base = fields.Datetime.to_datetime(base).replace(
                hour=0, minute=0, second=0, microsecond=0)
            new_days = []
            for seq in range(1, rec.num_days + 1):
                if seq in existing_seqs:
                    continue
                offset = seq - 1 if rec.sequential_days else 0
                new_days.append({
                    "assessment_id": rec.id,
                    "sequence": seq,
                    "scheduled_start": base + timedelta(days=offset),
                })
            if new_days:
                Day.create(new_days)
                created_total += len(new_days)
        parts = []
        if created_total:
            parts.append(f"{created_total} day(s) created")
        if dropped_total:
            parts.append(f"{dropped_total} extra day(s) removed")
        message = " · ".join(parts) if parts else "Plan already matches the requested number of days."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Days Scaffolded",
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def action_generate_plan(self):
        # See brief §4 — exact algorithm. Idempotent: skip evaluator×day
        # rows that already exist so re-runs don't double-create sessions.
        DaySession = self.env["etp.assessment.day.session"]
        now = fields.Datetime.now()
        notif_lines = []
        for rec in self:
            days = rec.day_ids.sorted("sequence")
            if not days:
                raise UserError(
                    "No days defined. Use Scaffold Days first.")
            missing_skill = [d.sequence for d in days if not d.skill_ids]
            if missing_skill:
                raise UserError(
                    "Days missing a skill: "
                    f"{', '.join(str(s) for s in missing_skill)}. "
                    "Assign at least one before generating the plan.")
            empty_skill = [
                f"Day {d.sequence} ({', '.join(d.skill_ids.mapped('name'))})"
                for d in days
                if d.question_source == "skill_pool"
                and not d.skill_ids.question_ids.filtered("active")
            ]
            if empty_skill:
                raise UserError(
                    "These days draw from skill(s) with no active questions: "
                    f"{', '.join(empty_skill)}. Generate or import questions "
                    "for the skill(s) first, or switch the day to manual mode "
                    "and pick questions directly.")
            if not rec.evaluator_ids:
                raise UserError(
                    "Assign at least one candidate before generating the plan.")
            first_seq = days[0].sequence
            sessions_created = 0
            for emp in rec.evaluator_ids:
                evaluator = self.env["etp.assessment.evaluator"].search([
                    ("assessment_id", "=", rec.id),
                    ("employee_id", "=", emp.id),
                ], limit=1)
                if not evaluator:
                    evaluator = self.env["etp.assessment.evaluator"].create({
                        "assessment_id": rec.id,
                        "employee_id": emp.id,
                        "access_token": str(uuid.uuid4()),
                    })
                existing_day_ids = set(evaluator.day_session_ids.mapped("day_id").ids)
                for day in days:
                    if day.id in existing_day_ids:
                        continue
                    order = day._resolve_question_ids()
                    # Default state: first day is available, others locked
                    # in sequential mode; parallel = everything available.
                    if not rec.sequential_days or day.sequence == first_seq:
                        state = "available"
                    else:
                        state = "locked"
                    # Future-scheduled days override to locked even on
                    # day 1 (the open-scheduled cron will flip them).
                    if day.scheduled_start and day.scheduled_start > now:
                        state = "locked"
                    new_sess = DaySession.create({
                        "assessment_id": rec.id,
                        "day_id": day.id,
                        "evaluator_id": evaluator.id,
                        "access_token": str(uuid.uuid4()),
                        "question_order": json.dumps(order),
                        "total_questions": len(order),
                        "state": state,
                    })
                    sessions_created += 1
                    new_sess._send_day_invitation()
            if rec.state == "draft":
                rec.write({
                    "state": "in_progress",
                    "start_date": rec.start_date or fields.Datetime.now(),
                })
            notif_lines.append(
                f"{rec.name}: {sessions_created} new session(s) across "
                f"{len(days)} day(s) for {len(rec.evaluator_ids)} candidate(s)")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Plan Generated",
                "message": " | ".join(notif_lines) or "No changes.",
                "type": "success",
                "sticky": False,
            },
        }
