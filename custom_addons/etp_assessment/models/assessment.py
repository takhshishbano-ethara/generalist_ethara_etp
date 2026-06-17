# -*- coding: utf-8 -*-
"""ETP Assessment — lifecycle models.

Carries the container (etp.assessment), the per-candidate assignment
(etp.assessment.evaluator), and the per-question response pair
(etp.assessment.response + etp.assessment.response.line).

Two modes:
- ``single``    — one flat test (legacy/simple). Question pool comes from
  ``category_id``; one evaluator row per candidate holds ``question_order``.
- ``multi_day`` — N-day plan. Each day binds a skill (see assessment_day.py);
  per-day sessions on the evaluator drive the candidate flow. Single-mode
  fields are not used in this path.

Scoring is question_type aware:
- mcq / msq: ALL-OR-NOTHING — chosen option set must EXACTLY match the
  defined correct set across all objective dimensions, else 0.
- subjective_justification / subjective_rubric: objective max is 0; the
  LLM scorer (services/scoring.py) decides PASS/FAIL of the written
  justification and awards full subjective points on PASS, 0 on FAIL.
"""
import base64
import csv
import io
import json
import logging
import random
import uuid

from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class EtpAssessment(models.Model):
    _name = "etp.assessment"
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

    # The mode decides which branch of the form / lifecycle drives the
    # assessment. category_id is required only in single; skill_id on day
    # ------------------------------------------------------------------
    # Question-source fields. Every assessment is a day-based plan now
    # (a one-off test is simply a 1-day plan). category_id/question_limit
    # are retained as OPTIONAL metadata (the Flutter/extension serializer
    # still reads them); the day binds questions via skill_id.
    # ------------------------------------------------------------------
    category_id = fields.Many2one(
        "etp.assessment.category",
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
        "etp.assessment.question",
        "etp_assessment_question_rel",
        "assessment_id",
        "question_id",
        string="Selected Questions",
    )
    total_questions_available = fields.Integer(
        compute="_compute_total_questions_available"
    )

    # ------------------------------------------------------------------
    # MULTI-DAY fields. day_ids/day_session_ids reference models declared
    # in assessment_day.py (loaded right after this file — both classes
    # are in the registry by the time One2many resolves).
    # ------------------------------------------------------------------
    num_days = fields.Integer(string="Number of Days", default=0)
    sequential_days = fields.Boolean(
        string="Sequential Days", default=True,
        help="Days unlock in order: submitting day N opens day N+1. "
             "Off = all days open simultaneously (subject to scheduled_start).",
    )
    day_ids = fields.One2many(
        "etp.assessment.day", "assessment_id", string="Days"
    )
    day_count = fields.Integer(compute="_compute_day_count", store=True)
    day_session_ids = fields.One2many(
        "etp.assessment.day.session", "assessment_id",
        string="Per-Candidate Day Sessions"
    )
    day_session_count = fields.Integer(
        compute="_compute_day_session_count", store=True
    )
    results_release = fields.Selection(
        [
            ("manual", "Manual (admin releases)"),
            ("eod", "End of Day"),
            ("immediate", "Immediate"),
        ],
        default="manual",
        required=True,
        string="Results Release",
        help="When candidate-facing results are revealed. Manual = admin "
             "flips results_released on each evaluator; eod = at assessment "
             "done; immediate = as each day is scored.",
    )

    # Shared scheduling.
    start_date = fields.Datetime(string="Start Date")
    end_date = fields.Datetime(string="End Date")
    deadline = fields.Date()

    # ------------------------------------------------------------------
    # Candidates: many2many evaluator_ids is the "assigned" list (admin
    # toggles before launch); assessment_evaluator_ids is the materialized
    # row per candidate (created by action_generate_plan).
    # ------------------------------------------------------------------
    evaluator_ids = fields.Many2many(
        "hr.employee",
        "etp_assessment_evaluator_rel",
        "assessment_id",
        "employee_id",
        string="Candidates",
    )
    assessment_evaluator_ids = fields.One2many(
        "etp.assessment.evaluator", "assessment_id",
        string="Candidate Assignments"
    )
    response_ids = fields.One2many(
        "etp.assessment.response", "assessment_id", string="Responses"
    )
    response_count = fields.Integer(compute="_compute_response_count", store=True)

    # ------------------------------------------------------------------
    # Rules & Proctoring — drive the candidate portal's anti-cheat script.
    # ------------------------------------------------------------------
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
        help="Legacy switch from the older schema; no-op when no questions "
             "of type image_comparison exist in the new question bank.",
    )
    llm_auto_score = fields.Boolean(
        string="Auto LLM-Score on Submit", default=False)

    candidate_csv_file = fields.Binary(string="Upload Candidates CSV")
    candidate_csv_filename = fields.Char(string="CSV Filename")

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
        Question = self.env["etp.assessment.question"]
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

        Employee = self.env["hr.employee"].sudo()
        imported_ids = []
        errors = []

        for row_num, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip()
            if not name or not email:
                errors.append(f"Row {row_num}: name and email are required.")
                continue
            employee = Employee.search([("work_email", "=", email)], limit=1)
            if not employee:
                emp_vals = {"name": name, "work_email": email}
                job_title = (row.get("job_title") or "").strip()
                department = (row.get("department") or "").strip()
                if job_title:
                    emp_vals["job_title"] = job_title
                if department:
                    dept = self.env["hr.department"].sudo().search(
                        [("name", "=", department)], limit=1)
                    if dept:
                        emp_vals["department_id"] = dept.id
                employee = Employee.create(emp_vals)
            imported_ids.append(employee.id)

        if errors:
            raise UserError(
                "CSV import completed with errors:\n" + "\n".join(errors))

        existing_ids = self.evaluator_ids.ids
        new_ids = list(set(imported_ids) - set(existing_ids))
        if new_ids:
            self.write({"evaluator_ids": [(4, eid) for eid in new_ids]})
        self.write({
            "candidate_csv_file": False,
            "candidate_csv_filename": False,
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Candidates Imported",
                "message": f"{len(new_ids)} new candidate(s) added. "
                           f"{len(imported_ids) - len(new_ids)} already assigned.",
                "type": "success",
                "sticky": False,
            },
        }

    def action_download_candidate_template(self):
        self.ensure_one()
        csv_content = "name,email,job_title,department\n"
        csv_content += "John Doe,john.doe@example.com,Evaluator,Engineering\n"
        csv_content += "Jane Smith,jane.smith@example.com,Senior Evaluator,Design\n"
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

    # ------------------------------------------------------------------
    # LLM scoring — trigger subjective scoring on every submitted candidate.
    # Implementation in services/scoring.py; per-evaluator inline call (no
    # broker per D3) — one Vertex call per candidate, all their needs_llm
    # responses bundled.
    # ------------------------------------------------------------------
    def action_llm_score_all(self):
        self.ensure_one()
        todo = self.assessment_evaluator_ids.filtered(
            lambda ev: ev.state == "submitted" and ev.llm_state in
            ("pending", "scoring", "partial", "failed"))
        if not todo:
            raise UserError("No submitted candidates awaiting subjective scoring.")
        scored_count = 0
        for ev in todo:
            scored_count += ev.action_llm_score()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Subjective Scoring Done",
                "message": f"{len(todo)} candidate(s) processed, "
                           f"{scored_count} response(s) scored.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _cron_llm_auto_score(self):
        """Background drainer: subjective scoring for assessments with
        ``llm_auto_score`` ON, for any evaluator still in a non-terminal
        llm_state. Runs from data/cron.xml every minute; idempotent — a
        terminal ``scored`` evaluator is a no-op.

        No broker per D3 — scoring runs inline in this cron transaction.
        """
        # Advisory lock so two cron workers don't double-score the same
        # evaluator (lock auto-releases at commit/rollback).
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (827193,))
        if not self.env.cr.fetchone()[0]:
            return
        evaluators = self.env["etp.assessment.evaluator"].search([
            ("state", "=", "submitted"),
            ("llm_state", "in", ("pending", "scoring", "partial", "failed")),
            ("assessment_id.llm_auto_score", "=", True),
        ], limit=20)
        for ev in evaluators:
            try:
                ev.action_llm_score()
            except Exception:
                _logger.exception(
                    "Auto-score failed for evaluator %s", ev.id)
                continue

    def action_export_results(self):
        """Export results as CSV: one row per candidate.

        Columns: candidate, days_done/total, objective points, subjective
        points, score %, result, rank. Re-uses the question.py
        ir.attachment + act_url download pattern.
        """
        self.ensure_one()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "rank", "candidate", "email", "days_done_total",
            "objective_score", "objective_max",
            "subjective_score", "subjective_max",
            "total_score", "total_max",
            "score_percent", "result",
        ])
        ranked = self.assessment_evaluator_ids.sorted(
            key=lambda e: (-e.score_percent, e.employee_id.name or ""))
        for idx, ev in enumerate(ranked, start=1):
            emp = ev.employee_id
            obj_score = ev.total_score or 0
            obj_max = ev.max_possible_score or 0
            subj_score = ev.llm_total_score or 0
            subj_max = ev.llm_max_score or 0
            w.writerow([
                idx,
                emp.name or "",
                emp.work_email or emp.private_email or "",
                ev.day_progress_label or "",
                obj_score, obj_max,
                subj_score, subj_max,
                obj_score + subj_score, obj_max + subj_max,
                ev.score_percent or 0.0,
                ev.result or "pending",
            ])
        content = buf.getvalue().encode("utf-8")
        attachment = self.env["ir.attachment"].create({
            "name": f"{self.name}_results.csv",
            "type": "binary",
            "datas": base64.b64encode(content).decode(),
            "mimetype": "text/csv",
            "res_model": self._name,
            "res_id": self.id,
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def _check_plan_complete(self):
        # Called by evaluator._rollup_overall_from_days when a candidate
        # finishes their last day. Flip the assessment to done when every
        # assigned candidate is submitted (mirrors single-mode behavior).
        for rec in self:
            evs = rec.assessment_evaluator_ids
            if evs and all(e.state == "submitted" for e in evs):
                if rec.state == "in_progress":
                    rec.write({"state": "done"})


class EtpAssessmentEvaluator(models.Model):
    _name = "etp.assessment.evaluator"
    _description = "Assessment Candidate Assignment"
    _order = "create_date desc"
    _rec_name = "employee_id"

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade")
    employee_id = fields.Many2one(
        "hr.employee", string="Candidate", required=True, ondelete="restrict")
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
    total_questions = fields.Integer()
    answered_count = fields.Integer(
        compute="_compute_progress", store=True, string="Answered")
    total_score = fields.Integer(
        compute="_compute_progress", store=True, string="Total Score")
    max_possible_score = fields.Integer(
        compute="_compute_progress", store=True, string="Max Possible Score")
    response_ids = fields.One2many(
        "etp.assessment.response", "assessment_evaluator_id",
        string="Responses")
    day_session_ids = fields.One2many(
        "etp.assessment.day.session", "evaluator_id",
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

    # Subjective rollup mirrors the reference. llm_state drives the
    # cron drainer + UI badge.
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
             "Manual = admin flips; eod = on assessment done; immediate = on submit.",
    )

    # Day progress label — drives the My Assessments badge "2 / 5".
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

    @api.depends("response_ids.needs_llm", "response_ids.llm_score",
                 "response_ids.llm_max_score", "response_ids.llm_state")
    def _compute_llm_progress(self):
        for rec in self:
            need = rec.response_ids.filtered(lambda r: r.needs_llm)
            scored = need.filtered(lambda r: r.llm_state == "scored")
            rec.llm_total_score = sum(scored.mapped("llm_score"))
            rec.llm_max_score = sum(need.mapped("llm_max_score"))
            rec.subjective_pending = len(need.filtered(
                lambda r: r.llm_state in ("pending", "queued", "failed")))

    @api.depends("total_score", "max_possible_score", "llm_total_score",
                 "llm_max_score", "subjective_pending", "state",
                 "response_ids.llm_state")
    def _compute_result(self):
        threshold = self._get_pass_threshold()
        for rec in self:
            rec.pass_threshold = threshold
            earned = (rec.total_score or 0) + (rec.llm_total_score or 0)
            possible = (rec.max_possible_score or 0) + (rec.llm_max_score or 0)
            rec.score_percent = round(
                (earned / possible) * 100.0, 2) if possible else 0.0
            # Result is only meaningful once submitted AND no subjective work
            # is still pending — never fail a candidate mid-scoring.
            if rec.state != "submitted" or rec.subjective_pending or not possible:
                rec.result = "pending"
            else:
                rec.result = "pass" if rec.score_percent >= threshold else "fail"

    @api.model
    def _get_pass_threshold(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.pass_threshold", "70")
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
            elif all(r.llm_state == "scored" for r in need):
                rec.llm_state = "scored"
                rec.llm_scored_at = fields.Datetime.now()
            elif any(r.llm_state == "failed" for r in need):
                rec.llm_state = "partial" if any(
                    r.llm_state == "scored" for r in need) else "failed"
            elif any(r.llm_state == "scored" for r in need):
                rec.llm_state = "partial"
            else:
                rec.llm_state = "pending"

    def action_llm_score(self):
        """Trigger subjective scoring for this candidate's needs_llm
        responses. Returns the count of responses scored (informational).

        Delegates to services/scoring.score_evaluator — one Vertex call
        per candidate (D3, batched).
        """
        from ..services import scoring as scoring_svc
        scored = 0
        for rec in self:
            if rec.state != "submitted":
                raise UserError(
                    f"Candidate '{rec.employee_id.name}' has not submitted "
                    f"— scoring runs on submitted assessments only.")
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
        return scored

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
        """Multi-day finalizer: flip the evaluator to ``submitted`` once
        every day_session is finished. Called by day.session.action_submit_day
        after _unlock_next_day.
        """
        for rec in self:
            sessions = rec.day_session_ids
            if not sessions:
                continue
            terminal = ("submitted", "scored", "missed")
            if all(s.state in terminal for s in sessions):
                rec.write({"state": "submitted", "is_locked": True})
                rec.assessment_id._check_plan_complete()


class EtpAssessmentResponse(models.Model):
    _name = "etp.assessment.response"
    _description = "Assessment Response"
    _order = "create_date desc"

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade")
    assessment_evaluator_id = fields.Many2one(
        "etp.assessment.evaluator", string="Candidate Assignment",
        ondelete="cascade")
    # day_session_id is the multi-day link; null on single-mode responses.
    # Indexed because portal lookups filter by day_session_id heavily.
    day_session_id = fields.Many2one(
        "etp.assessment.day.session", string="Day Session",
        ondelete="cascade", index=True)
    evaluator_id = fields.Many2one(
        "hr.employee", string="Candidate", required=True)
    question_id = fields.Many2one(
        "etp.assessment.question", required=True, ondelete="cascade")
    justification = fields.Text()
    line_ids = fields.One2many(
        "etp.assessment.response.line", "response_id",
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
    llm_score = fields.Integer(string="Subjective Score", readonly=True, copy=False)
    llm_max_score = fields.Integer(
        string="Subjective Max", readonly=True, copy=False)
    llm_passed = fields.Boolean(
        string="Subjective Passed", readonly=True, copy=False)
    llm_raw_score = fields.Float(
        string="Subjective Raw (0-1)", readonly=True, copy=False)
    llm_feedback = fields.Text(string="Subjective Reasoning", readonly=True, copy=False)
    llm_attempts = fields.Integer(
        string="Subjective Attempts", default=0, copy=False)
    llm_state = fields.Selection(
        [
            ("not_needed", "Not Needed"),
            ("pending", "Pending"),
            ("queued", "Queued"),
            ("scored", "Scored"),
            ("failed", "Failed"),
        ],
        default="not_needed",
        string="Subjective State",
        copy=False,
    )

    subjective_score = fields.Integer(
        related="llm_score", string="Subjective Points", readonly=True)
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

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_answer_summary(self):
        for rec in self:
            chosen = [
                line.selected_option_id.name
                for line in rec.line_ids
                if line.selected_option_id
            ]
            rec.answer_summary = ", ".join(chosen)
            correct = []
            for qd in rec.question_id.question_dimension_ids:
                for ol in qd.option_line_ids.filtered("is_correct"):
                    if ol.name:
                        correct.append(ol.name)
            rec.correct_summary = ", ".join(correct)

    @api.depends("llm_state", "llm_passed", "needs_llm")
    def _compute_subjective_result(self):
        for rec in self:
            if rec.needs_llm and rec.llm_state == "scored":
                rec.subjective_result = "pass" if rec.llm_passed else "fail"
            else:
                rec.subjective_result = False

    @api.depends("question_id", "question_id.question_type", "justification")
    def _compute_scoring_kind(self):
        # New question_type drives both flags directly — no more dimension
        # presence sniffing. has_objective fires for mcq/msq; needs_llm fires
        # for subjective_* when the candidate left a non-placeholder
        # justification.
        for rec in self:
            qtype = rec.question_id.question_type
            rec.has_objective = qtype in ("mcq", "msq")
            just = (rec.justification or "").strip()
            is_placeholder = just.startswith("[Auto-submitted")
            rec.needs_llm = (
                qtype in ("subjective_justification", "subjective_rubric")
                and bool(just)
                and not is_placeholder
            )

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_type",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_score(self):
        # ALL-OR-NOTHING per question:
        # - mcq / msq: chosen MASTER-option set across all objective
        #   dimensions must equal the correct set. Equal -> max_score,
        #   else 0.
        # - subjective_*: objective score is 0 (LLM scores separately).
        for rec in self:
            qtype = rec.question_id.question_type
            if qtype not in ("mcq", "msq"):
                rec.score = 0
                rec.max_score = 0
                continue
            objective_dims = rec.question_id.question_dimension_ids.filtered(
                lambda qd: qd.option_line_ids.filtered("is_correct"))
            max_score = len(objective_dims)
            if not max_score:
                rec.score = 0
                rec.max_score = 0
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
            rec.score = max_score if (objective_dims and all_correct) else 0
            rec.max_score = max_score

    def action_submit(self):
        for rec in self:
            if rec.state == "submitted":
                raise UserError("This response is already submitted.")
            if (rec.assessment_evaluator_id
                    and rec.assessment_evaluator_id.is_locked):
                raise UserError(
                    "This assessment is already locked. Cannot modify responses.")
            qtype = rec.question_id.question_type
            # Objective questions need at least one option pick; subjective
            # questions need a non-empty justification.
            if qtype in ("mcq", "msq") and not rec.line_ids:
                raise UserError(
                    "Please answer at least one dimension before submitting.")
            if qtype in ("subjective_justification", "subjective_rubric") \
                    and not (rec.justification or "").strip():
                raise UserError(
                    "Please provide a justification before submitting.")
            rec.write({"state": "submitted"})

            # Auto-enqueue subjective scoring if a justification is present
            # (subjective questions) — guarded by llm_auto_score on the
            # assessment when triggered from a submit (autoscore=True).
            if (rec.justification or "").strip():
                rec.with_context(autoscore=True)._enqueue_subjective_scoring()

            if rec.assessment_evaluator_id:
                rec._check_all_submitted()

    def _enqueue_subjective_scoring(self):
        """Inline subjective scoring (no broker, per D3).

        On submit: if the assessment has llm_auto_score, score this
        candidate's full needs_llm set in one Vertex call right now.
        Otherwise just mark llm_state=pending — the cron drainer will
        sweep it on the next tick.
        """
        # Group by evaluator so one inline call scores everything that
        # candidate has waiting in a single Vertex round-trip.
        by_eval = {}
        for rec in self:
            if not (rec.justification or "").strip():
                rec.llm_state = "not_needed"
                continue
            if not rec.needs_llm:
                # justification on a non-subjective question — ignore.
                rec.llm_state = "not_needed"
                continue
            # Set ceiling now; scoring.py writes it only post-LLM, so
            # partial-scoring rollups would otherwise inflate score_percent.
            rec.llm_max_score = rec._subjective_points()
            eval_rec = rec.assessment_evaluator_id
            if not eval_rec:
                rec.llm_state = "pending"
                continue
            rec.llm_state = "pending"
            # autoscore=True path: only fire when llm_auto_score is on.
            if self.env.context.get("autoscore") \
                    and not rec.assessment_id.llm_auto_score:
                continue
            by_eval.setdefault(eval_rec.id, eval_rec)
        # One inline scoring call per evaluator (D3).
        from ..services import scoring as scoring_svc
        for ev in by_eval.values():
            try:
                scoring_svc.score_evaluator(self.env, ev)
            except Exception:
                _logger.exception(
                    "Inline subjective scoring failed for evaluator %s; "
                    "responses left pending for cron drainer", ev.id)

    @api.model
    def _subjective_points(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.subjective_points", "10")
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = 10
        return val if val > 0 else 10

    @api.model
    def _subjective_pass_threshold(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.subjective_pass_threshold", "0.7")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return 0.7
        if val > 1.0:
            val = val / 100.0
        return val if 0.0 <= val <= 1.0 else 0.7

    def _check_all_submitted(self):
        evaluator = self.assessment_evaluator_id
        if not evaluator:
            return
        # Multi-day path: rollup is driven by day.session._rollup_day_score,
        # don't flip the evaluator here.
        if self.day_session_id:
            return
        total_expected = evaluator.total_questions
        submitted_count = self.env["etp.assessment.response"].search_count([
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
    _name = "etp.assessment.response.line"
    _description = "Assessment Response Line"

    response_id = fields.Many2one(
        "etp.assessment.response", required=True, ondelete="cascade")
    dimension_id = fields.Many2one(
        "etp.assessment.dimension", required=True, ondelete="restrict")
    # selected_option_id references the MASTER option (etp.assessment.
    # dimension.option). The portal form submits master IDs; scoring on
    # response._compute_score compares against the master IDs reachable
    # via question.dimension.option_line.master_option_id.
    selected_option_id = fields.Many2one(
        "etp.assessment.dimension.option",
        string="Selected Option",
        ondelete="restrict",
    )
