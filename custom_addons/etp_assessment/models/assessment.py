# -*- coding: utf-8 -*-
import base64
import csv
import io
import json
import random
import uuid

import logging

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
    category_id = fields.Many2one(
        "etp.assessment.category",
        string="Question Category",
        required=True,
        ondelete="restrict",
    )
    question_limit = fields.Integer(
        string="Number of Questions",
        help="Number of questions to pick from the category. 0 = all questions.",
        default=0,
    )
    start_date = fields.Datetime(string="Start Date")
    end_date = fields.Datetime(string="End Date")
    question_ids = fields.Many2many(
        "etp.assessment.question",
        "etp_assessment_question_rel",
        "assessment_id",
        "question_id",
        string="Selected Questions",
    )
    evaluator_ids = fields.Many2many(
        "hr.employee",
        "etp_assessment_evaluator_rel",
        "assessment_id",
        "employee_id",
        string="Candidates",
    )
    assessment_evaluator_ids = fields.One2many(
        "etp.assessment.evaluator", "assessment_id", string="Candidate Assignments"
    )
    response_ids = fields.One2many(
        "etp.assessment.response", "assessment_id", string="Responses"
    )
    response_count = fields.Integer(compute="_compute_response_count", store=True)
    duration_minutes = fields.Integer(
        string="Duration (Minutes)",
        help="Time limit for candidates to complete the assessment. 0 = no limit.",
        default=0,
    )
    deadline = fields.Date()

    # ------------------------------------------------------------------
    # Rules & Proctoring configuration
    # These flags drive the candidate portal: which rules are shown on the
    # instructions (start) screen and which client-side detectors are armed.
    # They stay editable while the assessment is in progress so admins can
    # relax/tighten proctoring live without resetting the assessment.
    # ------------------------------------------------------------------
    rule_block_tab_switch = fields.Boolean(
        string="Blur on Tab/Window Switch",
        default=True,
        help="Blur assessment content when the candidate switches tabs or windows.",
    )
    rule_block_screenshot = fields.Boolean(
        string="Block Screenshots & Screen Capture",
        default=True,
        help="Detect screenshot shortcuts, screen-capture APIs and screenshot "
             "extensions. A detection raises a violation.",
    )
    rule_block_copy_paste = fields.Boolean(
        string="Block Copy / Paste",
        default=True,
        help="Disable text selection, copy, cut, paste and save/print shortcuts.",
    )
    rule_block_right_click = fields.Boolean(
        string="Block Right-Click",
        default=True,
        help="Disable the browser context menu during the assessment.",
    )
    rule_block_devtools = fields.Boolean(
        string="Block Developer Tools",
        default=True,
        help="Detect opened browser developer tools. A detection raises a violation.",
    )
    rule_watermark = fields.Boolean(
        string="Watermark Content",
        default=True,
        help="Overlay candidate name + timestamp watermark on all assessment pages.",
    )
    violation_action = fields.Selection(
        [
            ("auto_submit", "Auto-submit assessment"),
            ("log_only", "Log violation only"),
        ],
        string="On Violation",
        default="auto_submit",
        required=True,
        help="Auto-submit: a violation immediately locks and submits the "
             "candidate's assessment. Log only: the violation is recorded on "
             "the candidate assignment but the assessment continues.",
    )
    require_justification_image_comparison = fields.Boolean(
        string="Require Justification for Image Comparison",
        default=False,
        help="When disabled, candidates do not have to type a written "
             "justification on image-comparison questions. Other question "
             "types always require a justification.",
    )
    llm_auto_score = fields.Boolean(
        string="Auto LLM-Score on Submit",
        default=False,
        help="When enabled, each candidate's responses are scored by the LLM "
             "automatically as soon as they submit (one Bedrock call per "
             "candidate). Keep disabled until Bedrock credentials are proven.",
    )

    total_questions_available = fields.Integer(
        compute="_compute_total_questions_available"
    )
    candidate_csv_file = fields.Binary(string="Upload Candidates CSV")
    candidate_csv_filename = fields.Char(string="CSV Filename")

    @api.depends("response_ids")
    def _compute_response_count(self):
        for rec in self:
            rec.response_count = len(rec.response_ids)

    @api.depends("category_id")
    def _compute_total_questions_available(self):
        for rec in self:
            if rec.category_id:
                rec.total_questions_available = self.env[
                    "etp.assessment.question"
                ].search_count(
                    [("category_id", "=", rec.category_id.id), ("active", "=", True)]
                )
            else:
                rec.total_questions_available = 0

    @api.constrains("question_limit", "category_id")
    def _check_question_limit(self):
        for rec in self:
            if rec.question_limit < 0:
                raise ValidationError("Number of Questions cannot be negative.")

    @api.constrains("start_date", "end_date")
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.start_date >= rec.end_date:
                raise ValidationError("End Date must be after Start Date.")

    def action_import_candidates_csv(self):
        self.ensure_one()
        if not self.candidate_csv_file:
            raise UserError("Please upload a CSV file first.")

        try:
            csv_data = base64.b64decode(self.candidate_csv_file)
            file_input = io.StringIO(csv_data.decode("utf-8"))
            reader = csv.DictReader(file_input)
        except Exception:
            raise UserError("Invalid CSV file. Please upload a valid UTF-8 CSV file.")

        required_fields = {"name", "email"}
        if not required_fields.issubset(set(reader.fieldnames or [])):
            raise UserError(
                "CSV must contain 'name' and 'email' columns. "
                f"Found columns: {', '.join(reader.fieldnames or [])}"
            )

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
                emp_vals = {
                    "name": name,
                    "work_email": email,
                }
                job_title = (row.get("job_title") or "").strip()
                department = (row.get("department") or "").strip()
                if job_title:
                    emp_vals["job_title"] = job_title
                if department:
                    dept = self.env["hr.department"].sudo().search(
                        [("name", "=", department)], limit=1
                    )
                    if dept:
                        emp_vals["department_id"] = dept.id
                employee = Employee.create(emp_vals)

            imported_ids.append(employee.id)

        if errors:
            raise UserError(
                f"CSV import completed with errors:\n" + "\n".join(errors)
            )

        existing_ids = self.evaluator_ids.ids
        new_ids = list(set(imported_ids) - set(existing_ids))
        if new_ids:
            self.write({
                "evaluator_ids": [(4, eid) for eid in new_ids],
            })

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

    @api.model
    def get_dashboard_data(self, filters=None):
        if not isinstance(filters, dict):
            filters = {}
        Assessment = self.env["etp.assessment"]
        Evaluator = self.env["etp.assessment.evaluator"]
        Question = self.env["etp.assessment.question"]
        Response = self.env["etp.assessment.response"]

        assessment_domain = []
        if filters.get("assessment_id"):
            assessment_domain.append(("id", "=", filters["assessment_id"]))
        if filters.get("state"):
            assessment_domain.append(("state", "=", filters["state"]))
        if filters.get("date_from"):
            assessment_domain.append(("start_date", ">=", filters["date_from"]))
        if filters.get("date_to"):
            assessment_domain.append(("end_date", "<=", filters["date_to"]))
        if filters.get("category_id"):
            assessment_domain.append(("category_id", "=", filters["category_id"]))

        has_filters = bool(assessment_domain)
        filtered_assessments = Assessment.search(assessment_domain)
        filtered_ids = filtered_assessments.ids

        ev_domain = [("assessment_id", "in", filtered_ids)] if has_filters else []
        resp_domain = [("assessment_id", "in", filtered_ids)] if has_filters else []

        total_assessments = len(filtered_assessments) if has_filters else Assessment.search_count([])
        draft_count = len(filtered_assessments.filtered(lambda a: a.state == "draft")) if has_filters else Assessment.search_count([("state", "=", "draft")])
        in_progress_count = len(filtered_assessments.filtered(lambda a: a.state == "in_progress")) if has_filters else Assessment.search_count([("state", "=", "in_progress")])
        done_count = len(filtered_assessments.filtered(lambda a: a.state == "done")) if has_filters else Assessment.search_count([("state", "=", "done")])
        cancelled_count = len(filtered_assessments.filtered(lambda a: a.state == "cancelled")) if has_filters else Assessment.search_count([("state", "=", "cancelled")])

        if has_filters:
            filtered_question_ids = filtered_assessments.mapped("question_ids").ids
            q_domain = [("id", "in", filtered_question_ids), ("active", "=", True)]
        else:
            filtered_question_ids = []
            q_domain = [("active", "=", True)]

        total_questions = Question.search_count(q_domain)
        total_evaluators = Evaluator.search_count(ev_domain)
        evaluators_pending = Evaluator.search_count(ev_domain + [("state", "=", "pending")])
        evaluators_in_progress = Evaluator.search_count(ev_domain + [("state", "=", "in_progress")])
        evaluators_submitted = Evaluator.search_count(ev_domain + [("state", "=", "submitted")])

        total_responses = Response.search_count(resp_domain)
        responses_submitted = Response.search_count(resp_domain + [("state", "=", "submitted")])
        responses_draft = Response.search_count(resp_domain + [("state", "=", "draft")])
        total_violators = Evaluator.search_count(ev_domain + [("is_violated", "=", True)])

        question_type_data = []
        for qtype in ["image_comparison", "text", "coding", "image_text", "video"]:
            count = Question.search_count(q_domain + [("question_type", "=", qtype)])
            question_type_data.append({"type": qtype, "count": count})

        category_data = []
        categories = self.env["etp.assessment.category"].search([("active", "=", True)])
        for cat in categories:
            q_count = Question.search_count(q_domain + [("category_id", "=", cat.id)])
            category_data.append({"id": cat.id, "name": cat.name, "count": q_count})

        if has_filters:
            active_domain = [("id", "in", filtered_ids)]
        else:
            active_domain = [("state", "=", "in_progress")]
        active_assessments = Assessment.search(active_domain, limit=10, order="start_date desc")
        active_work = []
        for a in active_assessments:
            ev_count = Evaluator.search_count([("assessment_id", "=", a.id)])
            ev_done = Evaluator.search_count([("assessment_id", "=", a.id), ("state", "=", "submitted")])
            active_work.append({
                "id": a.id,
                "name": a.name,
                "evaluators_total": ev_count,
                "evaluators_done": ev_done,
                "end_date": a.end_date.isoformat() if a.end_date else False,
            })

        evaluator_perf = []
        ev_perf_domain = ev_domain + [("state", "=", "submitted")]
        submitted_evaluators = Evaluator.search(ev_perf_domain, limit=20, order="total_score desc")
        for ev in submitted_evaluators:
            evaluator_perf.append({
                "id": ev.id,
                "name": ev.employee_id.name,
                "total_score": ev.total_score,
                "max_possible": ev.max_possible_score,
                "total_questions": ev.total_questions,
                "assessment_name": ev.assessment_id.name,
            })

        completion_rate = 0
        if total_evaluators > 0:
            completion_rate = round((evaluators_submitted / total_evaluators) * 100, 1)

        all_assessments = Assessment.search([], order="name")
        assessment_options = [{"id": a.id, "name": a.name} for a in all_assessments]

        return {
            "kpis": {
                "total_assessments": total_assessments,
                "draft": draft_count,
                "in_progress": in_progress_count,
                "done": done_count,
                "cancelled": cancelled_count,
                "total_questions": total_questions,
                "total_evaluators": total_evaluators,
                "evaluators_pending": evaluators_pending,
                "evaluators_in_progress": evaluators_in_progress,
                "evaluators_submitted": evaluators_submitted,
                "total_responses": total_responses,
                "responses_submitted": responses_submitted,
                "responses_draft": responses_draft,
                "completion_rate": completion_rate,
                "total_violators": total_violators,
            },
            "question_types": question_type_data,
            "categories": category_data,
            "active_work": active_work,
            "evaluator_performance": evaluator_perf,
            "assessment_options": assessment_options,
        }

    def action_start(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft assessments can be started.")
            if not rec.evaluator_ids:
                raise UserError("Please assign at least one candidate before starting.")
            if not rec.category_id:
                raise UserError("Please select a question category.")

            available_questions = self.env["etp.assessment.question"].search(
                [("category_id", "=", rec.category_id.id), ("active", "=", True)]
            )
            if not available_questions:
                raise UserError(
                    "No active questions found in the selected category."
                )

            limit = rec.question_limit or len(available_questions)
            if limit > len(available_questions):
                raise UserError(
                    f"Requested {limit} questions but only "
                    f"{len(available_questions)} available in this category."
                )

            all_question_ids = available_questions.ids
            rec.write({
                "question_ids": [(6, 0, all_question_ids)],
                "state": "in_progress",
                "start_date": rec.start_date or fields.Datetime.now(),
            })

            for evaluator in rec.evaluator_ids:
                candidate_questions = random.sample(all_question_ids, limit)
                random.shuffle(candidate_questions)
                self.env["etp.assessment.evaluator"].create({
                    "assessment_id": rec.id,
                    "employee_id": evaluator.id,
                    "question_order": json.dumps(candidate_questions),
                    "total_questions": len(candidate_questions),
                    "access_token": str(uuid.uuid4()),
                })

            rec._send_assessment_emails()

    def action_done(self):
        for rec in self:
            if rec.state != "in_progress":
                raise UserError("Only in-progress assessments can be marked done.")
        self.write({"state": "done"})

    def action_llm_score_all(self):
        """Enqueue subjective scoring for every submitted candidate.

        Per-question RabbitMQ tasks; broker-down falls back to the cron
        drainer. Re-runnable; never aborts the batch.
        """
        self.ensure_one()
        todo = self.assessment_evaluator_ids.filtered(
            lambda ev: ev.state == "submitted" and ev.llm_state in
            ("pending", "scoring", "partial", "failed")
        )
        if not todo:
            raise UserError(
                "No submitted candidates awaiting subjective scoring."
            )
        todo.action_llm_score()
        queued = sum(len(ev.response_ids.filtered(
            lambda r: r.llm_state in ("queued", "pending"))) for ev in todo)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Subjective Scoring Triggered",
                "message": f"{len(todo)} candidate(s), {queued} response(s) "
                           f"queued for LLM scoring.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _cron_llm_auto_score(self):
        """Drain pending per-response subjective scoring (no-broker fallback).

        Two jobs:
          1. Auto-enqueue submitted candidates on llm_auto_score assessments
             whose responses still need subjective scoring.
          2. Score any response left in 'pending' (broker was down at submit
             time, or manual trigger with no consumer running) directly,
             in small batches, so local dev works without RabbitMQ.
        """
        Resp = self.env["etp.assessment.response"]

        # (1) enqueue auto-score assessments still waiting
        evaluators = self.env["etp.assessment.evaluator"].search([
            ("state", "=", "submitted"),
            ("llm_state", "in", ("pending", "scoring")),
            ("assessment_id.llm_auto_score", "=", True),
        ], limit=20)
        for ev in evaluators:
            ev.response_ids.filtered(
                lambda r: r.needs_llm and r.llm_state in ("not_needed", "pending")
            )._enqueue_subjective_scoring()

        # (2) drain pending responses inline (the broker-less path)
        pending = Resp.search([
            ("llm_state", "=", "pending"),
            ("needs_llm", "=", True),
        ], limit=10)
        for resp in pending:
            try:
                resp.rmq_score_subjective()
            except Exception:
                _logger.exception(
                    "Cron subjective scoring failed for response %s", resp.id)
                # rmq_score_subjective already marked it failed
                continue

        # (3) RESCUE stale 'queued' responses — broker accepted the publish
        # but no consumer scored it within the grace window (consumer down,
        # or no broker setup at all). Score them inline so nothing gets stuck.
        from datetime import timedelta
        cutoff = fields.Datetime.now() - timedelta(minutes=5)
        stuck = Resp.search([
            ("llm_state", "=", "queued"),
            ("needs_llm", "=", True),
            ("write_date", "<", cutoff),
        ], limit=10)
        for resp in stuck:
            _logger.warning(
                "Rescuing stuck-queued response %s (no consumer scored it)",
                resp.id)
            try:
                resp.rmq_score_subjective()
            except Exception:
                _logger.exception(
                    "Cron rescue scoring failed for response %s", resp.id)
                continue

        # (4) BOUNDED RETRY of failed responses — a transient Vertex error
        # (429/5xx) shouldn't leave a candidate pending forever. Retry up to
        # the cap, then leave it failed for manual action (surfaced in UI).
        MAX_ATTEMPTS = 3
        failed = Resp.search([
            ("llm_state", "=", "failed"),
            ("needs_llm", "=", True),
            ("llm_attempts", "<", MAX_ATTEMPTS),
        ], limit=10)
        for resp in failed:
            _logger.info(
                "Retrying failed subjective scoring for response %s "
                "(attempt %s/%s)", resp.id, resp.llm_attempts + 1, MAX_ATTEMPTS)
            try:
                resp.rmq_score_subjective()
            except Exception:
                _logger.exception(
                    "Cron retry scoring failed for response %s", resp.id)
                continue

    def action_cancel(self):
        for rec in self:
            if rec.state in ("done", "cancelled"):
                raise UserError("Cannot cancel a completed or already cancelled assessment.")
        self.write({"state": "cancelled"})

    def action_reset_draft(self):
        for rec in self:
            if rec.state != "cancelled":
                raise UserError("Only cancelled assessments can be reset to draft.")
            rec.assessment_evaluator_ids.unlink()
            rec.response_ids.unlink()
            rec.write({"question_ids": [(5, 0, 0)]})
        self.write({"state": "draft"})

    def _send_assessment_emails(self):
        template = self.env.ref(
            "etp_assessment.email_assessment_invitation", raise_if_not_found=False
        )
        if not template:
            _logger.warning(
                "Assessment email template 'etp_assessment.email_assessment_invitation' not found. "
                "Please upgrade the module or create the template manually."
            )
            return

        _logger.info(
            "Sending assessment emails for '%s' to %d candidates",
            self.name, len(self.assessment_evaluator_ids)
        )

        for assignment in self.assessment_evaluator_ids:
            emp = assignment.employee_id
            recipient_email = emp.work_email or emp.private_email or (emp.user_id.email if emp.user_id else False)
            if not recipient_email:
                _logger.warning(
                    "Skipping email for candidate '%s' - no email found (work_email, private_email, user email all empty).",
                    emp.name
                )
                continue
            try:
                template.send_mail(
                    assignment.id,
                    force_send=True,
                    raise_exception=True,
                    email_values={
                        'email_to': recipient_email,
                    },
                )
                _logger.info(
                    "Assessment email sent to %s (token: %s)",
                    recipient_email, assignment.access_token
                )
            except Exception as e:
                _logger.error(
                    "Failed to send assessment email to %s: %s",
                    recipient_email, str(e)
                )


class EtpAssessmentEvaluator(models.Model):
    _name = "etp.assessment.evaluator"
    _description = "Assessment Candidate Assignment"
    _order = "create_date desc"
    _rec_name = "employee_id"

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade"
    )
    employee_id = fields.Many2one("hr.employee", string="Candidate", required=True, ondelete="restrict")
    access_token = fields.Char(
        string="Access Token", index=True, copy=False,
        default=lambda self: str(uuid.uuid4()),
    )
    question_order = fields.Text(string="Shuffled Question Order (JSON)")
    started_at = fields.Datetime(string="Started At", help="When the candidate first opened the assessment")
    deadline_datetime = fields.Datetime(
        string="Candidate Deadline", compute="_compute_deadline_datetime", store=True
    )
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
        compute="_compute_progress", store=True, string="Answered"
    )
    total_score = fields.Integer(
        compute="_compute_progress", store=True, string="Total Score"
    )
    max_possible_score = fields.Integer(
        compute="_compute_progress", store=True, string="Max Possible Score"
    )
    response_ids = fields.One2many(
        "etp.assessment.response", "assessment_evaluator_id", string="Responses"
    )
    is_locked = fields.Boolean(default=False, string="Locked")
    violation_reason = fields.Char(string="Violation Reason", readonly=True)
    violation_datetime = fields.Datetime(string="Violation Time", readonly=True)
    is_violated = fields.Boolean(default=False, string="Violated", readonly=True)

    # ------------------------------------------------------------------
    # OBJECTIVE / SUBJECTIVE rollups.
    # objective = sum of dimension-pick scores (instant, no LLM)
    # subjective = sum of per-question LLM scores (justification grading)
    # ------------------------------------------------------------------
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
        related="total_score", store=True, string="Objective Total", readonly=True
    )
    objective_max_total = fields.Integer(
        related="max_possible_score", store=True,
        string="Objective Max", readonly=True
    )
    llm_total_score = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Total"
    )
    llm_max_score = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Max"
    )
    subjective_pending = fields.Integer(
        compute="_compute_llm_progress", store=True, string="Subjective Pending"
    )
    llm_scored_at = fields.Datetime(string="Subjective Scored At", readonly=True)
    llm_error = fields.Char(string="Subjective Error", readonly=True)

    # ------------------------------------------------------------------
    # Combined score % + PASS/FAIL against the org pass threshold.
    # Threshold lives in System Parameter etp_assessment.pass_threshold
    # (percent, default 70). Combines objective + subjective into one
    # percentage; result is pending until all subjective scoring lands.
    # ------------------------------------------------------------------
    score_percent = fields.Float(
        string="Score %", compute="_compute_result", store=True,
        help="(objective + subjective earned) / (objective + subjective max) "
             "× 100, across all submitted responses.")
    pass_threshold = fields.Float(
        string="Pass Threshold %", compute="_compute_result", store=True,
        help="Org pass threshold from System Parameter "
             "etp_assessment.pass_threshold (default 70).")
    result = fields.Selection(
        [("pending", "Pending"), ("pass", "Pass"), ("fail", "Fail")],
        string="Result", compute="_compute_result", store=True, default="pending")

    @api.depends("total_score", "max_possible_score",
                 "llm_total_score", "llm_max_score", "subjective_pending",
                 "state", "response_ids.llm_state")
    def _compute_result(self):
        threshold = self._get_pass_threshold()
        for rec in self:
            rec.pass_threshold = threshold
            earned = (rec.total_score or 0) + (rec.llm_total_score or 0)
            possible = (rec.max_possible_score or 0) + (rec.llm_max_score or 0)
            rec.score_percent = round(
                (earned / possible) * 100.0, 2) if possible else 0.0
            # result is only meaningful once submitted AND no subjective work
            # is still pending (so we never fail someone mid-scoring)
            if rec.state != "submitted" or rec.subjective_pending:
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

    @api.depends("response_ids.llm_score", "response_ids.llm_max_score",
                 "response_ids.llm_state")
    def _compute_llm_progress(self):
        for rec in self:
            scored = rec.response_ids.filtered(
                lambda r: r.llm_state == "scored")
            rec.llm_total_score = sum(scored.mapped("llm_score"))
            rec.llm_max_score = sum(scored.mapped("llm_max_score"))
            rec.subjective_pending = len(rec.response_ids.filtered(
                lambda r: r.needs_llm
                and r.llm_state in ("pending", "queued", "failed")))

    def _compute_subjective_rollup(self):
        """Refresh the evaluator subjective state after a per-response score."""
        for rec in self:
            resp = rec.response_ids
            need = resp.filtered(lambda r: r.needs_llm)
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
        """Trigger subjective (LLM) scoring for this candidate's responses.

        Enqueues ONE RabbitMQ task PER justification-bearing response.
        Idempotent and re-runnable; re-queues pending/failed responses.
        """
        for rec in self:
            if rec.state != "submitted":
                raise UserError(
                    "Candidate '%s' has not submitted yet — scoring runs on "
                    "submitted assessments only." % rec.employee_id.name
                )
            rec.write({"llm_state": "scoring", "llm_error": False})
            todo = rec.response_ids.filtered(
                lambda r: r.needs_llm and r.llm_state in
                ("not_needed", "pending", "queued", "failed"))
            todo._enqueue_subjective_scoring()
            rec._compute_subjective_rollup()
        return True

    @api.depends("response_ids", "response_ids.state", "response_ids.score")
    def _compute_progress(self):
        for rec in self:
            submitted_responses = rec.response_ids.filtered(
                lambda r: r.state == "submitted"
            )
            rec.answered_count = len(submitted_responses)
            rec.total_score = sum(submitted_responses.mapped("score"))
            rec.max_possible_score = sum(
                submitted_responses.mapped("max_score")
            )

    @api.depends("started_at", "assessment_id.duration_minutes")
    def _compute_deadline_datetime(self):
        from datetime import timedelta
        for rec in self:
            if rec.started_at and rec.assessment_id.duration_minutes > 0:
                rec.deadline_datetime = rec.started_at + timedelta(
                    minutes=rec.assessment_id.duration_minutes
                )
            else:
                rec.deadline_datetime = False

    def is_time_expired(self):
        """Check if time has expired for this candidate."""
        self.ensure_one()
        if not self.deadline_datetime:
            return False
        return fields.Datetime.now() > self.deadline_datetime


class EtpAssessmentResponse(models.Model):
    _name = "etp.assessment.response"
    _description = "Assessment Response"
    _order = "create_date desc"

    assessment_id = fields.Many2one(
        "etp.assessment", required=True, ondelete="cascade"
    )
    assessment_evaluator_id = fields.Many2one(
        "etp.assessment.evaluator", string="Candidate Assignment", ondelete="cascade"
    )
    evaluator_id = fields.Many2one("hr.employee", string="Candidate", required=True)
    question_id = fields.Many2one(
        "etp.assessment.question", required=True, ondelete="cascade"
    )
    justification = fields.Text()
    line_ids = fields.One2many(
        "etp.assessment.response.line", "response_id", string="Dimension Answers"
    )
    state = fields.Selection(
        [("draft", "Draft"), ("submitted", "Submitted")],
        default="draft",
    )
    score = fields.Integer(compute="_compute_score", store=True, string="Score")
    max_score = fields.Integer(
        compute="_compute_score", store=True, string="Max Possible"
    )

    # ------------------------------------------------------------------
    # OBJECTIVE scoring: dimension picks vs the defined correct option.
    # Known instantly, no LLM. (objective_* mirror score/max_score, with
    # the clearer name the PL screens use.)
    # ------------------------------------------------------------------
    objective_score = fields.Integer(
        related="score", store=True, string="Objective Score", readonly=True
    )
    objective_max = fields.Integer(
        related="max_score", store=True, string="Objective Max", readonly=True
    )
    has_objective = fields.Boolean(
        compute="_compute_scoring_kind", store=True,
        string="Has Objective", help="Question has at least one dimension "
        "with a defined correct option.")

    # ------------------------------------------------------------------
    # SUBJECTIVE scoring: the written justification, graded by the LLM.
    # Only runs when a justification is present (needs_llm). Stored in the
    # llm_* fields (kept for backward compat); subjective_* are friendly
    # aliases the UI/API use.
    # ------------------------------------------------------------------
    needs_llm = fields.Boolean(
        compute="_compute_scoring_kind", store=True, string="Needs LLM",
        help="True when the candidate provided a written justification that "
             "requires subjective grading.")
    llm_score = fields.Integer(string="Subjective Score", readonly=True, copy=False)
    llm_max_score = fields.Integer(string="Subjective Max", readonly=True, copy=False)
    llm_passed = fields.Boolean(
        string="Subjective Passed", readonly=True, copy=False,
        help="True when the LLM's 0..1 score met the subjective threshold. "
             "Pass = full subjective points for this question, Fail = 0.")
    llm_raw_score = fields.Float(
        string="Subjective Raw Score (0-1)", readonly=True, copy=False,
        help="The LLM's raw 0..1 quality score for this justification, "
             "before the pass/fail threshold is applied.")
    llm_feedback = fields.Text(string="Subjective Reasoning", readonly=True, copy=False)
    llm_attempts = fields.Integer(
        string="Subjective Scoring Attempts", default=0, copy=False,
        help="Number of LLM scoring attempts; the cron stops auto-retrying "
             "failed responses after the cap so a hard error doesn't loop.")
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
        store=True,
        help="Per-question subjective verdict: PASS = full points, FAIL = 0.")
    subjective_reasoning = fields.Text(
        related="llm_feedback", string="Subjective Reasoning ", readonly=True)

    @api.depends("llm_state", "llm_passed", "needs_llm")
    def _compute_subjective_result(self):
        for rec in self:
            if rec.needs_llm and rec.llm_state == "scored":
                rec.subjective_result = "pass" if rec.llm_passed else "fail"
            else:
                rec.subjective_result = False

    @api.depends("line_ids.selected_option_id", "justification",
                 "question_id")
    def _compute_scoring_kind(self):
        Opt = self.env["etp.assessment.question.dimension.option"]
        for rec in self:
            has_obj = bool(Opt.search_count([
                ("question_dimension_id.question_id", "=", rec.question_id.id),
                ("is_correct", "=", True),
            ]))
            rec.has_objective = has_obj
            just = (rec.justification or "").strip()
            # Auto-submit placeholders are NOT real answers — never LLM-score
            # them. needs_llm is the single source of truth for the enqueue
            # filters, so excluding the markers here defeats re-enqueue.
            is_placeholder = just.startswith("[Auto-submitted")
            rec.needs_llm = bool(just) and not is_placeholder

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_score(self):
        for rec in self:
            score = 0
            Opt = self.env["etp.assessment.question.dimension.option"]
            # max is over the QUESTION's objective dimensions (those with a
            # defined correct option), NOT just the ones the candidate
            # answered — skipping a dimension must not inflate the percentage.
            objective_dims = rec.question_id.question_dimension_ids.filtered(
                lambda qd: qd.option_line_ids.filtered("is_correct"))
            max_score = len(objective_dims)
            answered = {l.dimension_id.id: l.selected_option_id
                        for l in rec.line_ids if l.selected_option_id}
            for qd in objective_dims:
                correct_opt = qd.option_line_ids.filtered("is_correct")[:1]
                picked = answered.get(qd.dimension_id.id)
                if (correct_opt and picked
                        and picked.id == correct_opt.master_option_id.id):
                    score += 1
            rec.score = score
            rec.max_score = max_score

    def action_submit(self):
        for rec in self:
            if rec.state == "submitted":
                raise UserError("This response is already submitted.")
            if rec.assessment_evaluator_id and rec.assessment_evaluator_id.is_locked:
                raise UserError(
                    "This assessment is already locked. Cannot modify responses."
                )
            if not rec.line_ids:
                raise UserError(
                    "Please answer at least one dimension before submitting."
                )
            rec.write({"state": "submitted"})

            # Subjective scoring: enqueue per-question if a justification
            # was provided. Objective score is already computed instantly.
            if (rec.justification or "").strip():
                rec.with_context(autoscore=True)._enqueue_subjective_scoring()

            if rec.assessment_evaluator_id:
                rec._check_all_submitted()

    # ------------------------------------------------------------------
    # SUBJECTIVE (LLM) scoring — per question, RabbitMQ-driven
    # ------------------------------------------------------------------
    def _enqueue_subjective_scoring(self):
        """Publish a per-question scoring task to RabbitMQ.

        Honors the assessment's llm_auto_score flag for the on-submit path
        (manual/bulk triggers bypass the flag). Falls back to inline state
        'pending' (cron drainer picks it up) when the broker is unreachable.
        """
        for rec in self:
            if not (rec.justification or "").strip():
                rec.llm_state = "not_needed"
                continue
            if self.env.context.get("autoscore") and \
                    not rec.assessment_id.llm_auto_score:
                # auto path but assessment opted out — leave as pending,
                # the PL triggers it manually later
                rec.llm_state = "pending"
                continue
            rec.llm_state = "queued"
            try:
                from ..services import rabbitmq_service
                rabbitmq_service.publish_score_task(rec.id)
            except Exception as exc:
                # broker down (e.g. local dev) — drop to pending so the
                # cron drainer / manual button can score it
                _logger.warning(
                    "Score task publish failed for response %s, "
                    "falling back to pending: %s", rec.id, exc)
                rec.llm_state = "pending"

    def rmq_score_subjective(self):
        """Entry point called by the RabbitMQ consumer (per response).

        Scores ONLY this response's justification. Idempotent and
        re-runnable. Returns a small status dict for the consumer log.
        """
        self.ensure_one()
        from ..services import bedrock_scoring
        if not (self.justification or "").strip():
            self.llm_state = "not_needed"
            return {"status": "skipped", "reason": "no justification"}
        # count the attempt up-front so the bounded-retry cron can cap it
        self.llm_attempts = (self.llm_attempts or 0) + 1
        try:
            result = bedrock_scoring.score_one_response(self.env, self)
        except Exception as exc:
            _logger.exception("Subjective scoring failed for response %s", self.id)
            self.write({"llm_state": "failed",
                        "llm_feedback": ("ERROR: " + str(exc))[:1000]})
            raise
        # The LLM returns a 0..1 quality score; WE apply the configurable
        # subjective threshold to decide PASS/FAIL. PASS = full subjective
        # points for this question, FAIL = 0. Both knobs live in Settings.
        points = self._subjective_points()
        score01 = float(result.get("score01") or 0.0)
        threshold = self._subjective_pass_threshold()
        passed = score01 >= threshold
        self.write({
            "llm_passed": passed,
            "llm_raw_score": score01,
            "llm_score": points if passed else 0,
            "llm_max_score": points,
            "llm_feedback": result.get("feedback", ""),
            "llm_state": "scored",
        })
        if self.assessment_evaluator_id:
            self.assessment_evaluator_id._compute_subjective_rollup()
        return {"status": "scored", "passed": passed}

    @api.model
    def _subjective_points(self):
        """Points a subjective question is worth (full on PASS, 0 on FAIL)."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.subjective_points", "10")
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = 10
        return val if val > 0 else 10

    @api.model
    def _subjective_pass_threshold(self):
        """0..1 cutoff: LLM score >= this => the subjective question PASSes.

        Configurable in Settings (etp_assessment.subjective_pass_threshold,
        default 0.7). Accepts either a 0..1 fraction or a 0..100 percent.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.subjective_pass_threshold", "0.7")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return 0.7
        if val > 1.0:          # entered as a percent (e.g. 70)
            val = val / 100.0
        return val if 0.0 <= val <= 1.0 else 0.7

    def _check_all_submitted(self):
        evaluator_assignment = self.assessment_evaluator_id
        total_expected = evaluator_assignment.total_questions
        submitted_count = self.env["etp.assessment.response"].search_count([
            ("assessment_evaluator_id", "=", evaluator_assignment.id),
            ("state", "=", "submitted"),
        ])
        if submitted_count >= total_expected:
            evaluator_assignment.write({
                "state": "submitted",
                "is_locked": True,
            })
            self._check_assessment_complete()

    def _check_assessment_complete(self):
        assessment = self.assessment_id
        all_assignments = assessment.assessment_evaluator_ids
        if all_assignments and all(a.state == "submitted" for a in all_assignments):
            assessment.write({"state": "done"})

    @api.constrains("state")
    def _check_locked(self):
        for rec in self:
            if (
                rec.assessment_evaluator_id
                and rec.assessment_evaluator_id.is_locked
                and rec.state != "submitted"
            ):
                raise ValidationError(
                    "Cannot modify responses after assessment is submitted and locked."
                )


class EtpAssessmentResponseLine(models.Model):
    _name = "etp.assessment.response.line"
    _description = "Assessment Response Line"

    response_id = fields.Many2one(
        "etp.assessment.response", required=True, ondelete="cascade"
    )
    dimension_id = fields.Many2one(
        "etp.assessment.dimension", required=True, ondelete="restrict"
    )
    selected_option_id = fields.Many2one(
        "etp.assessment.dimension.option",
        string="Selected Option",
        ondelete="restrict",
    )
