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
    def get_dashboard_data(self):
        Assessment = self.env["etp.assessment"]
        Evaluator = self.env["etp.assessment.evaluator"]
        Question = self.env["etp.assessment.question"]
        Response = self.env["etp.assessment.response"]
        Dimension = self.env["etp.assessment.dimension"]

        total_assessments = Assessment.search_count([])
        draft_count = Assessment.search_count([("state", "=", "draft")])
        in_progress_count = Assessment.search_count([("state", "=", "in_progress")])
        done_count = Assessment.search_count([("state", "=", "done")])
        cancelled_count = Assessment.search_count([("state", "=", "cancelled")])

        total_questions = Question.search_count([("active", "=", True)])
        total_evaluators = Evaluator.search_count([])
        evaluators_pending = Evaluator.search_count([("state", "=", "pending")])
        evaluators_in_progress = Evaluator.search_count([("state", "=", "in_progress")])
        evaluators_submitted = Evaluator.search_count([("state", "=", "submitted")])

        total_responses = Response.search_count([])
        responses_submitted = Response.search_count([("state", "=", "submitted")])
        responses_draft = Response.search_count([("state", "=", "draft")])
        total_violators = Evaluator.search_count([("is_violated", "=", True)])

        question_type_data = []
        for qtype in ["image_comparison", "text", "coding", "image_text", "video"]:
            count = Question.search_count([("question_type", "=", qtype), ("active", "=", True)])
            question_type_data.append({"type": qtype, "count": count})

        category_data = []
        categories = self.env["etp.assessment.category"].search([("active", "=", True)])
        for cat in categories:
            q_count = Question.search_count([("category_id", "=", cat.id), ("active", "=", True)])
            category_data.append({"name": cat.name, "count": q_count})

        active_assessments = Assessment.search(
            [("state", "=", "in_progress")], limit=10, order="start_date desc"
        )
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
        submitted_evaluators = Evaluator.search(
            [("state", "=", "submitted")], limit=20, order="total_score desc"
        )
        for ev in submitted_evaluators:
            evaluator_perf.append({
                "id": ev.id,
                "name": ev.employee_id.name,
                "total_score": ev.total_score,
                "max_possible": ev.max_possible_score,
                "total_questions": ev.total_questions,
                "assessment_name": ev.assessment_id.name,
            })

        dimension_stats = []
        dimensions = Dimension.search([("active", "=", True)])
        QuestionDimOption = self.env["etp.assessment.question.dimension.option"]
        for dim in dimensions:
            lines = self.env["etp.assessment.response.line"].search([
                ("dimension_id", "=", dim.id),
                ("selected_option_id", "!=", False),
                ("response_id.state", "=", "submitted"),
            ])
            if lines:
                correct_count = 0
                for line in lines:
                    correct_opt = QuestionDimOption.search([
                        ("question_dimension_id.question_id", "=", line.response_id.question_id.id),
                        ("question_dimension_id.dimension_id", "=", dim.id),
                        ("is_correct", "=", True),
                    ], limit=1)
                    if correct_opt and line.selected_option_id.id == correct_opt.master_option_id.id:
                        correct_count += 1
                dimension_stats.append({
                    "name": dim.name,
                    "response_count": len(lines),
                    "correct_count": correct_count,
                    "incorrect_count": len(lines) - correct_count,
                    "accuracy": round((correct_count / len(lines)) * 100, 1) if lines else 0,
                })

        completion_rate = 0
        if total_evaluators > 0:
            completion_rate = round((evaluators_submitted / total_evaluators) * 100, 1)

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
            "dimension_stats": dimension_stats,
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

            selected = random.sample(available_questions.ids, limit)
            rec.write({
                "question_ids": [(6, 0, selected)],
                "state": "in_progress",
                "start_date": rec.start_date or fields.Datetime.now(),
            })

            for evaluator in rec.evaluator_ids:
                shuffled_order = selected[:]
                random.shuffle(shuffled_order)
                self.env["etp.assessment.evaluator"].create({
                    "assessment_id": rec.id,
                    "employee_id": evaluator.id,
                    "question_order": json.dumps(shuffled_order),
                    "total_questions": len(shuffled_order),
                    "access_token": str(uuid.uuid4()),
                })

            rec._send_assessment_emails()

    def action_done(self):
        for rec in self:
            if rec.state != "in_progress":
                raise UserError("Only in-progress assessments can be marked done.")
        self.write({"state": "done"})

    def action_cancel(self):
        for rec in self:
            if rec.state in ("done", "cancelled"):
                raise UserError("Cannot cancel a completed or already cancelled assessment.")
        self.write({"state": "cancelled"})

    def action_reset_draft(self):
        for rec in self:
            if rec.state != "cancelled":
                raise UserError("Only cancelled assessments can be reset to draft.")
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
    access_token = fields.Char(string="Access Token", index=True, copy=False)
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

    @api.depends("line_ids.selected_option_id")
    def _compute_score(self):
        for rec in self:
            score = 0
            max_score = len(rec.line_ids)
            for line in rec.line_ids:
                if not line.selected_option_id:
                    continue
                correct_opt = self.env["etp.assessment.question.dimension.option"].search([
                    ("question_dimension_id.question_id", "=", rec.question_id.id),
                    ("question_dimension_id.dimension_id", "=", line.dimension_id.id),
                    ("is_correct", "=", True),
                ], limit=1)
                if correct_opt and line.selected_option_id.id == correct_opt.master_option_id.id:
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

            if rec.assessment_evaluator_id:
                rec._check_all_submitted()

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
