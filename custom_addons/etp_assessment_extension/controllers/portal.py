"""Candidate portal REST API (token-auth).

Mirrors `etp_assessment/controllers/portal.py` but returns JSON instead of
rendered website templates. The candidate's per-assignment `access_token`
(generated when the assessment is started and sent via email) is the
authentication mechanism — no gateway access_token header is needed.
"""

import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response

from .common import coerce_int, parse_json_body, pct

_logger = logging.getLogger(__name__)


def _get_evaluator(token):
    if not token:
        return False
    return (
        request.env["etp.assessment.evaluator"]
        .sudo()
        .search([("access_token", "=", token)], limit=1)
    )


def _serialize_evaluator(ev):
    return {
        "id": ev.id,
        "name": ev.employee_id.name if ev.employee_id else "",
        "email": (
            (ev.employee_id.work_email or ev.employee_id.private_email or "")
            if ev.employee_id else ""
        ),
        "state": ev.state,
        "started_at": ev.started_at.isoformat() if ev.started_at else None,
        "deadline_iso": (
            ev.deadline_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
            if ev.deadline_datetime else None
        ),
        "total_questions": ev.total_questions or 0,
        "answered_count": ev.answered_count or 0,
        "progress_percent": pct(ev.answered_count, ev.total_questions),
        "is_locked": bool(ev.is_locked),
        "is_violated": bool(ev.is_violated),
        "violation_reason": ev.violation_reason or "",
    }


def _serialize_assessment_brief(a):
    return {
        "id": a.id,
        "name": a.name or "",
        "duration_minutes": a.duration_minutes or 0,
        "state": a.state,
    }


def _serialize_question_for_portal(q):
    dimensions = []
    for qd in q.question_dimension_ids.sorted("sequence"):
        dim = qd.dimension_id
        if not dim:
            continue
        dimensions.append({
            "dimension_id": dim.id,
            "name": dim.name or "",
            "sequence": qd.sequence or 0,
            "options": [
                {
                    "id": opt.id,
                    "name": opt.name or "",
                    "sequence": opt.sequence or 0,
                }
                for opt in dim.option_ids.sorted("sequence")
            ],
        })

    return {
        "id": q.id,
        "name": q.name or "",
        "question_type": q.question_type or "",
        "prompt": q.prompt or "",
        "description": q.description or "",
        "image_a_url": q.image_a_url or "",
        "image_b_url": q.image_b_url or "",
        "code_snippet": q.code_snippet or "",
        "code_language": q.code_language or "",
        "video_url": q.video_url or "",
        "dimensions": dimensions,
    }


def _auto_submit_remaining(evaluator, reason=None):
    """Mark every un-submitted question as submitted, then lock the assignment.

    Mirrors `_auto_submit_remaining` / `_auto_submit_remaining_violation` from
    the website portal.
    """
    question_order = json.loads(evaluator.question_order or "[]")
    Response = request.env["etp.assessment.response"].sudo()
    justification = (
        f"[Auto-submitted: VIOLATION - {reason}]"
        if reason else "[Auto-submitted: time expired]"
    )

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
                "justification": justification,
                "state": "submitted",
            })

    evaluator.write({"state": "submitted", "is_locked": True})
    _check_assessment_complete(evaluator)


def _check_assessment_complete(evaluator):
    assessment = evaluator.assessment_id
    all_assignments = assessment.assessment_evaluator_ids
    if all_assignments and all(a.state == "submitted" for a in all_assignments):
        assessment.write({"state": "done"})


def _current_question(evaluator):
    """Return (current_question_record, current_index, total) for the candidate.

    None question => candidate is finished.
    """
    question_order = json.loads(evaluator.question_order or "[]")
    if not question_order:
        return False, 0, 0
    questions = (
        request.env["etp.assessment.question"].sudo().browse(question_order)
    )

    answered_ids = (
        request.env["etp.assessment.response"]
        .sudo()
        .search([
            ("assessment_evaluator_id", "=", evaluator.id),
            ("state", "=", "submitted"),
        ])
        .mapped("question_id.id")
    )

    for idx, q in enumerate(questions):
        if q.id not in answered_ids:
            return q, idx + 1, len(question_order)
    return False, 0, len(question_order)


class EtpAssessmentPortalApiController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/portal/<string:token>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def portal_state(self, token, **kwargs):
        evaluator = _get_evaluator(token)
        if not evaluator:
            return return_Response(
                message="Invalid or expired assessment token.",
                status=404,
                data={"state": "invalid"},
            )

        assessment = evaluator.assessment_id
        if assessment.state != "in_progress":
            return return_Response(
                message="Assessment is not active.",
                status=200,
                data={
                    "state": "closed",
                    "assessment": _serialize_assessment_brief(assessment),
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        if evaluator.is_locked:
            return return_Response(
                message="Assessment already submitted.",
                status=200,
                data={
                    "state": "locked",
                    "assessment": _serialize_assessment_brief(assessment),
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        if not evaluator.started_at:
            return return_Response(
                message="Awaiting candidate start.",
                status=200,
                data={
                    "state": "instructions",
                    "assessment": _serialize_assessment_brief(assessment),
                    "evaluator": _serialize_evaluator(evaluator),
                    "duration_minutes": assessment.duration_minutes or 0,
                },
            )

        if evaluator.is_time_expired():
            _auto_submit_remaining(evaluator)
            return return_Response(
                message="Time expired - remaining questions auto-submitted.",
                status=200,
                data={
                    "state": "expired",
                    "assessment": _serialize_assessment_brief(assessment),
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        question, current_index, total = _current_question(evaluator)
        if not question:
            return return_Response(
                message="All questions answered.",
                status=200,
                data={
                    "state": "done",
                    "assessment": _serialize_assessment_brief(assessment),
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        return return_Response(
            message="OK",
            status=200,
            data={
                "state": "question",
                "assessment": _serialize_assessment_brief(assessment),
                "evaluator": _serialize_evaluator(evaluator),
                "question": _serialize_question_for_portal(question),
                "current_index": current_index,
                "total_questions": total,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/portal/<string:token>/begin",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def portal_begin(self, token, **kwargs):
        evaluator = _get_evaluator(token)
        if not evaluator:
            return return_Response(
                message="Invalid or expired assessment token.",
                status=404, data={"state": "invalid"},
            )
        if evaluator.assessment_id.state != "in_progress":
            return return_Response(
                message="Assessment is not active.",
                status=400, data={"state": "closed"},
            )
        if evaluator.is_locked:
            return return_Response(
                message="Assessment already submitted.",
                status=400, data={"state": "locked"},
            )
        if not evaluator.started_at:
            evaluator.write({"started_at": fields.Datetime.now()})

        return return_Response(
            message="Assessment started",
            status=200,
            data={
                "state": "question",
                "evaluator": _serialize_evaluator(evaluator),
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/portal/<string:token>/submit",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def portal_submit(self, token, **kwargs):
        evaluator = _get_evaluator(token)
        if not evaluator:
            return return_Response(
                message="Invalid or expired assessment token.",
                status=404, data={"state": "invalid"},
            )
        if evaluator.is_locked:
            return return_Response(
                message="Assessment already submitted.",
                status=400, data={"state": "locked"},
            )
        if evaluator.is_time_expired():
            _auto_submit_remaining(evaluator)
            return return_Response(
                message="Time expired - remaining questions auto-submitted.",
                status=400,
                data={
                    "state": "expired",
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        body = parse_json_body()
        question_id = coerce_int(body.get("question_id"), 0)
        justification = (body.get("justification") or "").strip()
        selections = body.get("selections") or []

        if not question_id:
            return return_Response(
                message="'question_id' is required", status=400,
            )
        if not justification:
            return return_Response(
                message="'justification' is required", status=400,
            )
        if not isinstance(selections, list) or not selections:
            return return_Response(
                message="'selections' must be a non-empty list of "
                        "{dimension_id, option_id} items.",
                status=400,
            )

        question = (
            request.env["etp.assessment.question"].sudo().browse(question_id)
        )
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        question_order = json.loads(evaluator.question_order or "[]")
        if question_id not in question_order:
            return return_Response(
                message="This question is not assigned to the candidate.",
                status=403,
            )

        Response = request.env["etp.assessment.response"].sudo()
        existing = Response.search([
            ("assessment_evaluator_id", "=", evaluator.id),
            ("question_id", "=", question_id),
        ], limit=1)
        if existing and existing.state == "submitted":
            return return_Response(
                message="This question is already submitted.",
                status=400,
            )

        line_vals = []
        for sel in selections:
            if not isinstance(sel, dict):
                continue
            dim_id = coerce_int(sel.get("dimension_id"), 0)
            opt_id = coerce_int(sel.get("option_id"), 0)
            if not dim_id or not opt_id:
                continue
            line_vals.append((0, 0, {
                "dimension_id": dim_id,
                "selected_option_id": opt_id,
            }))

        if not line_vals:
            return return_Response(
                message="No valid dimension selections supplied.",
                status=400,
            )

        if existing:
            existing.line_ids.unlink()
            existing.write({
                "justification": justification,
                "line_ids": line_vals,
            })
            response = existing
        else:
            response = Response.create({
                "assessment_id": evaluator.assessment_id.id,
                "assessment_evaluator_id": evaluator.id,
                "evaluator_id": evaluator.employee_id.id,
                "question_id": question_id,
                "justification": justification,
                "line_ids": line_vals,
            })

        try:
            response.action_submit()
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        if evaluator.state == "pending":
            evaluator.write({"state": "in_progress"})

        return return_Response(
            message="Response submitted",
            status=200,
            data={
                "evaluator": _serialize_evaluator(evaluator),
                "response_id": response.id,
                "score": response.score or 0,
                "max_score": response.max_score or 0,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/portal/<string:token>/progress",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def portal_progress(self, token, **kwargs):
        evaluator = _get_evaluator(token)
        if not evaluator:
            return return_Response(
                message="Invalid or expired assessment token.",
                status=404, data={"state": "invalid"},
            )

        question_order = json.loads(evaluator.question_order or "[]")
        questions = (
            request.env["etp.assessment.question"].sudo().browse(question_order)
        )
        responses = (
            request.env["etp.assessment.response"]
            .sudo()
            .search([("assessment_evaluator_id", "=", evaluator.id)])
        )
        response_map = {r.question_id.id: r for r in responses}

        items = []
        for idx, q in enumerate(questions):
            resp = response_map.get(q.id)
            items.append({
                "index": idx + 1,
                "question_id": q.id,
                "question_name": q.name or "",
                "status": resp.state if resp else "pending",
                "score": (resp.score or 0) if resp else 0,
                "max_score": (resp.max_score or 0) if resp else 0,
            })

        return return_Response(
            message="OK",
            status=200,
            data={
                "evaluator": _serialize_evaluator(evaluator),
                "total_questions": len(question_order),
                "answered_count": evaluator.answered_count or 0,
                "items": items,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/portal/<string:token>/violation",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def portal_violation(self, token, **kwargs):
        evaluator = _get_evaluator(token)
        if not evaluator:
            return return_Response(
                message="Invalid or expired assessment token.",
                status=404, data={"state": "invalid"},
            )
        if evaluator.is_locked:
            return return_Response(
                message="Assessment already submitted.",
                status=200,
                data={
                    "state": "locked",
                    "evaluator": _serialize_evaluator(evaluator),
                },
            )

        body = parse_json_body()
        reason = (body.get("violation_reason") or "Unknown violation").strip()
        _logger.warning(
            "PORTAL VIOLATION for candidate '%s' (assessment: %s): %s",
            evaluator.employee_id.name,
            evaluator.assessment_id.name,
            reason,
        )

        evaluator.write({
            "is_violated": True,
            "violation_reason": reason,
            "violation_datetime": fields.Datetime.now(),
        })
        _auto_submit_remaining(evaluator, reason=reason)

        return return_Response(
            message="Violation recorded - assessment auto-submitted.",
            status=200,
            data={
                "state": "locked",
                "evaluator": _serialize_evaluator(evaluator),
            },
        )
