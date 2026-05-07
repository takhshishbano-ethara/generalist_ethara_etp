# -*- coding: utf-8 -*-
import json
import logging

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)


class EtpAssessmentPortal(http.Controller):

    def _get_evaluator_from_token(self, token):
        if not token:
            return False
        return (
            request.env["etp.assessment.evaluator"]
            .sudo()
            .search([("access_token", "=", token)], limit=1)
        )

    def _auto_submit_remaining(self, evaluator):
        question_order = json.loads(evaluator.question_order or "[]")
        Response = request.env["etp.assessment.response"].sudo()

        for q_id in question_order:
            existing = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue

            draft = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted"})
            else:
                Response.create({
                    "assessment_id": evaluator.assessment_id.id,
                    "assessment_evaluator_id": evaluator.id,
                    "evaluator_id": evaluator.employee_id.id,
                    "question_id": q_id,
                    "justification": "[Auto-submitted: time expired]",
                    "state": "submitted",
                })

        evaluator.write({"state": "submitted", "is_locked": True})
        self._check_assessment_complete(evaluator)

    def _check_assessment_complete(self, evaluator):
        assessment = evaluator.assessment_id
        all_assignments = assessment.assessment_evaluator_ids
        if all_assignments and all(a.state == "submitted" for a in all_assignments):
            assessment.write({"state": "done"})

    @http.route(
        "/assessment/<string:token>",
        type="http",
        auth="public",
        website=True,
    )
    def assessment_landing(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")

        assessment = evaluator.assessment_id
        if assessment.state != "in_progress":
            return request.render(
                "etp_assessment.portal_assessment_closed",
                {"assessment": assessment, "evaluator": evaluator},
            )

        if evaluator.is_locked:
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator},
            )

        if not evaluator.started_at:
            return request.render(
                "etp_assessment.portal_instructions",
                {
                    "assessment": assessment,
                    "evaluator": evaluator,
                    "token": token,
                    "duration_minutes": assessment.duration_minutes,
                },
            )

        if evaluator.is_time_expired():
            self._auto_submit_remaining(evaluator)
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator},
            )

        question_order = json.loads(evaluator.question_order or "[]")
        questions = request.env["etp.assessment.question"].sudo().browse(question_order)

        answered_ids = (
            request.env["etp.assessment.response"]
            .sudo()
            .search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("state", "=", "submitted"),
            ])
            .mapped("question_id.id")
        )

        current_question = None
        current_index = 0
        for idx, q in enumerate(questions):
            if q.id not in answered_ids:
                current_question = q
                current_index = idx + 1
                break

        if not current_question:
            return request.render(
                "etp_assessment.portal_assessment_complete",
                {"assessment": assessment, "evaluator": evaluator},
            )

        deadline_iso = ""
        if evaluator.deadline_datetime:
            deadline_iso = evaluator.deadline_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")

        dimensions = current_question.question_dimension_ids
        return request.render(
            "etp_assessment.portal_question_page",
            {
                "assessment": assessment,
                "evaluator": evaluator,
                "question": current_question,
                "dimensions": dimensions,
                "current_index": current_index,
                "total_questions": len(question_order),
                "token": token,
                "progress_percent": int(
                    (len(answered_ids) / len(question_order)) * 100
                )
                if question_order
                else 0,
                "deadline_iso": deadline_iso,
                "duration_minutes": assessment.duration_minutes,
            },
        )

    @http.route(
        "/assessment/<string:token>/begin",
        type="http",
        auth="public",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def assessment_begin(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")

        if not evaluator.started_at:
            evaluator.write({"started_at": fields.Datetime.now()})

        return request.redirect(f"/assessment/{token}")

    @http.route(
        "/assessment/<string:token>/submit",
        type="http",
        auth="public",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def assessment_submit_response(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")

        if evaluator.is_locked:
            return request.redirect(f"/assessment/{token}")

        if evaluator.is_time_expired():
            self._auto_submit_remaining(evaluator)
            return request.redirect(f"/assessment/{token}")

        question_id = int(kw.get("question_id", 0))
        justification = kw.get("justification", "")

        if not question_id:
            return request.redirect(f"/assessment/{token}")

        question = (
            request.env["etp.assessment.question"].sudo().browse(question_id)
        )
        if not question.exists():
            return request.redirect(f"/assessment/{token}")

        existing_response = (
            request.env["etp.assessment.response"]
            .sudo()
            .search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", question_id),
            ], limit=1)
        )
        if existing_response and existing_response.state == "submitted":
            return request.redirect(f"/assessment/{token}")

        line_vals = []
        for qd in question.question_dimension_ids:
            option_key = f"dimension_{qd.dimension_id.id}"
            option_id = kw.get(option_key)
            if option_id:
                line_vals.append((0, 0, {
                    "dimension_id": qd.dimension_id.id,
                    "selected_option_id": int(option_id),
                }))

        if not line_vals:
            return request.redirect(f"/assessment/{token}")

        if existing_response:
            existing_response.line_ids.unlink()
            existing_response.write({
                "justification": justification,
                "line_ids": line_vals,
            })
            response = existing_response
        else:
            response = (
                request.env["etp.assessment.response"]
                .sudo()
                .create({
                    "assessment_id": evaluator.assessment_id.id,
                    "assessment_evaluator_id": evaluator.id,
                    "evaluator_id": evaluator.employee_id.id,
                    "question_id": question_id,
                    "justification": justification,
                    "line_ids": line_vals,
                })
            )

        response.action_submit()

        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})

        return request.redirect(f"/assessment/{token}")

    @http.route(
        "/assessment/<string:token>/progress",
        type="http",
        auth="public",
        website=True,
    )
    def assessment_progress(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")

        question_order = json.loads(evaluator.question_order or "[]")
        questions = request.env["etp.assessment.question"].sudo().browse(question_order)

        responses = (
            request.env["etp.assessment.response"]
            .sudo()
            .search([("assessment_evaluator_id", "=", evaluator.id)])
        )
        response_map = {r.question_id.id: r for r in responses}

        question_statuses = []
        for idx, q in enumerate(questions):
            resp = response_map.get(q.id)
            question_statuses.append({
                "index": idx + 1,
                "question": q,
                "status": resp.state if resp else "pending",
            })

        return request.render(
            "etp_assessment.portal_progress_page",
            {
                "assessment": evaluator.assessment_id,
                "evaluator": evaluator,
                "question_statuses": question_statuses,
                "token": token,
                "total_questions": len(question_order),
                "answered_count": evaluator.answered_count,
            },
        )

    @http.route(
        "/assessment/<string:token>/violation",
        type="http",
        auth="public",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def assessment_violation(self, token, **kw):
        evaluator = self._get_evaluator_from_token(token)
        if not evaluator:
            return request.render("etp_assessment.portal_invalid_token")

        if evaluator.is_locked:
            return request.redirect(f"/assessment/{token}")

        reason = kw.get("violation_reason", "Unknown violation")
        _logger.warning(
            "VIOLATION for candidate '%s' (assessment: %s): %s",
            evaluator.employee_id.name,
            evaluator.assessment_id.name,
            reason,
        )

        evaluator.write({
            "is_violated": True,
            "violation_reason": reason,
            "violation_datetime": fields.Datetime.now(),
        })

        self._auto_submit_remaining_violation(evaluator, reason)

        return request.redirect(f"/assessment/{token}")

    def _auto_submit_remaining_violation(self, evaluator, reason):
        question_order = json.loads(evaluator.question_order or "[]")
        Response = request.env["etp.assessment.response"].sudo()

        for q_id in question_order:
            existing = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "submitted"),
            ], limit=1)
            if existing:
                continue

            draft = Response.search([
                ("assessment_evaluator_id", "=", evaluator.id),
                ("question_id", "=", q_id),
                ("state", "=", "draft"),
            ], limit=1)
            if draft:
                draft.write({"state": "submitted"})
            else:
                Response.create({
                    "assessment_id": evaluator.assessment_id.id,
                    "assessment_evaluator_id": evaluator.id,
                    "evaluator_id": evaluator.employee_id.id,
                    "question_id": q_id,
                    "justification": f"[Auto-submitted: VIOLATION - {reason}]",
                    "state": "submitted",
                })

        evaluator.write({"state": "submitted", "is_locked": True})
        self._check_assessment_complete(evaluator)
