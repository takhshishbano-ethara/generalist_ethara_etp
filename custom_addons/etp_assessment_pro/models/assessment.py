# -*- coding: utf-8 -*-
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

from ..constants import (
    DEFAULT_SUBJECTIVE_THRESHOLD,
    AB_VERDICT_WEIGHT,
    AB_JUSTIFICATION_WEIGHT,
    ADVISORY_LOCK_AUTOSCORE,
    ADVISORY_LOCK_AUTOSCORE_SHARD_BASE,
    ADVISORY_LOCK_EXPIRE_ATTEMPTS,
    MAX_SCORING_SHARDS,
    is_integrity_gate,
)

from psycopg2 import IntegrityError

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

    generator_id = fields.Many2one(
        "etp.assessment.pro.prompt",
        string="Question Generator",
        ondelete="restrict",
    )
    question_limit = fields.Integer(
        string="Number of Questions",
        help="Number of questions to pick from the generator. 0 = all questions.",
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

    results_release = fields.Selection(
        [
            ("manual", "Manual (admin releases)"),
        ],
        default="manual",
        required=True,
        string="Results Release",
        help="Candidate-facing results stay hidden until an admin explicitly "
             "releases them (per evaluator via Release Results). This is the "
             "only release mode: results are never auto-revealed.",
    )

    subjective_threshold = fields.Float(
        string="Subjective Pass Threshold (%)",
        default=DEFAULT_SUBJECTIVE_THRESHOLD,
        help="Pass bar (0-100) for this assessment: an LLM-graded answer earns "
             "its mark when its raw 0-100 score is >= this, and a candidate "
             "passes when their overall score is >= this. Editable anytime "
             "(even after Done); changing it re-decides Pass/Fail without "
             "re-running the LLM (small assessments update on save; large ones "
             "within a minute via the recompute cron).")
    threshold_recompute_pending = fields.Boolean(default=False, copy=False)

    # copy=False required: a copied (now-past) schedule trips _check_schedule_dates.
    start_date = fields.Datetime(string="Start Date", copy=False)
    end_date = fields.Datetime(string="End Date", copy=False)

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
    submitted_count = fields.Integer(
        string="Submitted", compute="_compute_release_state",
        help="Candidates who have submitted their attempt.")
    awaiting_scoring_count = fields.Integer(
        string="Awaiting Subjective Scoring", compute="_compute_release_state",
        help="Submitted candidates with subjective answers not yet graded.")
    releasable_count = fields.Integer(
        string="Releasable", compute="_compute_release_state",
        help="Submitted candidates whose results are not yet released.")
    invite_summary = fields.Char(
        compute="_compute_invite_summary", string="Invitations",
        help="Live status of the background invitation-send job.")

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
        string="Watermark Content", default=True,
        help="Overlay the candidate's identity (name + email) faintly across "
             "the exam page to deter screenshots/photos of the questions.")
    rule_fullscreen = fields.Boolean(
        string="Require Fullscreen", default=True,
        help="Prompt the candidate into fullscreen when the exam opens; "
             "leaving fullscreen raises a proctoring violation.")
    rule_webcam = fields.Boolean(
        string="Require Webcam", default=False,
        help="Ask for webcam access when the exam opens; denying or losing "
             "the camera raises a proctoring violation.")
    max_violations = fields.Integer(
        string="Violations Allowed Before Auto-Submit", default=0,
        help="Auto-submit the exam once this many violations are reached "
             "(when On Violation = Auto-submit). 0 and 1 both end the exam on "
             "the first violation; 2 allows one then ends on the second, and so "
             "on. To never auto-submit, set On Violation = Log only.",
    )
    violation_action = fields.Selection(
        [("auto_submit", "Auto-submit assessment"),
         ("log_only", "Log violation only")],
        string="On Violation", default="auto_submit", required=True,
        help="Auto-submit assessment = end the exam once Violations Allowed is "
             "reached. Log violation only = record every violation but never "
             "end the exam (no cap).",
    )
    require_objective_justification = fields.Boolean(
        string="Require Justification on Objective Questions", default=False,
        help="When on, MCQ/MSQ questions also show a justification box the "
             "candidate must fill. Off (default) keeps objective questions "
             "clean - no confusing optional box.",
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
             "grader (still batched in 20s by the cron - never scored inline "
             "during the exam). Off (default): an admin must click 'Run "
             "Subjective Evaluation'.")

    candidate_csv_file = fields.Binary(string="Upload Candidates CSV")
    candidate_csv_filename = fields.Char(string="CSV Filename")

    def write(self, vals):
        res = super().write(vals)
        if "require_justification_image_comparison" in vals:
            stale = self.mapped(
                "assessment_evaluator_ids.response_ids").filtered(
                lambda r: r.question_id.question_type == "image_ab"
                and r.state == "submitted")
            if stale:
                stale._enqueue_subjective_scoring()
        if "subjective_threshold" in vals:
            for a in self:
                if not a.assessment_evaluator_ids:
                    continue
                if len(a.response_ids.filtered("needs_llm")) <= 500:
                    a._recompute_subjective_results()
                else:
                    a.threshold_recompute_pending = True
        return res

    def _recompute_subjective_results(self, batch=500, commit=False):
        # Must stay batched: one transaction over all responses locks the whole
        # response table; commit=True releases locks between batches.
        subj_fields = ["llm_raw_score", "llm_passed", "llm_score", "llm_max_score"]
        for a in self:
            responses = a.response_ids.filtered("needs_llm")
            for i in range(0, len(responses), batch):
                chunk = responses[i:i + batch]
                chunk.invalidate_recordset(subj_fields)
                chunk._compute_subjective_marks()
                chunk.flush_recordset(subj_fields)
                if commit:
                    self.env.cr.commit()
            evs = a.assessment_evaluator_ids
            evs.invalidate_recordset(
                ["llm_total_score", "subjective_pending", "score_percent",
                 "pass_threshold", "result"])
            evs._compute_llm_progress()
            evs._compute_result()
            evs.flush_recordset()
            if a.threshold_recompute_pending:
                a.threshold_recompute_pending = False
            if commit:
                self.env.cr.commit()

    @api.model
    def _cron_recompute_subjective_results(self, limit=20):
        pending = self.search(
            [("threshold_recompute_pending", "=", True)], limit=limit)
        for a in pending:
            try:
                a._recompute_subjective_results(commit=True)
            except Exception:
                _logger.exception(
                    "Threshold recompute failed for assessment %s", a.id)

    @api.depends("response_ids")
    def _compute_response_count(self):
        for rec in self:
            rec.response_count = len(rec.response_ids)

    @api.depends(
        "assessment_evaluator_ids.state",
        "assessment_evaluator_ids.results_released",
        "assessment_evaluator_ids.response_ids.needs_llm",
        "assessment_evaluator_ids.response_ids.llm_state")
    def _compute_release_state(self):
        for rec in self:
            submitted = rec.assessment_evaluator_ids.filtered(
                lambda e: e.state == "submitted")
            rec.submitted_count = len(submitted)
            rec.releasable_count = len(
                submitted.filtered(lambda e: not e.results_released))
            rec.awaiting_scoring_count = len(submitted.filtered(
                lambda e: any(
                    r.needs_llm and r.llm_state != "scored"
                    for r in e.response_ids)))

    @api.depends("assessment_evaluator_ids.invite_state")
    def _compute_invite_summary(self):
        for rec in self:
            evs = rec.assessment_evaluator_ids
            sent = len(evs.filtered(lambda e: e.invite_state == "sent"))
            queued = len(evs.filtered(lambda e: e.invite_state == "queued"))
            failed = len(evs.filtered(lambda e: e.invite_state == "failed"))
            parts = []
            if queued:
                parts.append("%d queued" % queued)
            if sent:
                parts.append("%d sent" % sent)
            if failed:
                parts.append("%d failed" % failed)
            rec.invite_summary = "  ·  ".join(parts)

    def action_requeue_all_invitations(self):
        self.ensure_one()
        self.assessment_evaluator_ids.action_requeue_invitation()

    @api.depends("generator_id")
    def _compute_total_questions_available(self):
        Question = self.env["etp.assessment.pro.question"]
        for rec in self:
            if rec.generator_id:
                rec.total_questions_available = Question.search_count([
                    ("generator_id", "=", rec.generator_id.id),
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

    def action_start(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft assessments can be started.")
            if not rec.evaluator_ids:
                raise UserError("Please assign at least one candidate before starting.")
            if not rec.generator_id:
                raise UserError("Please select a question generator.")

            available_questions = self.env["etp.assessment.pro.question"].search([
                ("generator_id", "=", rec.generator_id.id), ("active", "=", True),
            ]).filtered(
                lambda q: q._has_required_images() and q._has_required_videos())
            if not available_questions:
                raise UserError(
                    "No active questions found for the selected generator.")

            limit = rec.question_limit or len(available_questions)
            if limit > len(available_questions):
                raise UserError(
                    f"Requested {limit} questions but only "
                    f"{len(available_questions)} available in this generator.")

            all_question_ids = available_questions.ids
            rec.write({
                "question_ids": [(6, 0, all_question_ids)],
                "state": "in_progress",
                "start_date": rec.start_date or fields.Datetime.now(),
            })

            for evaluator in rec.evaluator_ids:
                rec._ensure_candidate_user(evaluator)
                candidate_questions = random.sample(all_question_ids, limit)
                random.shuffle(candidate_questions)
                ev = self.env["etp.assessment.pro.evaluator"].create({
                    "assessment_id": rec.id,
                    "applicant_id": evaluator.id,
                    "question_order": json.dumps(candidate_questions),
                    "access_token": str(uuid.uuid4()),
                    "invite_state": "queued",
                })

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
            rec.write({"question_ids": [(5, 0, 0)],
                       "start_date": False, "end_date": False})
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
        applicant = applicant.sudo()
        if applicant.candidate_user_id:
            return "exists"
        email = (applicant.email_from or "").strip()
        if not email:
            return "skipped"
        Users = self.env["res.users"].sudo()
        user = Users.with_context(active_test=False).search(
            [("login", "=ilike", email)], limit=1)
        if user and user._is_internal():
            _logger.warning(
                "Candidate %s email %s matches an internal user (id=%s); not "
                "binding - resolve manually.", applicant.partner_name, email, user.id)
            return "skipped"
        created = False
        if not user:
            company = (
                self.env.ref("base.main_company", raise_if_not_found=False)
                or self.env["res.company"].search([], limit=1, order="id asc")
            )
            portal = self.env.ref("base.group_portal")
            user = Users.with_company(company).with_context(
                no_reset_password=True,
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
            ).create({
                "name": applicant.partner_name or email,
                "login": email,
                "email": email,
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
                "group_ids": [(6, 0, [portal.id])],
            })
            created = True
        if not created and not user.active:
            _logger.warning(
                "Candidate %s email %s matches a DEACTIVATED portal user "
                "(id=%s); linking but NOT reactivating - enable it manually if "
                "this candidate should sit the exam.",
                applicant.partner_name, email, user.id)
        applicant.candidate_user_id = user.id
        if not applicant.partner_id and user.partner_id:
            applicant.partner_id = user.partner_id.id
        return "created" if created else "linked"

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

    def action_llm_score_all(self):
        self.ensure_one()
        todo = self.assessment_evaluator_ids.filtered(
            lambda ev: ev.state == "submitted" and any(
                r.needs_llm and r.llm_state != "scored" for r in ev.response_ids))
        if not todo:
            raise UserError("No submitted candidates awaiting subjective scoring.")
        todo.write({"scoring_requested": True, "llm_state": "pending"})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Subjective Grading Queued",
                "message": f"Queued {len(todo)} candidate(s). The background "
                           f"grader processes about 20 at a time - refresh to "
                           f"watch the LLM status advance.",
                "type": "success",
                "sticky": False,
            },
        }

    def action_release_all_results(self):
        self.ensure_one()
        to_release = self.assessment_evaluator_ids.filtered(
            lambda r: r.state == "submitted" and not r.results_released)
        if not to_release:
            raise UserError(
                "No submitted candidates awaiting release. Results are only "
                "releasable once a candidate has submitted their attempt.")
        unscored = to_release.filtered(lambda e: any(
            r.needs_llm and r.llm_state != "scored" for r in e.response_ids))
        to_release.write({"results_released": True})
        msg = "Released results for %s candidate(s)." % len(to_release)
        warn = bool(unscored)
        if unscored:
            msg += (" NOTE: %s of them still have subjective answers being "
                    "graded - their scores will fill in automatically as the "
                    "grader finishes (usually within a minute). Use 'Run "
                    "Subjective Evaluation' if it stays pending."
                    % len(unscored))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Results Released",
                "message": msg,
                "type": "warning" if warn else "success",
                "sticky": warn,
            },
        }

    @api.model
    def _scoring_shard_count(self):
        """Configured parallel scoring lanes (ir.config_parameter
        etp_assessment_pro.scoring_shards). Clamped to [1, MAX_SCORING_SHARDS];
        1 (default) reproduces the original single-lock serial drainer.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.scoring_shards", "1")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(n, MAX_SCORING_SHARDS))

    @api.model
    def _cron_llm_auto_score(self, shard=0):
        """Drain one scoring shard. Each shard cron owns a disjoint slice of
        submitted evaluators (id %% shard_count == shard) under its OWN advisory
        lock, so N shard crons score N candidates in parallel instead of one at a
        time. Shard 0 keeps ADVISORY_LOCK_AUTOSCORE (identical to the single-lock
        design at shard_count=1); higher shards are no-ops until scoring_shards is
        raised, so parallelism is a pure config lever with no cron/code change.
        """
        shard_count = self._scoring_shard_count()
        if shard >= shard_count:
            return
        lock = (ADVISORY_LOCK_AUTOSCORE if shard == 0
                else ADVISORY_LOCK_AUTOSCORE_SHARD_BASE + shard)
        self.env.cr.execute("SELECT pg_advisory_unlock_all()")
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (lock,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            for ev in self._scoring_shard_evaluators(shard, shard_count):
                try:
                    with self.env.cr.savepoint():
                        ev.action_llm_score()
                    self.env.cr.commit()
                except Exception:
                    _logger.exception(
                        "Auto-score failed for evaluator %s", ev.id)
                    continue
        finally:
            try:
                self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (lock,))
            except Exception:
                _logger.warning(
                    "auto-score advisory lock %s not released this tick", lock)

    @api.model
    def _scoring_shard_evaluators(self, shard, shard_count, limit=20):
        """Up to ``limit`` submitted-and-queued evaluators owned by this shard
        (id %% shard_count == shard). shard_count=1 skips the modulo so the query
        is identical to the original drainer. Queued evaluators are bounded by the
        live cohort, so selecting matching ids and capping in Python is cheap and
        keeps the per-shard limit exact without ORM-version-specific raw SQL.
        """
        Evaluator = self.env["etp.assessment.pro.evaluator"]
        domain = [
            ("state", "=", "submitted"),
            ("scoring_requested", "=", True),
            ("llm_state", "in", ("pending", "scoring", "partial", "failed")),
        ]
        if shard_count <= 1:
            return Evaluator.search(domain, limit=limit)
        matching = Evaluator.search(domain, order="id").filtered(
            lambda e: e.id % shard_count == shard)
        return matching[:limit]

    def action_export_results(self):
        self.ensure_one()
        from ..services import export as export_svc
        return export_svc.export_results(self)

    def action_export_responses(self):
        self.ensure_one()
        from ..services import export as export_svc
        return export_svc.export_responses(self)

    def _check_plan_complete(self):
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
        "etp.assessment.pro", required=True, ondelete="cascade", index=True)
    applicant_id = fields.Many2one(
        "hr.applicant", string="Candidate", required=True, ondelete="restrict")
    invite_state = fields.Selection(
        [("none", "Not Invited"), ("queued", "Queued"),
         ("sent", "Sent"), ("failed", "Failed")],
        default="none", index=True, copy=False, string="Invite",
        help="Invitation email status. Launch queues invites; a background cron "
             "sends them in batches so a large cohort never blocks the request.")
    invite_error = fields.Char(readonly=True, copy=False)

    def init(self):
        self._cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS etp_pro_evaluator_uniq_assess_appl
            ON etp_assessment_pro_evaluator (assessment_id, applicant_id)
        """)

    def _candidate_user(self):
        self.ensure_one()
        user = self.applicant_id.candidate_user_id
        if not user and self.applicant_id.partner_id:
            user = self.env["res.users"].sudo().search(
                [("partner_id", "=", self.applicant_id.partner_id.id)], limit=1)
        if not user and (self.applicant_id.email_from or "").strip():
            # Security: login==email fallback can match an internal user never
            # bound as a candidate, widening candidate auth.
            user = self.env["res.users"].sudo().with_context(
                active_test=False).search(
                [("login", "=ilike", self.applicant_id.email_from.strip())],
                limit=1)
        return user
    access_token = fields.Char(
        string="Access Token", index=True, copy=False,
        default=lambda self: str(uuid.uuid4()))
    question_order = fields.Text(string="Shuffled Question Order (JSON)")
    started_at = fields.Datetime(string="Started At")
    submitted_at = fields.Datetime(
        string="Submitted On", readonly=True, copy=False)
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

    is_locked = fields.Boolean(default=False, string="Locked")
    is_violated = fields.Boolean(default=False, string="Violated", readonly=True)
    violation_reason = fields.Char(string="Violation Reason", readonly=True)
    violation_datetime = fields.Datetime(string="Violation Time", readonly=True)
    violation_count = fields.Integer(
        string="Violations", default=0, readonly=True,
        help="Cumulative count of detected violations for this candidate. "
             "Compared against assessment.max_violations to decide auto-submit.",
    )

    llm_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("scoring", "Scoring"),
            ("scored", "Scored"),
            ("partial", "Partial"),
            ("failed", "Failed"),
            ("error", "Scoring Error"),
        ],
        default="pending",
        string="Subjective Scoring",
        copy=False,
        help="error = at least one of the candidate's answers hit a hard "
             "scoring error after all retries (surfaced, not silently scored).",
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
    scoring_error_flag = fields.Char(
        compute="_compute_scoring_error_flag", store=True, string="Scoring Error",
        help="Shows '!' when a subjective answer hit a terminal scoring error "
             "(Vertex failure): the candidate can't be finalized until an admin "
             "runs 'Reset & Re-score Errored'.")

    @api.depends("response_ids.llm_state")
    def _compute_scoring_error_flag(self):
        for rec in self:
            rec.scoring_error_flag = "!" if any(
                r.llm_state == "error" for r in rec.response_ids) else False

    integrity_alert = fields.Boolean(
        compute="_compute_integrity_alert", store=True,
        string="Integrity Alert",
        help="True when any of this candidate's answers tripped a scoring "
             "integrity signal (empty/injection gate or answer-key drift), so "
             "an admin can spot and filter flagged candidates. Display only; "
             "never affects the score or pass/fail.")

    @api.depends("response_ids.integrity_alert")
    def _compute_integrity_alert(self):
        for rec in self:
            rec.integrity_alert = any(
                r.integrity_alert for r in rec.response_ids)

    def write(self, vals):
        if vals.get("state") == "submitted":
            stamp = fields.Datetime.now()
            for rec in self.filtered(lambda r: not r.submitted_at):
                super(EtpAssessmentEvaluator, rec).write(
                    {"submitted_at": stamp})
        res = super().write(vals)
        # Auto-queue subjective grading the moment a candidate submits, so the
        # every-minute background cron (_cron_llm_auto_score) drains it without
        # an admin having to click "Run Subjective Evaluation" - and without
        # lagging the exam, because the actual Vertex call happens in the cron,
        # never in this request. Respects the per-assessment llm_auto_score
        # toggle (default OFF while the Vertex testing budget is frozen; flip ON
        # per-assessment to auto-grade, or OFF to halt all Vertex spend).
        # Only flags candidates that actually have subjective (needs_llm)
        # answers still awaiting a score.
        if vals.get("state") == "submitted":
            to_queue = self.filtered(
                lambda r: r.assessment_id.llm_auto_score
                and not r.scoring_requested and any(
                    resp.needs_llm and resp.llm_state != "scored"
                    for resp in r.response_ids))
            if to_queue:
                super(EtpAssessmentEvaluator, to_queue).write(
                    {"scoring_requested": True, "llm_state": "pending"})
        return res

    score_percent = fields.Float(
        string="Score %", compute="_compute_result", store=True,
        aggregator="avg")
    pass_threshold = fields.Float(
        string="Pass Threshold %", compute="_compute_result", store=True)
    result = fields.Selection(
        [("pending", "Pending"), ("pass", "Pass"), ("fail", "Fail")],
        string="Result", compute="_compute_result", store=True, default="pending")
    results_released = fields.Boolean(
        string="Results Released", default=False,
        help="Gates candidate-facing results. An admin flips this by "
             "releasing results; until then the candidate sees no score.",
    )
    scoring_requested = fields.Boolean(
        string="Subjective Grading Requested", default=False, copy=False,
        help="Set by 'Run Subjective Evaluation'. The background grader drains "
             "requested candidates in batches of 20; subjective scoring never "
             "runs during the exam.",
    )

    result_summary = fields.Html(
        string="Result Summary", compute="_compute_result_summary",
        sanitize=False,
        help="Post-scoring overview for this candidate: objective and "
             "subjective tallies, total, percentage, and pass/fail.")

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
        import html as _html

        def esc(v):
            return _html.escape(str(v))

        for rec in self:
            submitted = rec.response_ids.filtered(
                lambda r: r.state == "submitted")
            obj = submitted.filtered(lambda r: r.has_objective)
            obj_total = len(obj)
            obj_correct = len(obj.filtered(
                lambda r: r.max_score and r.score >= r.max_score))
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
                    'Candidate has not submitted yet - '
                    f'{esc(rec.answered_count or 0)} of '
                    f'{esc(rec.total_questions or 0)} answered. '
                    'Summary appears once the assessment is submitted and '
                    'scored.</div>')
                continue

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
                    'response(s) still awaiting scoring - totals below are '
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
            rec.subjective_pending = len(need.filtered(
                lambda r: r.llm_state in (
                    "pending", "queued", "failed", "error")))

    @api.depends("total_score", "max_possible_score", "llm_total_score",
                 "llm_max_score", "subjective_pending", "state",
                 "total_questions", "answered_count",
                 "response_ids.llm_state")
    def _compute_result(self):
        for rec in self:
            raw_t = rec.assessment_id.subjective_threshold
            threshold = raw_t if 0.0 <= raw_t <= 100.0 \
                else DEFAULT_SUBJECTIVE_THRESHOLD
            rec.pass_threshold = threshold
            earned = (rec.total_score or 0) + (rec.llm_total_score or 0)
            possible = rec.total_questions or 0
            if not possible:
                possible = (rec.max_possible_score or 0) + (rec.llm_max_score or 0)
            rec.score_percent = round(
                (earned / possible) * 100.0, 2) if possible else 0.0
            if rec.state != "submitted" or rec.subjective_pending or not possible:
                rec.result = "pending"
            else:
                rec.result = "pass" if rec.score_percent >= threshold else "fail"

    def _compute_subjective_rollup(self):
        for rec in self:
            need = rec.response_ids.filtered(lambda r: r.needs_llm)
            if not need:
                rec.llm_state = "scored"
            elif all(r.llm_state in ("scored", "error") for r in need):
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
                    "web.base.url", "")
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
            tpl.send_mail(rec.id, force_send=True)

    def _deliver_invitation(self):
        self.ensure_one()
        user = self.applicant_id.candidate_user_id
        if user and not user.login_date and not user._is_internal():
            user.sudo().with_context(
                create_user=1, import_file=False, install_mode=False,
            ).action_reset_password()
        self._send_single_invitation()

    def _auto_submit_expired(self):
        """Settle an expired/finished attempt: fill placeholders for unanswered
        questions, lock it, and flip the assessment to done once all evaluators
        are submitted. Shared by the portal path and _cron_expire_stale_attempts
        so an abandoned tab settles identically; idempotent per response.
        """
        self.ensure_one()
        Response = self.env["etp.assessment.pro.response"].sudo()
        question_order = json.loads(self.question_order or "[]")
        for q_id in question_order:
            existing = Response.search([
                ("assessment_evaluator_id", "=", self.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue
            draft = Response.search([
                ("assessment_evaluator_id", "=", self.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted", "llm_state": "not_needed"})
            else:
                try:
                    with self.env.cr.savepoint():
                        Response.create({
                            "assessment_id": self.assessment_id.id,
                            "assessment_evaluator_id": self.id,
                            "evaluator_id": self.applicant_id.id,
                            "question_id": q_id,
                            "justification": "[Auto-submitted: time expired]",
                            "state": "submitted",
                            "llm_state": "not_needed",
                        }).flush_recordset()
                except IntegrityError:
                    continue
        self.write({"state": "submitted", "is_locked": True})
        assessment = self.assessment_id
        evs = assessment.assessment_evaluator_ids
        if evs and all(e.state == "submitted" for e in evs):
            assessment.write({"state": "done"})

    @api.model
    def _cron_expire_stale_attempts(self, limit=100):
        """Rescue abandoned in-progress attempts past their deadline. Without
        this, a candidate who closes the tab is never auto-submitted (that only
        happened on a live portal request), leaving answers unscored and the
        assessment pinned in 'in_progress'. Unlimited sittings (no
        deadline_datetime) are skipped by the '<' filter.
        """
        self.env.cr.execute("SELECT pg_advisory_unlock_all()")
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_EXPIRE_ATTEMPTS,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            stale = self.search([
                ("state", "=", "in_progress"),
                ("deadline_datetime", "!=", False),
                ("deadline_datetime", "<", fields.Datetime.now()),
            ], limit=limit)
            for ev in stale:
                try:
                    with self.env.cr.savepoint():
                        ev._auto_submit_expired()
                    self.env.cr.commit()
                except Exception:  # noqa: BLE001 - isolate per candidate
                    _logger.exception(
                        "Expire-stale: auto-submit failed for evaluator %s",
                        ev.id)
                    continue
        finally:
            try:
                self.env.cr.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (ADVISORY_LOCK_EXPIRE_ATTEMPTS,))
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "expire-stale advisory lock %s not released this tick",
                    ADVISORY_LOCK_EXPIRE_ATTEMPTS)

    @api.model
    def _cron_send_pending_invitations(self, batch=25):
        pending = self.search([("invite_state", "=", "queued")], limit=batch)
        for ev in pending:
            try:
                with self.env.cr.savepoint():
                    ev._deliver_invitation()
                    ev.invite_state = "sent"
                    ev.invite_error = False
                self.env.cr.commit()
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "Invite send failed for candidate %s (evaluator %s)",
                    ev.applicant_id.partner_name, ev.id)
                try:
                    with self.env.cr.savepoint():
                        ev.invite_state = "failed"
                        ev.invite_error = str(exc)[:200]
                    self.env.cr.commit()
                except Exception:
                    _logger.exception(
                        "Invite cron: could not flag evaluator %s failed", ev.id)

    def action_requeue_invitation(self):
        self.filtered(lambda e: e.invite_state != "sent").write(
            {"invite_state": "queued", "invite_error": False})

    def action_llm_score(self):
        from ..services import scoring as scoring_svc
        scored = 0
        for rec in self:
            if rec.state != "submitted":
                raise UserError(
                    f"Candidate '{rec.applicant_id.partner_name}' has not "
                    f"submitted - scoring runs on submitted assessments only.")
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

    def action_queue_llm_score(self):
        for rec in self:
            if rec.state != "submitted":
                raise UserError(
                    f"Candidate '{rec.applicant_id.partner_name}' has not "
                    f"submitted - scoring runs on submitted assessments only.")
        self.write({"scoring_requested": True, "llm_state": "pending"})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Subjective Grading Queued",
                "message": "Queued for the background grader - refresh shortly.",
                "type": "success", "sticky": False,
            },
        }

    def action_reset_errored_scoring(self):
        reset = 0
        for rec in self:
            errored = rec.response_ids.filtered(lambda r: r.llm_state == "error")
            if not errored:
                continue
            errored.write({"llm_state": "pending", "llm_attempts": 0})
            rec.write({"scoring_requested": True, "llm_state": "pending"})
            reset += len(errored)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Re-queued for scoring",
                "message": f"Reset {reset} errored answer(s) to pending; the "
                           f"background grader will retry them.",
                "type": "success", "sticky": False,
            },
        }

    def action_export_responses(self):
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

    @api.depends("question_order")
    def _compute_total_questions(self):
        for rec in self:
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


class EtpAssessmentResponse(models.Model):
    _name = "etp.assessment.pro.response"
    _description = "Assessment Response"
    _order = "create_date desc"

    assessment_id = fields.Many2one(
        "etp.assessment.pro", required=True, ondelete="cascade")
    assessment_evaluator_id = fields.Many2one(
        "etp.assessment.pro.evaluator", string="Candidate Assignment",
        ondelete="cascade", index=True)
    evaluator_id = fields.Many2one(
        "hr.applicant", string="Candidate", required=True)
    question_id = fields.Many2one(
        "etp.assessment.pro.question", required=True, ondelete="cascade",
        index=True)
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
    llm_key_closeness = fields.Float(
        string="Key Closeness (0-100)", readonly=True, copy=False,
        help="v10: how close the worker answer is to the golden answer, judged "
             "claim by claim. Weight 0.60 of the score.")
    llm_sop_coverage = fields.Float(
        string="SOP Coverage (0-100)", readonly=True, copy=False,
        help="v10: how many of the question's required elements the answer "
             "demonstrates. Weight 0.25 of the score.")
    llm_clarity = fields.Char(
        string="Clarity", readonly=True, copy=False,
        help="v10: clear / mixed / unclear. Weight 0.15 of the score.")
    llm_ai_confidence = fields.Char(
        string="AI-likeness Confidence", readonly=True, copy=False,
        help="v10: none / medium / high. Never changes the score, a flag only.")
    llm_verdict_consistency = fields.Char(
        string="Verdict Consistency", readonly=True, copy=False,
        help="v10: match / contradiction / indeterminate / not_applicable - "
             "does the worker's committed verdict agree with the answer key.")
    llm_golden_claims_json = fields.Text(
        string="Golden Claims (JSON)", readonly=True, copy=False,
        help="v10: the golden answer decomposed into deciding/supporting claims "
             "with the worker's per-claim hit/partial/miss verdict + evidence "
             "quote. The audit trail of how key closeness was judged.")
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
    ab_mcq_pct = fields.Float(
        compute="_compute_ab_scores", string="Verdict %",
        help="image_ab: % of scoring axes whose chosen verdict matches the key "
             "(objective, no LLM).")
    ab_final_pct = fields.Float(
        compute="_compute_ab_scores", string="Final %",
        help="image_ab final 0-100: verdict-only when the justification is off, "
             "else ceil(0.75*verdict% + 0.25*justification%). Pass/fail vs the "
             "threshold gives the 1 mark.")

    integrity_alert = fields.Boolean(
        string="Integrity Alert", compute="_compute_integrity_alert",
        store=True, copy=False,
        help="Read-only audit flag: True when the scoring audit signals an "
             "integrity event - a Phase-1 gate (empty_answer/injection_attempt), "
             "a Phase-3 answer-key drift, or an integrity/key_drift flag. "
             "Display + filter only; never affects the score or pass/fail.")
    ab_verdict_pct = fields.Float(
        string="AB Verdict %", compute="_compute_audit_subscores",
        help="image_ab verdict-lane sub-score read from the scoring audit "
             "(0-100). Display-only; does NOT affect llm_raw_100 or the mark.")
    ab_justification_pct = fields.Float(
        string="AB Justification %", compute="_compute_audit_subscores",
        help="image_ab justification-lane sub-score from the audit (0-100); "
             "0 when the answer was verdict-only. Display-only.")
    label_coverage_pct = fields.Float(
        string="Label Coverage %", compute="_compute_audit_subscores",
        help="image_label coverage sub-score (attempted/total boxes, 0-100) "
             "read from the audit. Display-only.")
    label_correctness_pct = fields.Float(
        string="Label Correctness %", compute="_compute_audit_subscores",
        help="image_label correctness sub-score (grader accuracy, 0-100) read "
             "from the audit. Display-only.")

    @staticmethod
    def _audit_json(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    @api.depends("llm_gate", "llm_flags_json", "llm_result_json")
    def _compute_integrity_alert(self):
        flag_alerts = ("integrity_alert", "key_drift")
        for rec in self:
            alert = is_integrity_gate(rec.llm_gate)
            if not alert:
                flags = rec._audit_json(rec.llm_flags_json)
                if isinstance(flags, list):
                    alert = any(
                        str(f).strip().lower() in flag_alerts for f in flags)
            if not alert:
                audit = rec._audit_json(rec.llm_result_json)
                if isinstance(audit, dict):
                    if audit.get("integrity_gated") or \
                            audit.get("integrity_key_drift"):
                        alert = True
                    else:
                        afl = audit.get("flags")
                        if isinstance(afl, list):
                            alert = any(
                                str(f).strip().lower() in flag_alerts
                                for f in afl)
            rec.integrity_alert = alert

    @staticmethod
    def _pct_from_fraction(value):
        try:
            return round(max(0.0, float(value)) * 100.0, 2)
        except (TypeError, ValueError):
            return 0.0

    @api.depends("llm_result_json")
    def _compute_audit_subscores(self):
        for rec in self:
            audit = rec._audit_json(rec.llm_result_json)
            ab = audit.get("ab_scores") if isinstance(audit, dict) else None
            lab = audit.get("label_scores") if isinstance(audit, dict) else None
            ab = ab if isinstance(ab, dict) else {}
            lab = lab if isinstance(lab, dict) else {}
            rec.ab_verdict_pct = rec._pct_from_fraction(ab.get("verdict_score"))
            rec.ab_justification_pct = rec._pct_from_fraction(
                ab.get("justification_score"))
            rec.label_coverage_pct = rec._pct_from_fraction(lab.get("coverage"))
            rec.label_correctness_pct = rec._pct_from_fraction(
                lab.get("correctness"))

    def init(self):
        self._cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS etp_pro_response_uniq_eval_q_sess
            ON etp_assessment_pro_response
               (assessment_evaluator_id, question_id)
        """)

    @api.depends("line_ids.selected_option_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_answer_summary(self):
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

    def _image_ab_mcq_pct(self):
        self.ensure_one()
        total = correct = 0
        for qd in self.question_id.question_dimension_ids:
            key = set(qd.option_line_ids.filtered("is_correct").ids)
            if not key:
                continue
            total += 1
            chosen = {line.selected_option_id.id for line in self.line_ids
                      if line.question_dimension_id.id == qd.id
                      and line.selected_option_id}
            if chosen == key:
                correct += 1
        return (correct / total * 100.0) if total else 0.0

    def _image_ab_uses_llm(self):
        self.ensure_one()
        return bool(
            self.question_id.question_type == "image_ab"
            and self.assessment_id.require_justification_image_comparison
            and (self.justification or "").strip())

    @api.depends("question_id.question_type", "line_ids.selected_option_id",
                 "line_ids.question_dimension_id",
                 "question_id.question_dimension_ids.option_line_ids.is_correct",
                 "llm_raw_100", "llm_state")
    def _compute_ab_scores(self):
        for rec in self:
            if rec.question_id.question_type != "image_ab":
                rec.ab_mcq_pct = 0.0
                rec.ab_final_pct = 0.0
                continue
            mcq = rec._image_ab_mcq_pct()
            rec.ab_mcq_pct = round(mcq, 2)
            if rec.llm_state == "scored":
                rec.ab_final_pct = round(rec.llm_raw_100 or 0.0, 2)
            else:
                rec.ab_final_pct = float(math.ceil(mcq))

    @api.depends("llm_raw_100", "llm_state", "needs_llm", "ab_final_pct",
                 "question_id.question_type")
    def _compute_subjective_marks(self):
        for rec in self:
            raw_t = rec.assessment_id.subjective_threshold
            threshold = raw_t if 0.0 <= raw_t <= 100.0 \
                else DEFAULT_SUBJECTIVE_THRESHOLD
            if rec.question_id.question_type == "image_ab":
                raw100 = rec.llm_raw_100 or 0.0
                rec.llm_raw_score = round(raw100 / 100.0, 4)
                rec.llm_max_score = 1
                passed = rec.llm_state == "scored" and raw100 >= threshold
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
        for rec in self:
            qtype = rec.question_id.question_type
            rec.has_objective = qtype in ("mcq", "msq")
            just = (rec.justification or "").strip()
            is_placeholder = just.startswith("[Auto-submitted")
            if qtype == "image_ab":
                rec.needs_llm = bool(rec.line_ids) and not is_placeholder
            else:
                rec.needs_llm = (
                    qtype in ("subjective_rubric",
                              "image_prompt", "image_label", "video_prompt")
                    and bool(just)
                    and not is_placeholder
                )

    @api.depends("line_ids.selected_option_id", "line_ids.question_dimension_id",
                 "question_id.question_type",
                 "question_id.question_dimension_ids.option_line_ids.is_correct")
    def _compute_score(self):
        for rec in self:
            qtype = rec.question_id.question_type
            if qtype not in ("mcq", "msq"):
                rec.score = 0
                rec.max_score = 0
                continue
            objective_dims = rec.question_id.question_dimension_ids.filtered(
                lambda qd: qd.option_line_ids.filtered("is_correct"))
            if not objective_dims:
                rec.score = 0
                rec.max_score = 1
                continue
            all_correct = True
            for qd in objective_dims:
                correct_for_dim = {
                    ol.id
                    for ol in qd.option_line_ids.filtered("is_correct")}
                chosen_for_dim = {
                    line.selected_option_id.id
                    for line in rec.line_ids
                    if line.selected_option_id
                    and line.question_dimension_id.id == qd.id}
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
            has_dims = bool(rec.question_id.question_dimension_ids)
            if qtype in ("mcq", "msq", "image_ab") and not rec.line_ids:
                raise UserError(
                    "Please answer at least one dimension before submitting.")
            if qtype in ("subjective_rubric",
                         "image_prompt", "image_label", "video_prompt") \
                    and not (rec.justification or "").strip():
                raise UserError(
                    "Please provide a justification before submitting.")
            if qtype in ("image_prompt", "image_label") and has_dims \
                    and not rec.line_ids:
                raise UserError(
                    "Please answer the multiple-choice checks before "
                    "submitting.")
            rec.write({"state": "submitted"})

            if qtype == "image_ab" or (rec.justification or "").strip():
                rec._enqueue_subjective_scoring()

            if rec.assessment_evaluator_id:
                rec._check_all_submitted()

    def _enqueue_subjective_scoring(self):
        # Queue only: LLM scoring must never run inline on the candidate's
        # submit path - the cron drains it.
        from ..services import scoring as scoring_svc
        auto_eval_ids = set()
        repend_eval_ids = set()
        for rec in self:
            qtype = rec.question_id.question_type
            if qtype == "image_ab":
                if not rec.line_ids:
                    rec.llm_state = "not_needed"
                elif rec._image_ab_uses_llm():
                    rec.llm_state = "pending"
                    if rec.assessment_evaluator_id:
                        repend_eval_ids.add(rec.assessment_evaluator_id.id)
                        if rec.assessment_id.llm_auto_score:
                            auto_eval_ids.add(rec.assessment_evaluator_id.id)
                else:
                    scoring_svc._store_ab_verdict_only(rec)
                    if rec.assessment_evaluator_id:
                        repend_eval_ids.add(rec.assessment_evaluator_id.id)
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
        if repend_eval_ids:
            self.env["etp.assessment.pro.evaluator"].browse(
                repend_eval_ids)._compute_subjective_rollup()

    def _check_all_submitted(self):
        evaluator = self.assessment_evaluator_id
        if not evaluator:
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
    question_dimension_id = fields.Many2one(
        "etp.assessment.pro.question.dimension",
        string="Dimension", required=True, ondelete="restrict")
    selected_option_id = fields.Many2one(
        "etp.assessment.pro.question.dimension.option",
        string="Selected Option",
        ondelete="restrict",
    )
