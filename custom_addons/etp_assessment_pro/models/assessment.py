# -*- coding: utf-8 -*-
"""ETP Assessment lifecycle models: assessment, evaluator, response, response line."""
import base64
import csv
import io
import json
import logging
import math
import random
import uuid

from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class EtpAssessment(models.Model):
    _name = "etp.assessment.pro"
    _description = "Assessment"
    _order = "create_date desc"

    name = fields.Char(required=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
    )

    # Mode-required fields (category_id / day skill_id) are enforced via
    # @api.constrains, not NOT NULL, so blank scaffolding doesn't trip the DB.
    assessment_mode = fields.Selection(
        [("single", "Single Test"), ("multi_day", "Multi-Day Plan")],
        default="multi_day",
        required=True,
        help="Single = one flat test from a category. Multi-day = N-day plan, "
             "one skill per day, per-candidate per-day sessions. "
             "NOTE: single mode is retired from the UI; all new assessments "
             "are multi_day (num_days=1 covers the old single-test case). "
             "The single-mode field/methods are kept dormant for data "
             "integrity on legacy records.",
    )

    # SINGLE-MODE fields. Inert in multi_day.
    category_id = fields.Many2one(
        "etp.assessment.pro.category",
        string="Question Category",
        ondelete="restrict",
        help="Required only in single mode; @api.constrains enforces it.",
    )
    question_limit = fields.Integer(
        string="Number of Questions",
        help="Number of questions to pick from the category. 0 = all questions.",
        default=0,
    )
    duration_minutes = fields.Integer(
        string="Duration (Minutes)",
        help="Time limit for candidates to complete the assessment. 0 = no limit.",
        default=0,
    )
    question_ids = fields.Many2many(
        "etp.assessment.pro.question",
        "etp_assessment_pro_question_rel",
        "assessment_id",
        "question_id",
        string="Selected Questions",
    )
    total_questions_available = fields.Integer(
        compute="_compute_total_questions_available"
    )

    # MULTI-DAY fields. day_ids/day_session_ids reference models in
    # assessment_day.py (loaded right after this file).
    num_days = fields.Integer(
        string="Number of Days", default=1,
        help="1 for a single-day test, more for a multi-day plan.")
    sequential_days = fields.Boolean(
        string="Sequential Days", default=True,
        help="Days unlock in order: submitting day N opens day N+1. "
             "Off = all days open simultaneously (subject to scheduled_start).",
    )
    day_ids = fields.One2many(
        "etp.assessment.pro.day", "assessment_id", string="Days"
    )
    day_count = fields.Integer(compute="_compute_day_count", store=True)
    day_session_ids = fields.One2many(
        "etp.assessment.pro.day.session", "assessment_id",
        string="Per-Candidate Day Sessions"
    )
    day_session_count = fields.Integer(
        compute="_compute_day_session_count", store=True
    )
    results_release = fields.Selection(
        [
            ("manual", "Manual (admin releases)"),
            ("immediate", "Immediate"),
        ],
        default="manual",
        required=True,
        string="Results Release",
        help="When candidate-facing results are revealed. Manual = admin "
             "flips results_released on each evaluator; immediate = as each "
             "day is scored.",
    )

    start_date = fields.Datetime(string="Start Date")
    end_date = fields.Datetime(string="End Date")

    # evaluator_ids = assigned list (admin toggles pre-launch);
    # assessment_evaluator_ids = materialized row per candidate.
    evaluator_ids = fields.Many2many(
        "hr.applicant",
        "etp_assessment_pro_applicant_rel",
        "assessment_id",
        "applicant_id",
        string="Candidates",
    )
    assessment_evaluator_ids = fields.One2many(
        "etp.assessment.pro.evaluator", "assessment_id",
        string="Candidate Assignments"
    )
    response_ids = fields.One2many(
        "etp.assessment.pro.response", "assessment_id", string="Responses"
    )
    response_count = fields.Integer(compute="_compute_response_count", store=True)

    # Rules & Proctoring — drive the candidate portal's anti-cheat script.
    rule_block_tab_switch = fields.Boolean(
        string="Blur on Tab/Window Switch", default=True,
        help="Blur assessment content when the candidate switches tabs.")
    rule_block_screenshot = fields.Boolean(
        string="Block Screenshots & Screen Capture", default=True)
    rule_block_copy_paste = fields.Boolean(
        string="Block Copy / Paste", default=True)
    rule_block_right_click = fields.Boolean(
        string="Block Right-Click", default=True)
    rule_block_devtools = fields.Boolean(
        string="Block Developer Tools", default=True)
    rule_watermark = fields.Boolean(
        string="Watermark Content", default=True)
    rule_fullscreen = fields.Boolean(
        string="Require Fullscreen", default=True,
        help="Force the assessment to run in fullscreen; exiting raises a "
             "violation.")
    rule_webcam = fields.Boolean(
        string="Require Webcam", default=False,
        help="Require webcam access for proctoring (placeholder switch — "
             "client-side detector wired in portal_templates.xml).")
    max_violations = fields.Integer(
        string="Max Violations (0 = no cap)", default=0,
        help="Auto-submit once a candidate exceeds this many violations. "
             "0 disables the cap; violation_action still applies per event.",
    )
    violation_action = fields.Selection(
        [("auto_submit", "Auto-submit assessment"),
         ("log_only", "Log violation only")],
        string="On Violation", default="auto_submit", required=True,
    )
    require_objective_justification = fields.Boolean(
        string="Require Justification on Objective Questions", default=False,
        help="When on, MCQ/MSQ questions also show a justification box the "
             "candidate must fill. Off (default) keeps objective questions "
             "clean — no confusing optional box.",
    )
    require_justification_image_comparison = fields.Boolean(
        string="Require Justification for Image Comparison", default=False,
        help="When on, Image A/B questions show a justification box graded by "
             "the LLM and the final score blends verdict% (75%) + "
             "justification% (25%), rounded up. When off, Image A/B is scored "
             "on the verdicts alone (objective, no LLM).",
    )
    llm_auto_score = fields.Boolean(
        string="Auto-queue subjective grading on submit", default=False,
        help="When on, a candidate's submit auto-queues them for the background "
             "grader (still batched in 20s by the cron — never scored inline "
             "during the exam). Off (default): an admin must click 'Run "
             "Subjective Evaluation'.")

    candidate_csv_file = fields.Binary(string="Upload Candidates CSV")
    candidate_csv_filename = fields.Char(string="CSV Filename")

    def write(self, vals):
        res = super().write(vals)
        # Flipping the image_ab justification toggle changes how already-submitted
        # answers must score, so re-decide their state: verdict-only -> 'scored',
        # required+justified -> 'pending' (an admin re-run then grades it).
        if "require_justification_image_comparison" in vals:
            stale = self.mapped(
                "assessment_evaluator_ids.response_ids").filtered(
                lambda r: r.question_id.question_type == "image_ab"
                and r.state == "submitted")
            if stale:
                stale._enqueue_subjective_scoring()
        return res

    @api.depends("response_ids")
    def _compute_response_count(self):
        for rec in self:
            rec.response_count = len(rec.response_ids)

    @api.depends("day_ids")
    def _compute_day_count(self):
        for rec in self:
            rec.day_count = len(rec.day_ids)

    @api.depends("day_session_ids")
    def _compute_day_session_count(self):
        for rec in self:
            rec.day_session_count = len(rec.day_session_ids)

    @api.depends("category_id")
    def _compute_total_questions_available(self):
        Question = self.env["etp.assessment.pro.question"]
        for rec in self:
            if rec.category_id:
                rec.total_questions_available = Question.search_count([
                    ("category_id", "=", rec.category_id.id),
                    ("active", "=", True),
                ])
            else:
                rec.total_questions_available = 0

    @api.constrains("question_limit")
    def _check_question_limit(self):
        for rec in self:
            if rec.question_limit < 0:
                raise ValidationError("Number of Questions cannot be negative.")

    @api.constrains("start_date", "end_date")
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.start_date >= rec.end_date:
                raise ValidationError("End Date must be after Start Date.")

    @api.constrains("assessment_mode", "category_id")
    def _check_single_requires_category(self):
        # Mode-specific NOT NULL surrogate: column stays nullable for multi_day.
        for rec in self:
            if rec.assessment_mode == "single" and not rec.category_id:
                raise ValidationError(
                    "Single-mode assessments require a question category."
                )

    # SINGLE-MODE lifecycle.
    def action_start(self):
        for rec in self:
            if rec.assessment_mode != "single":
                raise UserError(
                    "Single-mode action. Use 'Generate Plan' for multi-day "
                    "assessments."
                )
            if rec.state != "draft":
                raise UserError("Only draft assessments can be started.")
            if not rec.evaluator_ids:
                raise UserError("Please assign at least one candidate before starting.")
            if not rec.category_id:
                raise UserError("Please select a question category.")

            available_questions = self.env["etp.assessment.pro.question"].search([
                ("category_id", "=", rec.category_id.id), ("active", "=", True),
            ])
            if not available_questions:
                raise UserError(
                    "No active questions found in the selected category.")

            limit = rec.question_limit or len(available_questions)
            if limit > len(available_questions):
                raise UserError(
                    f"Requested {limit} questions but only "
                    f"{len(available_questions)} available in this category.")

            all_question_ids = available_questions.ids
            rec.write({
                "question_ids": [(6, 0, all_question_ids)],
                "state": "in_progress",
                "start_date": rec.start_date or fields.Datetime.now(),
            })

            for evaluator in rec.evaluator_ids:
                candidate_questions = random.sample(all_question_ids, limit)
                random.shuffle(candidate_questions)
                ev = self.env["etp.assessment.pro.evaluator"].create({
                    "assessment_id": rec.id,
                    "applicant_id": evaluator.id,
                    "question_order": json.dumps(candidate_questions),
                    "access_token": str(uuid.uuid4()),
                })
                ev._send_single_invitation()

    def action_done(self):
        for rec in self:
            if rec.state != "in_progress":
                raise UserError("Only in-progress assessments can be marked done.")
        self.write({"state": "done"})

    def action_cancel(self):
        for rec in self:
            if rec.state in ("done", "cancelled"):
                raise UserError(
                    "Cannot cancel a completed or already cancelled assessment.")
        self.write({"state": "cancelled"})

    def action_reset_draft(self):
        for rec in self:
            if rec.state != "cancelled":
                raise UserError("Only cancelled assessments can be reset to draft.")
            rec.assessment_evaluator_ids.unlink()
            rec.response_ids.unlink()
            rec.write({"question_ids": [(5, 0, 0)]})
        self.write({"state": "draft"})

    @api.constrains("start_date", "end_date")
    def _check_schedule_dates(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.state != "draft":
                continue
            if rec.start_date and rec.start_date.date() < today:
                raise ValidationError(
                    "Start Date cannot be in the past.")
            if rec.start_date and rec.end_date and rec.end_date < rec.start_date:
                raise ValidationError(
                    "End Date must be on or after the Start Date.")

    def _ensure_candidate_user(self, applicant):
        # Bind candidate to a PORTAL login so the exam guard recognizes them.
        # Idempotent. Returns 'exists' | 'linked' | 'created' | 'skipped'.
        applicant = applicant.sudo()
        if applicant.candidate_user_id:
            return "exists"
        email = (applicant.email_from or "").strip()
        if not email:
            return "skipped"
        Users = self.env["res.users"].sudo()
        user = Users.with_context(active_test=False).search(
            [("login", "=", email)], limit=1)
        status = "linked"
        if not user:
            company = (
                self.env.ref("base.main_company", raise_if_not_found=False)
                or self.env["res.company"].search([], limit=1, order="id asc")
            )
            portal = self.env.ref("base.group_portal")
            user_vals = {
                "name": applicant.partner_name or email,
                "login": email,
                "email": email,
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
                "group_ids": [(6, 0, [portal.id])],
            }
            user = Users.with_company(company).with_context(
                no_reset_password=True,
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
            ).create(user_vals)
            status = "created"
            try:
                user.action_reset_password()
            except Exception:
                _logger.exception(
                    "Portal signup invite failed for candidate %s (%s)",
                    applicant.partner_name, email)
        if not user.active:
            user.active = True
        applicant.candidate_user_id = user.id
        if not applicant.partner_id and user.partner_id:
            applicant.partner_id = user.partner_id.id
        return status

    def action_import_candidates_csv(self):
        self.ensure_one()
        if not self.candidate_csv_file:
            raise UserError("Please upload a CSV file first.")
        try:
            csv_data = base64.b64decode(self.candidate_csv_file)
            file_input = io.StringIO(csv_data.decode("utf-8"))
            reader = csv.DictReader(file_input)
        except Exception:
            raise UserError(
                "Invalid CSV file. Please upload a valid UTF-8 CSV file.")

        required_fields = {"name", "email"}
        if not required_fields.issubset(set(reader.fieldnames or [])):
            raise UserError(
                "CSV must contain 'name' and 'email' columns. "
                f"Found columns: {', '.join(reader.fieldnames or [])}")

        Applicant = self.env["hr.applicant"].sudo()
        imported_ids = []
        errors = []

        for row_num, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip()
            if not name or not email:
                errors.append(f"Row {row_num}: name and email are required.")
                continue
            applicant = Applicant.search(
                [("email_from", "=ilike", email)], limit=1)
            if not applicant:
                applicant = Applicant.create({
                    "partner_name": name,
                    "email_from": email,
                })
            elif not applicant.partner_name:
                applicant.partner_name = name
            imported_ids.append(applicant.id)

        if errors:
            raise UserError(
                "CSV import completed with errors:\n" + "\n".join(errors))

        existing_ids = self.evaluator_ids.ids
        new_ids = list(set(imported_ids) - set(existing_ids))
        if new_ids:
            self.write({"evaluator_ids": [(4, aid) for aid in new_ids]})
        self.write({
            "candidate_csv_file": False,
            "candidate_csv_filename": False,
        })

        provisioned = {"created": 0, "linked": 0, "exists": 0, "skipped": 0}
        for applicant in Applicant.browse(imported_ids):
            try:
                provisioned[self._ensure_candidate_user(applicant)] += 1
            except Exception:
                _logger.exception(
                    "Portal provisioning failed for candidate %s",
                    applicant.partner_name)
                provisioned["skipped"] += 1

        message = (f"{len(new_ids)} new candidate(s) added. "
                   f"{len(imported_ids) - len(new_ids)} already assigned.\n"
                   f"Portal access: {provisioned['created']} invited, "
                   f"{provisioned['linked']} linked, "
                   f"{provisioned['exists']} already had one.")
        if provisioned["skipped"]:
            message += (f"\n\u26a0 {provisioned['skipped']} candidate(s) had no "
                        f"email \u2014 no portal access created for them.")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Candidates Imported",
                "message": message,
                "type": "warning" if provisioned["skipped"] else "success",
                "sticky": bool(provisioned["skipped"]),
            },
        }

    def action_download_candidate_template(self):
        self.ensure_one()
        csv_content = "name,email\n"
        csv_content += "John Doe,john.doe@gmail.com\n"
        csv_content += "Jane Smith,jane.smith@gmail.com\n"
        csv_bytes = base64.b64encode(csv_content.encode("utf-8"))
        attachment = self.env["ir.attachment"].create({
            "name": "candidate_import_template.csv",
            "type": "binary",
            "datas": csv_bytes,
            "mimetype": "text/csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "new",
        }

    # LLM scoring — one inline Vertex call per candidate (no broker, per D3);
    # implementation in services/scoring.py.
    def action_llm_score_all(self):
        """Admin trigger: queue every submitted, unscored candidate for
        subjective grading. The background cron drains them in batches of 20 —
        no LLM call runs in this request, so the admin UI never blocks."""
        self.ensure_one()
        # Filter on the ACTUAL responses (robust to a stale evaluator llm_state):
        # any submitted candidate with a needs_llm answer not yet scored.
        todo = self.assessment_evaluator_ids.filtered(
            lambda ev: ev.state == "submitted" and any(
                r.needs_llm and r.llm_state != "scored" for r in ev.response_ids))
        if not todo:
            raise UserError("No submitted candidates awaiting subjective scoring.")
        # Force pending so the cron drains them — covers evaluators stuck at a
        # stale state (e.g. image_ab answered with verdicts only, submitted
        # before verdict-only grading existed). score_evaluator picks up the
        # not_needed/pending responses.
        todo.write({"scoring_requested": True, "llm_state": "pending"})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Subjective Grading Queued",
                "message": f"Queued {len(todo)} candidate(s). The background "
                           f"grader processes about 20 at a time — refresh to "
                           f"watch the LLM status advance.",
                "type": "success",
                "sticky": False,
            },
        }

    def action_release_all_results(self):
        self.ensure_one()
        to_release = self.assessment_evaluator_ids.filtered(
            lambda r: not r.results_released)
        if not to_release:
            raise UserError("All candidates' results are already released.")
        to_release.write({"results_released": True})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Results Released",
                "message": "Released results for %s candidate(s)." % len(to_release),
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _cron_llm_auto_score(self):
        """Background grader: drains admin-requested subjective scoring in
        batches of 20 per tick. Only candidates explicitly flagged via 'Run
        Subjective Evaluation' (scoring_requested) are graded — nothing runs on
        the candidate's exam path."""
        # Advisory lock so two cron workers don't double-score (auto-releases at commit).
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (827193,))
        if not self.env.cr.fetchone()[0]:
            return
        evaluators = self.env["etp.assessment.pro.evaluator"].search([
            ("state", "=", "submitted"),
            ("scoring_requested", "=", True),
            ("llm_state", "in", ("pending", "scoring", "partial", "failed")),
        ], limit=20)
        for ev in evaluators:
            try:
                ev.action_llm_score()
            except Exception:
                _logger.exception(
                    "Auto-score failed for evaluator %s", ev.id)
                continue

    def action_export_results(self):
        """Export the scorecard: one row per candidate."""
        self.ensure_one()
        from ..services import export as export_svc
        return export_svc.export_results(self)

    def action_export_responses(self):
        """Export every response in this assessment, one row per answer."""
        self.ensure_one()
        from ..services import export as export_svc
        return export_svc.export_responses(self)

    def _check_plan_complete(self):
        # Flip assessment to done once every assigned candidate is submitted.
        for rec in self:
            evs = rec.assessment_evaluator_ids
            if evs and all(e.state == "submitted" for e in evs):
                if rec.state == "in_progress":
                    rec.write({"state": "done"})


class EtpAssessmentEvaluator(models.Model):
    _name = "etp.assessment.pro.evaluator"
    _description = "Assessment Candidate Assignment"
    _order = "create_date desc"
    _rec_name = "applicant_id"

    assessment_id = fields.Many2one(
        "etp.assessment.pro", required=True, ondelete="cascade")
    applicant_id = fields.Many2one(
        "hr.applicant", string="Candidate", required=True, ondelete="restrict")

    def _candidate_user(self):
        # Resolve portal login: direct applicant link, else partner-matched user.
        self.ensure_one()
        user = self.applicant_id.candidate_user_id
        if not user and self.applicant_id.partner_id:
            user = self.env["res.users"].sudo().search(
                [("partner_id", "=", self.applicant_id.partner_id.id)], limit=1)
        return user
    access_token = fields.Char(
        string="Access Token", index=True, copy=False,
        default=lambda self: str(uuid.uuid4()))
    question_order = fields.Text(string="Shuffled Question Order (JSON)")
    started_at = fields.Datetime(string="Started At")
    deadline_datetime = fields.Datetime(
        string="Candidate Deadline", compute="_compute_deadline_datetime",
        store=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("in_progress", "In Progress"),
            ("submitted", "Submitted"),
        ],
        default="pending",
        required=True,
    )
    total_questions = fields.Integer(
        compute="_compute_total_questions", store=True,
        string="Total Questions")
    answered_count = fields.Integer(
        compute="_compute_progress", store=True, string="Answered")
    total_score = fields.Integer(
        compute="_compute_progress", store=True, string="Total Score")
    max_possible_score = fields.Integer(
        compute="_compute_progress", store=True, string="Max Possible Score")
    response_ids = fields.One2many(
        "etp.assessment.pro.response", "assessment_evaluator_id",
        string="Responses")
    day_session_ids = fields.One2many(
        "etp.assessment.pro.day.session", "evaluator_id",
        string="Day Sessions")

    is_locked = fields.Boolean(default=False, string="Locked")
    is_violated = fields.Boolean(default=False, string="Violated", readonly=True)
    violation_reason = fields.Char(string="Violation Reason", readonly=True)
    violation_datetime = fields.Datetime(string="Violation Time", readonly=True)
    violation_count = fields.Integer(
        string="Violations", default=0, readonly=True,
        help="Cumulative count of detected violations for this candidate. "
             "Compared against assessment.max_violations to decide auto-submit.",
    )

    # llm_state drives the cron drainer + UI badge.
    llm_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("scoring", "Scoring"),
            ("scored", "Scored"),
            ("partial", "Partial"),
            ("failed", "Failed"),
        ],
        default="pending",
        string="Subjective Scoring",
        copy=False,
    )
    objective_total = fields.Integer(
        related="total_score", store=True, string="Objective Total", readonly=True)
    objective_max_total = fields.Integer(
        related="max_possible_score", store=True,
        string="Objective Max", readonly=True)
    llm_total_score = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Total")
    llm_max_score = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Max")
    subjective_pending = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Pending")
    llm_scored_at = fields.Datetime(string="Subjective Scored At", readonly=True)
    llm_error = fields.Char(string="Subjective Error", readonly=True)

    score_percent = fields.Float(
        string="Score %", compute="_compute_result", store=True)
    pass_threshold = fields.Float(
        string="Pass Threshold %", compute="_compute_result", store=True)
    result = fields.Selection(
        [("pending", "Pending"), ("pass", "Pass"), ("fail", "Fail")],
        string="Result", compute="_compute_result", store=True, default="pending")
    results_released = fields.Boolean(
        string="Results Released", default=False,
        help="Gates candidate-facing results based on assessment.results_release. "
             "Manual = admin flips; immediate = on submit.",
    )
    scoring_requested = fields.Boolean(
        string="Subjective Grading Requested", default=False, copy=False,
        help="Set by 'Run Subjective Evaluation'. The background grader drains "
             "requested candidates in batches of 20; subjective scoring never "
             "runs during the exam.",
    )

    # Admin-facing post-scoring summary card, derived from the same stored
    # response fields as scoring so it can't disagree with the breakdown below.
    result_summary = fields.Html(
        string="Result Summary", compute="_compute_result_summary",
        sanitize=False,
        help="Post-scoring overview for this candidate: objective and "
             "subjective tallies, total, percentage, and pass/fail.")

    # Day progress label — drives the My Assessments badge.
    days_total = fields.Integer(
        compute="_compute_day_progress", store=True)
    days_done = fields.Integer(
        compute="_compute_day_progress", store=True)
    day_progress_label = fields.Char(
        compute="_compute_day_progress", store=True, string="Day Progress")

    @api.depends("day_session_ids", "day_session_ids.state")
    def _compute_day_progress(self):
        for rec in self:
            sessions = rec.day_session_ids
            rec.days_total = len(sessions)
            rec.days_done = len(sessions.filtered(
                lambda s: s.state in ("submitted", "scored", "missed")))
            rec.day_progress_label = (
                f"{rec.days_done} / {rec.days_total}"
                if rec.days_total else "—"
            )

    @api.depends(
        "state", "result", "score_percent", "pass_threshold",
        "total_score", "max_possible_score",
        "llm_total_score", "llm_max_score", "subjective_pending",
        "answered_count", "total_questions", "violation_count",
        "response_ids.state", "response_ids.has_objective",
        "response_ids.score", "response_ids.max_score",
        "response_ids.needs_llm", "response_ids.llm_state",
        "response_ids.llm_passed")
    def _compute_result_summary(self):
        """Build the admin-facing post-scoring summary card."""
        import html as _html

        def esc(v):
            return _html.escape(str(v))

        for rec in self:
            submitted = rec.response_ids.filtered(
                lambda r: r.state == "submitted")
            # Objective tally: "correct" = full marks on that question.
            obj = submitted.filtered(lambda r: r.has_objective)
            obj_total = len(obj)
            obj_correct = len(obj.filtered(
                lambda r: r.max_score and r.score >= r.max_score))
            # Subjective tally: needs_llm rows, pass = llm_passed once scored.
            subj = submitted.filtered(lambda r: r.needs_llm)
            subj_total = len(subj)
            subj_scored = subj.filtered(lambda r: r.llm_state == "scored")
            subj_pass = len(subj_scored.filtered("llm_passed"))

            obj_pts = rec.total_score or 0
            obj_max = rec.max_possible_score or 0
            sub_pts = rec.llm_total_score or 0
            sub_max = rec.llm_max_score or 0
            total_pts = obj_pts + sub_pts
            total_max = obj_max + sub_max
            pct = rec.score_percent or 0.0
            thr = rec.pass_threshold or 0.0

            if rec.state != "submitted":
                rec.result_summary = (
                    '<div class="alert alert-info mb-0" role="status">'
                    'Candidate has not submitted yet — '
                    f'{esc(rec.answered_count or 0)} of '
                    f'{esc(rec.total_questions or 0)} answered. '
                    'Summary appears once the assessment is submitted and '
                    'scored.</div>')
                continue

            # Result badge.
            if rec.result == "pass":
                badge = ('<span class="badge text-bg-success" '
                         'style="font-size:1rem;padding:0.5em 0.9em">PASS</span>')
            elif rec.result == "fail":
                badge = ('<span class="badge text-bg-danger" '
                         'style="font-size:1rem;padding:0.5em 0.9em">FAIL</span>')
            else:
                badge = ('<span class="badge text-bg-secondary" '
                         'style="font-size:1rem;padding:0.5em 0.9em">'
                         'PENDING</span>')

            pending_note = ""
            if rec.subjective_pending:
                pending_note = (
                    '<div class="alert alert-warning mt-2 mb-0 py-1 px-2" '
                    'role="status">'
                    f'\u26a0 {esc(rec.subjective_pending)} subjective '
                    'response(s) still awaiting scoring — totals below are '
                    'not final.</div>')

            def cell(label, value, sub=""):
                sub_html = (f'<div class="text-muted small">{sub}</div>'
                            if sub else "")
                return (
                    '<div style="flex:1;min-width:120px;padding:0.4em 0.6em">'
                    f'<div class="text-muted small text-uppercase">{label}</div>'
                    f'<div style="font-size:1.15rem;font-weight:600">{value}</div>'
                    f'{sub_html}</div>')

            cells = []
            cells.append(cell(
                "Objective",
                f"{esc(obj_correct)} / {esc(obj_total)} correct",
                f"{esc(obj_pts)} / {esc(obj_max)} pts")
                if obj_total else "")
            if subj_total:
                cells.append(cell(
                    "Subjective",
                    f"{esc(subj_pass)} / {esc(subj_total)} passed",
                    f"{esc(sub_pts)} / {esc(sub_max)} pts"))
            cells.append(cell(
                "Total",
                f"{esc(total_pts)} / {esc(total_max)} pts"))
            cells.append(cell(
                "Score",
                f"{esc(round(pct, 2))}%",
                f"threshold {esc(round(thr, 2))}%"))
            if rec.violation_count:
                cells.append(cell(
                    "Violations", esc(rec.violation_count)))

            row = "".join(c for c in cells if c)
            rec.result_summary = (
                '<div class="border rounded p-2" '
                'style="background:#f8f9fa">'
                '<div style="display:flex;align-items:center;gap:0.75em;'
                'flex-wrap:wrap">'
                f'<div>{badge}</div>'
                '<div style="display:flex;flex-wrap:wrap;flex:1">'
                f'{row}</div></div>'
                f'{pending_note}</div>')

    @api.depends("response_ids.needs_llm", "response_ids.llm_score",
                 "response_ids.llm_max_score", "response_ids.llm_state",
                 "response_ids.llm_raw_100")
    def _compute_llm_progress(self):
        for rec in self:
            need = rec.response_ids.filtered(lambda r: r.needs_llm)
            scored = need.filtered(lambda r: r.llm_state == "scored")
            rec.llm_total_score = sum(scored.mapped("llm_score"))
            rec.llm_max_score = sum(need.mapped("llm_max_score"))
            # 'error' is terminal (surfaced to admin), NOT pending — it must not
            # pin the candidate result at pending forever. Only states the cron
            # will still act on count as pending.
            rec.subjective_pending = len(need.filtered(
                lambda r: r.llm_state in ("pending", "queued", "failed")))

    @api.depends("total_score", "max_possible_score", "llm_total_score",
                 "llm_max_score", "subjective_pending", "state",
                 "total_questions", "answered_count",
                 "response_ids.llm_state")
    def _compute_result(self):
        threshold = self._get_pass_threshold()
        for rec in self:
            rec.pass_threshold = threshold
            # EQUAL MARKS: denominator is ALL assigned questions (answered or
            # not) so skipping a subjective one can't raise the candidate's %.
            earned = (rec.total_score or 0) + (rec.llm_total_score or 0)
            possible = rec.total_questions or 0
            # Fallback when total_questions is unavailable (still equal-marks).
            if not possible:
                possible = (rec.max_possible_score or 0) + (rec.llm_max_score or 0)
            rec.score_percent = round(
                (earned / possible) * 100.0, 2) if possible else 0.0
            # Only meaningful once submitted with no subjective work pending —
            # never fail mid-scoring (exhausted LLM calls resolve as scored fails).
            if rec.state != "submitted" or rec.subjective_pending or not possible:
                rec.result = "pending"
            else:
                rec.result = "pass" if rec.score_percent >= threshold else "fail"

    @api.model
    def _get_pass_threshold(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.pass_threshold", "70")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = 70.0
        return val if 0 <= val <= 100 else 70.0

    def _compute_subjective_rollup(self):
        for rec in self:
            need = rec.response_ids.filtered(lambda r: r.needs_llm)
            if not need:
                rec.llm_state = "scored"
            elif all(r.llm_state in ("scored", "error") for r in need):
                # All answers reached a terminal state. If any hit a hard
                # scoring error, surface it as 'error' rather than a clean
                # 'scored'; otherwise the candidate is fully scored.
                if any(r.llm_state == "error" for r in need):
                    rec.llm_state = "error"
                else:
                    rec.llm_state = "scored"
                    rec.llm_scored_at = fields.Datetime.now()
            elif any(r.llm_state == "failed" for r in need):
                rec.llm_state = "partial" if any(
                    r.llm_state in ("scored", "error") for r in need) else "failed"
            elif any(r.llm_state in ("scored", "error") for r in need):
                rec.llm_state = "partial"
            else:
                rec.llm_state = "pending"

    def _send_single_invitation(self):
        tpl = self.env.ref(
            "etp_assessment_pro.mail_template_single_invitation",
            raise_if_not_found=False)
        if not tpl:
            return
        for rec in self:
            emp = rec.applicant_id
            to_email = (emp.email_from or
                        (emp.candidate_user_id.email
                         if emp.candidate_user_id else "") or "")
            if not to_email:
                base = self.env["ir.config_parameter"].sudo().get_param(
                    "web.base.url") or "http://localhost:8069"
                link = f"{base}/pro_assessment/{rec.access_token}"
                self.env["mail.mail"].sudo().create({
                    "subject": f"[NO EMAIL] {rec.assessment_id.name}",
                    "body_html": tpl._render_field("body_html", rec.ids)[rec.id],
                    "email_to": "",
                    "state": "cancel",
                    "failure_reason": f"No email on candidate "
                                      f"{emp.partner_name!r}. "
                                      f"Portal link: {link}",
                    "auto_delete": False,
                })
                continue
            # force_send: invitations go out immediately, not via mail-queue cron.
            tpl.send_mail(rec.id, force_send=True)

    def action_llm_score(self):
        """Trigger subjective scoring for this candidate; returns count scored."""
        from ..services import scoring as scoring_svc
        scored = 0
        for rec in self:
            if rec.state != "submitted":
                raise UserError(
                    f"Candidate '{rec.applicant_id.partner_name}' has not "
                    f"submitted — scoring runs on submitted assessments only.")
            rec.write({"llm_state": "scoring", "llm_error": False})
            try:
                scored += scoring_svc.score_evaluator(self.env, rec)
            except Exception as exc:
                _logger.exception(
                    "Subjective scoring failed for evaluator %s", rec.id)
                rec.write({
                    "llm_state": "failed",
                    "llm_error": str(exc)[:240],
                })
                continue
            rec._compute_subjective_rollup()
            rec.day_session_ids._rollup_day_score()
            rec._apply_results_disclosure()
        return scored

    def action_export_responses(self):
        """Per-candidate Export Responses: one row per answer for this candidate."""
        self.ensure_one()
        from ..services import export as export_svc
        return export_svc.export_responses(self.assessment_id, evaluator=self)

    def action_release_results(self):
        to_release = self.filtered(lambda r: not r.results_released)
        to_release.write({"results_released": True})
        already = len(self) - len(to_release)
        msg = "%s candidate(s) released." % len(to_release)
        if already:
            msg += " %s already released." % already
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Results Released",
                "message": msg,
                "type": "success",
                "sticky": False,
            },
        }

    @api.depends("day_session_ids.total_questions", "question_order")
    def _compute_total_questions(self):
        # Multi-day rolls up from day sessions; single-mode falls back to
        # question_order length (else _check_all_submitted breaks for flat tests).
        for rec in self:
            if rec.day_session_ids:
                rec.total_questions = sum(
                    rec.day_session_ids.mapped("total_questions"))
            else:
                try:
                    rec.total_questions = len(
                        json.loads(rec.question_order or "[]"))
                except (ValueError, TypeError):
                    rec.total_questions = 0

    @api.depends("response_ids", "response_ids.state", "response_ids.score")
    def _compute_progress(self):
        for rec in self:
            submitted = rec.response_ids.filtered(lambda r: r.state == "submitted")
            rec.answered_count = len(submitted)
            rec.total_score = sum(submitted.mapped("score"))
            rec.max_possible_score = sum(submitted.mapped("max_score"))

    @api.depends("started_at", "assessment_id.duration_minutes")
    def _compute_deadline_datetime(self):
        from datetime import timedelta
        for rec in self:
            if rec.started_at and rec.assessment_id.duration_minutes > 0:
                rec.deadline_datetime = rec.started_at + timedelta(
                    minutes=rec.assessment_id.duration_minutes)
            else:
                rec.deadline_datetime = False

    def is_time_expired(self):
        self.ensure_one()
        if not self.deadline_datetime:
            return False
        return fields.Datetime.now() > self.deadline_datetime

    def _rollup_overall_from_days(self):
        """Multi-day finalizer: submit the evaluator once every day_session is done."""
        for rec in self:
            sessions = rec.day_session_ids
            if not sessions:
                continue
            terminal = ("submitted", "scored", "missed")
            if all(s.state in terminal for s in sessions):
                rec.write({"state": "submitted", "is_locked": True})
                rec.assessment_id._check_plan_complete()
                rec._apply_results_disclosure()

    def _apply_results_disclosure(self):
        """Reveal results per assessment.results_release; never un-releases."""
        for rec in self:
            if rec.results_released:
                continue
            mode = rec.assessment_id.results_release
            sessions = rec.day_session_ids
            done_single = rec.state == "submitted" and not rec.subjective_pending
            if mode == "immediate":
                reveal = (any(s.state == "scored" for s in sessions)
                          if sessions else done_single)
            else:
                reveal = False
            if reveal:
                rec.results_released = True


class EtpAssessmentResponse(models.Model):
    _name = "etp.assessment.pro.response"
    _description = "Assessment Response"
    _order = "create_date desc"

    assessment_id = fields.Many2one(
        "etp.assessment.pro", required=True, ondelete="cascade")
    assessment_evaluator_id = fields.Many2one(
        "etp.assessment.pro.evaluator", string="Candidate Assignment",
        ondelete="cascade")
    # day_session_id is the multi-day link; null on single-mode responses.
    # Indexed because portal lookups filter by day_session_id heavily.
    day_session_id = fields.Many2one(
        "etp.assessment.pro.day.session", string="Day Session",
        ondelete="cascade", index=True)
    evaluator_id = fields.Many2one(
        "hr.applicant", string="Candidate", required=True)
    question_id = fields.Many2one(
        "etp.assessment.pro.question", required=True, ondelete="cascade")
    justification = fields.Text()
    line_ids = fields.One2many(
        "etp.assessment.pro.response.line", "response_id",
        string="Dimension Answers")
    state = fields.Selection(
        [("draft", "Draft"), ("submitted", "Submitted")], default="draft")

    score = fields.Integer(compute="_compute_score", store=True, string="Score")
    max_score = fields.Integer(
        compute="_compute_score", store=True, string="Max Possible")
    objective_score = fields.Integer(
        related="score", store=True, string="Objective Score", readonly=True)
    objective_max = fields.Integer(
        related="max_score", store=True, string="Objective Max", readonly=True)
    has_objective = fields.Boolean(
        compute="_compute_scoring_kind", store=True, string="Has Objective")

    needs_llm = fields.Boolean(
        compute="_compute_scoring_kind", store=True, string="Needs LLM")

    # ── LLM SCORING (v6): the LLM's 0-100 quality score is the IMMUTABLE TRUTH,
    # written once by the scorer. Everything the candidate's mark/pass depends on
    # is COMPUTED from it against the live Settings threshold, so changing the
    # threshold re-decides pass/fail without re-running the LLM. ──
    llm_raw_100 = fields.Float(
        string="Subjective Score (0-100)", readonly=True, copy=False,
        help="The grader's raw 0-100 quality score for this answer. Immutable "
             "truth: pass/fail and the earned mark are derived from this and "
             "the Settings threshold, so a threshold change re-decides them "
             "live without re-scoring.")
    llm_raw_score = fields.Float(
        string="Subjective Raw (0-1)", compute="_compute_subjective_marks",
        store=True, readonly=True, copy=False,
        help="llm_raw_100 expressed as a 0-1 fraction (display).")
    llm_passed = fields.Boolean(
        string="Subjective Passed", compute="_compute_subjective_marks",
        store=True, readonly=True, copy=False)
    llm_score = fields.Integer(
        string="Subjective Mark", compute="_compute_subjective_marks",
        store=True, readonly=True, copy=False,
        help="EQUAL-MARKS earned mark: 1 when llm_raw_100 >= the per-answer "
             "subjective threshold, else 0. Computed, never frozen.")
    llm_max_score = fields.Integer(
        string="Subjective Max", compute="_compute_subjective_marks",
        store=True, readonly=True, copy=False,
        help="Always 1 for a needs_llm answer (equal marks). Computed, so it "
             "never drifts as scoring progresses.")
    llm_feedback = fields.Text(string="Subjective Reasoning", readonly=True, copy=False)
    # ── SOP v6 audit trail (stored verbatim from the grader, per field) ──
    llm_gate = fields.Char(
        string="Scoring Gate", readonly=True, copy=False,
        help="The SOP gate that fired (empty_answer, off_topic, wrong_item, "
             "injection_attempt, ...) or 'none'.")
    llm_rubric_source = fields.Char(
        string="Rubric Source", readonly=True, copy=False,
        help="'supplied' when the item carried a grading block, 'generated' "
             "when the grader authored one from the prompt + skill.")
    llm_reference_answer = fields.Text(
        string="Reference Answer", readonly=True, copy=False,
        help="The grader's quasi-ground-truth model answer used to anchor "
             "judging.")
    llm_reasoning = fields.Text(
        string="Scoring Audit", readonly=True, copy=False,
        help="The grader's full evidence-first audit: each checklist point with "
             "its quote and finding, constraints, quality errors, and what "
             "capped the score.")
    llm_flags_json = fields.Text(
        string="Scoring Flags (JSON)", readonly=True, copy=False)
    llm_result_json = fields.Text(
        string="Full Scoring Result (JSON)", readonly=True, copy=False,
        help="The complete per-field v6 result object from the grader, kept "
             "for audit.")
    llm_attempts = fields.Integer(
        string="Subjective Attempts", default=0, copy=False)
    llm_state = fields.Selection(
        [
            ("not_needed", "Not Needed"),
            ("pending", "Pending"),
            ("queued", "Queued"),
            ("scored", "Scored"),
            ("failed", "Failed"),
            ("error", "Scoring Error"),
        ],
        default="not_needed",
        string="Subjective State",
        copy=False,
        help="scored = the grader returned a usable 0-100 score. error = the "
             "scoring call/parse failed after all attempts (surfaced, NOT a "
             "silent 0). failed = a recoverable miss the cron will retry.")

    subjective_score = fields.Integer(
        related="llm_score", string="Subjective Mark", readonly=True,
        help="The earned equal-marks subjective mark (0 or 1). For the raw "
             "0-100 grader score see llm_raw_100.")
    subjective_result = fields.Selection(
        [("pass", "Pass"), ("fail", "Fail")],
        string="Subjective Result", compute="_compute_subjective_result",
        store=True)
    subjective_reasoning = fields.Text(
        related="llm_feedback", string="Subjective Reasoning ", readonly=True)

    question_type = fields.Selection(
        related="question_id.question_type", string="Question Type",
        readonly=True)
    answer_summary = fields.Char(
        compute="_compute_answer_summary", string="Candidate Answer")
    correct_summary = fields.Char(
        compute="_compute_answer_summary", string="Correct Answer")
    # image_ab is graded objective verdicts + (optionally) an LLM justification.
    ab_mcq_pct = fields.Float(
        compute="_compute_ab_scores", string="Verdict %",
        help="image_ab: % of scoring axes whose chosen verdict matches the key "
             "(objective, no LLM).")
    ab_final_pct = fields.Float(
        compute="_compute_ab_scores", string="Final %",
        help="image_ab final 0-100: verdict-only when the justification is off, "
             "else ceil(0.75*verdict% + 0.25*justification%). Pass/fail vs the "
             "threshold gives the 1 mark.")

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_answer_summary(self):
        # SECURITY: own-records rule already scoped self to the candidate's own
        # responses, so sudo here is a read-only display bypass over own data.
        for rec in self:
            rec_s = rec.sudo()
            chosen = [
                line.selected_option_id.name
                for line in rec_s.line_ids
                if line.selected_option_id
            ]
            rec.answer_summary = ", ".join(chosen)
            correct = []
            for qd in rec_s.question_id.question_dimension_ids:
                for ol in qd.option_line_ids.filtered("is_correct"):
                    if ol.name:
                        correct.append(ol.name)
            rec.correct_summary = ", ".join(correct)

    @api.model
    def _subjective_threshold_100(self):
        """Per-answer subjective pass bar on a 0-100 scale (default 70). A
        legacy 0-1 value (e.g. '0.7') is tolerated by scaling up. This is read
        live on every recompute, so changing it in Settings re-decides every
        answer's pass/fail."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.subjective_pass_threshold", "70")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return 70.0
        if val <= 1.0:
            val = val * 100.0
        return val if 0.0 <= val <= 100.0 else 70.0

    def _image_ab_mcq_pct(self):
        """image_ab verdicts, objective: the % of scoring axes whose chosen
        verdict exactly matches the keyed verdict (no LLM)."""
        self.ensure_one()
        total = correct = 0
        for qd in self.question_id.question_dimension_ids:
            key = set(qd.option_line_ids.filtered("is_correct")
                      .mapped("master_option_id").ids)
            if not key:
                continue
            total += 1
            chosen = {line.selected_option_id.id for line in self.line_ids
                      if line.dimension_id.id == qd.dimension_id.id
                      and line.selected_option_id}
            if chosen == key:
                correct += 1
        return (correct / total * 100.0) if total else 0.0

    def _image_ab_uses_llm(self):
        """image_ab needs a Vertex call ONLY when the assessment requires a
        justification AND the candidate wrote one. Verdict-only answers (toggle
        off, or a blank justification) are fully scored from the verdicts."""
        self.ensure_one()
        return bool(
            self.question_id.question_type == "image_ab"
            and self.assessment_id.require_justification_image_comparison
            and (self.justification or "").strip())

    @api.depends("question_id.question_type", "line_ids.selected_option_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct",
                 "llm_raw_100",
                 "assessment_id.require_justification_image_comparison")
    def _compute_ab_scores(self):
        """image_ab verdict% (objective) and final% (verdict-only, or the
        ceil'd 0.75/0.25 blend when the justification is required)."""
        for rec in self:
            if rec.question_id.question_type != "image_ab":
                rec.ab_mcq_pct = 0.0
                rec.ab_final_pct = 0.0
                continue
            mcq = rec._image_ab_mcq_pct()
            rec.ab_mcq_pct = round(mcq, 2)
            if rec.assessment_id.require_justification_image_comparison:
                rec.ab_final_pct = float(
                    math.ceil(0.75 * mcq + 0.25 * (rec.llm_raw_100 or 0.0)))
            else:
                rec.ab_final_pct = float(math.ceil(mcq))

    @api.depends("llm_raw_100", "llm_state", "needs_llm", "ab_final_pct",
                 "question_id.question_type")
    def _compute_subjective_marks(self):
        """Derive the earned mark, pass flag and 0-1 display score from the
        IMMUTABLE llm_raw_100 against the LIVE per-answer threshold. Because
        these are computed (not frozen at scoring time), changing the threshold
        in Settings re-decides pass/fail for every already-scored answer without
        re-running the LLM. EQUAL MARKS: max is always 1, mark is 1 when the raw
        score clears the threshold else 0 (a gated/zero answer fails)."""
        threshold = self._subjective_threshold_100()
        for rec in self:
            if rec.question_id.question_type == "image_ab":
                # image_ab's mark is its blended (verdict + justification) score
                # vs the threshold. The verdict part is objective and known at
                # submit; the justification part (when required) is the LLM raw.
                final = rec.ab_final_pct or 0.0
                rec.llm_raw_score = round(final / 100.0, 4)
                rec.llm_max_score = 1
                passed = rec.llm_state == "scored" and final >= threshold
                rec.llm_passed = passed
                rec.llm_score = 1 if passed else 0
                continue
            if not rec.needs_llm:
                rec.llm_raw_score = 0.0
                rec.llm_passed = False
                rec.llm_score = 0
                rec.llm_max_score = 0
                continue
            raw100 = rec.llm_raw_100 or 0.0
            rec.llm_raw_score = round(raw100 / 100.0, 4)
            rec.llm_max_score = 1
            # Only a SCORED answer can pass; pending/error/failed earn 0 but
            # still count against the candidate (max stays 1).
            passed = rec.llm_state == "scored" and raw100 >= threshold
            rec.llm_passed = passed
            rec.llm_score = 1 if passed else 0

    @api.depends("llm_state", "llm_passed", "needs_llm")
    def _compute_subjective_result(self):
        for rec in self:
            if rec.needs_llm and rec.llm_state == "scored":
                rec.subjective_result = "pass" if rec.llm_passed else "fail"
            else:
                rec.subjective_result = False

    @api.depends("question_id", "question_id.question_type", "justification",
                 "line_ids.selected_option_id")
    def _compute_scoring_kind(self):
        # EQUAL MARKS: has_objective = mcq/msq (code-marked); needs_llm =
        # subjective_* and image_ab/image_text (LLM-marked, not code-objective).
        for rec in self:
            qtype = rec.question_id.question_type
            rec.has_objective = qtype in ("mcq", "msq")
            just = (rec.justification or "").strip()
            is_placeholder = just.startswith("[Auto-submitted")
            if qtype == "image_ab":
                # The image_ab answer IS the per-axis verdicts (line_ids); the
                # justification is supplementary. Grade it whenever verdicts
                # exist and never gate on the justification — a verdicts-only
                # answer MUST still be scored (LLM grades verdict match + any
                # justification). Auto-submitted placeholders stay ungraded.
                rec.needs_llm = bool(rec.line_ids) and not is_placeholder
            else:
                rec.needs_llm = (
                    qtype in ("subjective_justification", "subjective_rubric",
                              "image_text")
                    and bool(just)
                    and not is_placeholder
                )

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_type",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_score(self):
        # EQUAL MARKS, all-or-nothing: mcq/msq score 1 only if chosen master
        # option set exactly equals the correct set; others are 0/0 (LLM-marked).
        for rec in self:
            qtype = rec.question_id.question_type
            if qtype not in ("mcq", "msq"):
                rec.score = 0
                rec.max_score = 0
                continue
            objective_dims = rec.question_id.question_dimension_ids.filtered(
                lambda qd: qd.option_line_ids.filtered("is_correct"))
            if not objective_dims:
                # Mis-keyed question (no correct option): score 0/1 (not 0/0) so
                # it still counts against the candidate rather than vanishing.
                rec.score = 0
                rec.max_score = 1
                continue
            all_correct = True
            for qd in objective_dims:
                correct_for_dim = {
                    ol.master_option_id.id
                    for ol in qd.option_line_ids.filtered("is_correct")
                    if ol.master_option_id}
                chosen_for_dim = {
                    line.selected_option_id.id
                    for line in rec.line_ids
                    if line.selected_option_id
                    and line.dimension_id.id == qd.dimension_id.id}
                if chosen_for_dim != correct_for_dim:
                    all_correct = False
                    break
            rec.score = 1 if all_correct else 0
            rec.max_score = 1

    def action_submit(self):
        for rec in self:
            if rec.state == "submitted":
                raise UserError("This response is already submitted.")
            if (rec.assessment_evaluator_id
                    and rec.assessment_evaluator_id.is_locked):
                raise UserError(
                    "This assessment is already locked. Cannot modify responses.")
            qtype = rec.question_id.question_type
            # image_text is hybrid: always needs the written response, and also
            # needs a pick per gate dimension only when the question carries any.
            has_dims = bool(rec.question_id.question_dimension_ids)
            if qtype in ("mcq", "msq", "image_ab") and not rec.line_ids:
                raise UserError(
                    "Please answer at least one dimension before submitting.")
            if qtype in ("subjective_justification", "subjective_rubric",
                         "image_text") \
                    and not (rec.justification or "").strip():
                raise UserError(
                    "Please provide a justification before submitting.")
            if qtype == "image_text" and has_dims and not rec.line_ids:
                raise UserError(
                    "Please answer the multiple-choice checks before "
                    "submitting.")
            rec.write({"state": "submitted"})

            # Queue subjective scoring on submit — NEVER scored inline on the
            # candidate's path. image_ab ALWAYS enqueues (its verdicts are the
            # answer, even with a blank justification); the others only when a
            # justification was written.
            if qtype == "image_ab" or (rec.justification or "").strip():
                rec._enqueue_subjective_scoring()

            if rec.assessment_evaluator_id:
                rec._check_all_submitted()

    def _enqueue_subjective_scoring(self):
        """Queue needs_llm responses for subjective scoring. Scoring NEVER runs
        inline on the candidate's submit — it is admin-triggered (Run Subjective
        Evaluation) and drained by the cron in batches of 20. If the assessment
        opts into auto-queue (llm_auto_score), flag the evaluator so the cron
        picks it up next tick — still batched, never on the candidate's path."""
        auto_eval_ids = set()
        repend_eval_ids = set()
        for rec in self:
            qtype = rec.question_id.question_type
            if qtype == "image_ab":
                # Verdicts are objective (scored now). Only an answer that needs
                # the LLM (required + non-blank justification) is deferred;
                # verdict-only answers are fully determined at submit -> scored.
                if not rec.line_ids:
                    rec.llm_state = "not_needed"
                elif rec._image_ab_uses_llm():
                    rec.llm_state = "pending"
                    if rec.assessment_evaluator_id:
                        repend_eval_ids.add(rec.assessment_evaluator_id.id)
                        if rec.assessment_id.llm_auto_score:
                            auto_eval_ids.add(rec.assessment_evaluator_id.id)
                else:
                    rec.llm_state = "scored"
                continue
            if not (rec.justification or "").strip() or not rec.needs_llm:
                rec.llm_state = "not_needed"
                continue
            rec.llm_state = "pending"
            if rec.assessment_evaluator_id:
                repend_eval_ids.add(rec.assessment_evaluator_id.id)
                if rec.assessment_id.llm_auto_score:
                    auto_eval_ids.add(rec.assessment_evaluator_id.id)
        if auto_eval_ids:
            self.env["etp.assessment.pro.evaluator"].browse(
                auto_eval_ids).write({"scoring_requested": True})
        # A response moved (back) to 'pending' must re-derive its evaluator's
        # rollup: the cron/auto path gates on the evaluator's stored llm_state,
        # so an already-'scored' candidate would otherwise never be re-graded.
        if repend_eval_ids:
            self.env["etp.assessment.pro.evaluator"].browse(
                repend_eval_ids)._compute_subjective_rollup()

    def _check_all_submitted(self):
        evaluator = self.assessment_evaluator_id
        if not evaluator:
            return
        # Multi-day path: rollup is driven by day.session._rollup_day_score,
        # don't flip the evaluator here.
        if self.day_session_id:
            return
        total_expected = evaluator.total_questions
        submitted_count = self.env["etp.assessment.pro.response"].search_count([
            ("assessment_evaluator_id", "=", evaluator.id),
            ("state", "=", "submitted"),
        ])
        if submitted_count >= total_expected:
            evaluator.write({"state": "submitted", "is_locked": True})
            self._check_assessment_complete()

    def _check_assessment_complete(self):
        assessment = self.assessment_id
        evs = assessment.assessment_evaluator_ids
        if evs and all(a.state == "submitted" for a in evs):
            assessment.write({"state": "done"})

    @api.constrains("state")
    def _check_locked(self):
        for rec in self:
            if (rec.assessment_evaluator_id
                    and rec.assessment_evaluator_id.is_locked
                    and rec.state != "submitted"):
                raise ValidationError(
                    "Cannot modify responses after the assessment is locked.")


class EtpAssessmentResponseLine(models.Model):
    _name = "etp.assessment.pro.response.line"
    _description = "Assessment Response Line"

    response_id = fields.Many2one(
        "etp.assessment.pro.response", required=True, ondelete="cascade")
    dimension_id = fields.Many2one(
        "etp.assessment.pro.dimension", required=True, ondelete="restrict")
    # References the MASTER option; portal submits master IDs and _compute_score
    # compares against question.dimension.option_line.master_option_id.
    selected_option_id = fields.Many2one(
        "etp.assessment.pro.dimension.option",
        string="Selected Option",
        ondelete="restrict",
    )
