"""Candidate self-service REST API (gateway-token-auth).

Endpoints that the logged-in candidate (Flutter SCR-092 / SCR-093 / SCR-094)
calls to view their own assessments, fetch the day workspace, autosave draft
responses, flag questions, and submit a day.

Auth model: gateway token via `@validate_token`. The caller is identified by
`request.env.user.employee_id` and is only ever served their own data — no
manager / evaluator role gate is applied.
"""

import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_request,
    validate_token,
)

from .common import coerce_bool, coerce_int, parse_json_body, pct

_logger = logging.getLogger(__name__)

_PASS_THRESHOLD = 70.0


def _current_employee():
    """Return the caller's `hr.employee` or a 400 Response."""
    employee = request.env.user.employee_id
    if not employee:
        return None, return_Response(
            message="No employee profile is linked to your user account.",
            status=400,
        )
    return employee, None


def _question_day_index(question):
    """Best-effort day index for a question.

    Mirrors the defensive `getattr` used by `approve_assessment_day`. Returns
    None when the model has no `day_index` field.
    """
    day = getattr(question, "day_index", False)
    return day if day else None


def _day_questions(assessment, day_index, fallback_split=True):
    """Return the questions belonging to `day_index` (1-indexed).

    Strategy:
      1. If any question on the assessment has a populated `day_index` field,
         filter by it.
      2. Otherwise, when `fallback_split=True`, partition the bank evenly
         across `total_days` so day 1 gets the first slice, day 2 the next,
         etc. This keeps the candidate flow working even when the assessment
         model doesn't model days explicitly yet.
    """
    questions = assessment.question_ids
    has_day_field = any(_question_day_index(q) for q in questions)
    if has_day_field:
        return questions.filtered(
            lambda q: _question_day_index(q) == day_index
        )

    if not fallback_split:
        return questions.browse([])

    total_days = max(1, coerce_int(getattr(assessment, "total_days", 0), 1))
    if day_index < 1 or day_index > total_days:
        return questions.browse([])

    sorted_questions = questions.sorted("id")
    n = len(sorted_questions)
    if n == 0:
        return questions.browse([])

    per_day = max(1, n // total_days)
    start = (day_index - 1) * per_day
    end = start + per_day if day_index < total_days else n
    return sorted_questions[start:end]


def _serialize_assessment_card(a):
    return {
        "id": a.id,
        "name": a.name or "",
        "state": a.state,
        "window_start": (
            a.start_date.isoformat()
            if getattr(a, "start_date", False) else None
        ),
        "window_end": (
            a.end_date.isoformat()
            if getattr(a, "end_date", False) else None
        ),
        "duration_minutes": a.duration_minutes or 0,
        "total_days": coerce_int(getattr(a, "total_days", 0), 1) or 1,
        "questions_per_day": coerce_int(
            getattr(a, "questions_per_day", 0),
            len(a.question_ids) or 0,
        ),
        "daily_window_start": getattr(a, "daily_window_start", "") or "",
        "daily_window_end": getattr(a, "daily_window_end", "") or "",
    }


def _assignment_for_employee(employee, states=None):
    """Return the candidate's most-recent assignment, optionally state-filtered."""
    domain = [("employee_id", "=", employee.id)]
    if states:
        domain.append(("state", "in", list(states)))
    return (
        request.env["etp.assessment.evaluator"]
        .sudo()
        .search(domain, order="id desc", limit=1)
    )


def _day_status_for_candidate(assignment, day_index):
    """Derive an SCR-092 day-card status (approved/active/upcoming).

    - `approved` when every day-question has a submitted response
    - `active` when there is at least one draft / submitted response for the
      day or it is the next day to attempt
    - `upcoming` otherwise
    """
    assessment = assignment.assessment_id
    day_qs = _day_questions(assessment, day_index)
    if not day_qs:
        return "upcoming", 0, 0

    Response = request.env["etp.assessment.response"].sudo()
    submitted = Response.search_count([
        ("assessment_evaluator_id", "=", assignment.id),
        ("question_id", "in", day_qs.ids),
        ("state", "=", "submitted"),
    ])
    drafted = Response.search_count([
        ("assessment_evaluator_id", "=", assignment.id),
        ("question_id", "in", day_qs.ids),
        ("state", "=", "draft"),
    ])

    if submitted >= len(day_qs):
        return "approved", submitted, len(day_qs)
    if submitted + drafted > 0:
        return "active", submitted + drafted, len(day_qs)
    return "upcoming", 0, len(day_qs)


def _day_score(assignment, day_qs):
    """Aggregate score across the day's submitted responses."""
    if not day_qs:
        return 0, 0
    Response = request.env["etp.assessment.response"].sudo()
    submitted = Response.search([
        ("assessment_evaluator_id", "=", assignment.id),
        ("question_id", "in", day_qs.ids),
        ("state", "=", "submitted"),
    ])
    total = sum(r.score or 0 for r in submitted)
    max_total = sum(r.max_score or 0 for r in submitted)
    return total, max_total


def _serialize_day_card(assignment, day_index):
    assessment = assignment.assessment_id
    day_qs = _day_questions(assessment, day_index)
    status, answered, total = _day_status_for_candidate(assignment, day_index)
    score, max_score = _day_score(assignment, day_qs)
    score_pct = pct(score, max_score) if max_score else None

    type_labels = {
        "image_comparison": "Eval Compare",
        "image_text": "Mixed set",
        "text": "Prompt Writing",
        "coding": "Coding",
        "video": "Video review",
    }
    types = [
        type_labels.get(q.question_type or "", q.question_type or "")
        for q in day_qs
    ]
    type_summary = " · ".join(sorted({t for t in types if t})) or "Mixed set"

    return {
        "index": day_index,
        "label": f"Day {day_index} · {type_summary}",
        "summary": (
            f"{total} questions" if status != "approved"
            else f"{total} questions · submitted"
        ),
        "status": status,
        "answered": answered,
        "total": total,
        "score": score_pct,
    }


def _serialize_past_assessment(assignment):
    a = assignment.assessment_id
    total_score = assignment.total_score or 0
    max_score = assignment.max_possible_score or 0
    return {
        "id": a.id,
        "code": a.name or f"ASM-{a.id:04d}",
        "name": a.name or "",
        "window_start": (
            a.start_date.isoformat()
            if getattr(a, "start_date", False) else None
        ),
        "window_end": (
            a.end_date.isoformat()
            if getattr(a, "end_date", False) else None
        ),
        "score": pct(total_score, max_score) if max_score else None,
        "passed": (
            (pct(total_score, max_score) >= _PASS_THRESHOLD)
            if max_score else None
        ),
        "submitted_at": (
            assignment.write_date.isoformat()
            if assignment.write_date else None
        ),
    }


# ---------------------------------------------------------------------------
# Question / draft serializers
# ---------------------------------------------------------------------------


def _serialize_workspace_question(q):
    """Lightweight question payload for the day workspace palette."""
    return {
        "id": q.id,
        "name": q.name or "",
        "question_type": q.question_type or "",
        "prompt": q.prompt or "",
        "description": q.description or "",
        "image_a_url": q.image_a_url or "",
        "image_b_url": q.image_b_url or "",
        "has_image_a": bool(q.image_a),
        "has_image_b": bool(q.image_b),
        "code_snippet": q.code_snippet or "",
        "code_language": q.code_language or "",
        "video_url": q.video_url or "",
        "dimensions": [
            {
                "dimension_id": qd.dimension_id.id,
                "name": qd.dimension_id.name or "",
                "sequence": qd.sequence or 0,
                "options": [
                    {
                        "id": opt.id,
                        "name": opt.name or "",
                        "sequence": opt.sequence or 0,
                    }
                    for opt in qd.dimension_id.option_ids.sorted("sequence")
                ],
            }
            for qd in q.question_dimension_ids.sorted("sequence")
            if qd.dimension_id
        ],
    }


def _serialize_response_draft(response):
    """Per-question draft envelope returned to the workspace.

    Stores the candidate's selections + free-text justification + (optionally)
    a structured `draft_payload` JSON for prompt / bbox question types. The
    `draft_payload` lives in the response's `justification` when no dedicated
    column exists yet (encoded as JSON string with a `__draft__` marker).
    """
    payload = {}
    justification = response.justification or ""
    if justification.startswith("__draft__:"):
        try:
            payload = json.loads(justification[len("__draft__:"):])
            justification = ""
        except Exception:
            payload = {}

    return {
        "question_id": response.question_id.id,
        "state": response.state,
        "justification": justification,
        "draft_payload": payload,
        "selections": [
            {
                "dimension_id": line.dimension_id.id,
                "option_id": (
                    line.selected_option_id.id
                    if line.selected_option_id else None
                ),
            }
            for line in response.line_ids
            if line.dimension_id
        ],
        "score": response.score or 0,
        "max_score": response.max_score or 0,
    }


def _encode_draft_justification(justification, draft_payload):
    """Pack non-eval-compare drafts (prompt / bbox) into `justification`.

    When the candidate is writing a prompt or drawing bboxes the response
    has no `selections` rows, so we keep the structured draft in the only
    free-text column that exists today.
    """
    if draft_payload:
        return f"__draft__:{json.dumps(draft_payload, default=str)}"
    return justification or ""


def _upsert_response(assignment, question_id, *, draft_payload, selections,
                     justification, submit):
    Response = request.env["etp.assessment.response"].sudo()
    existing = Response.search([
        ("assessment_evaluator_id", "=", assignment.id),
        ("question_id", "=", question_id),
    ], limit=1)

    if existing and existing.state == "submitted":
        return existing, return_Response(
            message="This question is already submitted.",
            status=400,
        )

    line_vals = []
    for sel in selections or []:
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

    stored_justification = _encode_draft_justification(
        justification, draft_payload,
    )

    if existing:
        existing.line_ids.unlink()
        existing.write({
            "justification": stored_justification,
            "line_ids": line_vals,
            "state": "draft",
        })
        response = existing
    else:
        response = Response.create({
            "assessment_id": assignment.assessment_id.id,
            "assessment_evaluator_id": assignment.id,
            "evaluator_id": assignment.employee_id.id,
            "question_id": question_id,
            "justification": stored_justification,
            "line_ids": line_vals,
            "state": "draft",
        })

    if submit:
        try:
            response.action_submit()
        except (UserError, ValidationError) as exc:
            return response, return_Response(
                message=str(exc.args[0] if exc.args else exc),
                status=400,
            )
        if assignment.state == "pending":
            assignment.write({"state": "in_progress"})

    return response, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class EtpCandidateSelfController(http.Controller):
    """All endpoints are scoped to the calling candidate."""

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/current-assessment",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def current_assessment(self, **kwargs):
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment:
            return return_Response(
                message="No active assessment for this candidate.",
                status=200,
                data={"assessment": None, "days": [], "assignment": None},
            )

        assessment = assignment.assessment_id
        total_days = coerce_int(getattr(assessment, "total_days", 0), 1) or 1
        days = [
            _serialize_day_card(assignment, idx)
            for idx in range(1, total_days + 1)
        ]
        completed = sum(1 for d in days if d["status"] == "approved")

        return return_Response(
            message="OK",
            status=200,
            data={
                "assessment": _serialize_assessment_card(assessment),
                "days": days,
                "progress": {
                    "completed_days": completed,
                    "total_days": total_days,
                    "progress_pct": pct(completed, total_days),
                },
                "assignment": {
                    "id": assignment.id,
                    "state": assignment.state,
                    "is_locked": bool(assignment.is_locked),
                    "answered_count": assignment.answered_count or 0,
                    "total_questions": assignment.total_questions or 0,
                    "deadline": (
                        assignment.deadline_datetime.isoformat()
                        if assignment.deadline_datetime else None
                    ),
                },
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/past-assessments",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def past_assessments(self, **kwargs):
        employee, err = _current_employee()
        if err is not None:
            return err

        assignments = (
            request.env["etp.assessment.evaluator"]
            .sudo()
            .search([
                ("employee_id", "=", employee.id),
                ("state", "=", "submitted"),
            ], order="id desc")
        )
        rows = [_serialize_past_assessment(a) for a in assignments]
        return return_Response(
            message="OK",
            status=200,
            data={"past": rows, "total": len(rows)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/past-assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def past_assessment_detail(self, assessment_id, **kwargs):
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = (
            request.env["etp.assessment.evaluator"]
            .sudo()
            .search([
                ("employee_id", "=", employee.id),
                ("assessment_id", "=", assessment_id),
            ], limit=1, order="id desc")
        )
        if not assignment:
            return return_Response(
                message="Past assessment not found.", status=404,
            )

        assessment = assignment.assessment_id
        total_days = coerce_int(getattr(assessment, "total_days", 0), 1) or 1
        days = [
            _serialize_day_card(assignment, idx)
            for idx in range(1, total_days + 1)
        ]
        return return_Response(
            message="OK",
            status=200,
            data={
                "assessment": _serialize_assessment_card(assessment),
                "days": days,
                "score": pct(
                    assignment.total_score or 0,
                    assignment.max_possible_score or 0,
                ) if (assignment.max_possible_score or 0) else None,
                "submitted_at": (
                    assignment.write_date.isoformat()
                    if assignment.write_date else None
                ),
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/day/<int:day_index>/questions",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def day_questions(self, day_index, **kwargs):
        employee, err = _current_employee()
        if err is not None:
            return err
        if day_index < 1:
            return return_Response(
                message="day_index must be >= 1.", status=400,
            )

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment:
            return return_Response(
                message="No active assessment for this candidate.",
                status=404,
            )

        assessment = assignment.assessment_id
        day_qs = _day_questions(assessment, day_index)
        if not day_qs:
            return return_Response(
                message=f"No questions configured for day {day_index}.",
                status=404,
            )

        # Existing responses keyed by question id
        Response = request.env["etp.assessment.response"].sudo()
        existing = Response.search([
            ("assessment_evaluator_id", "=", assignment.id),
            ("question_id", "in", day_qs.ids),
        ])
        drafts_by_qid = {r.question_id.id: r for r in existing}

        # Time left = whole-assessment timer when configured, else None
        time_left = None
        if assignment.deadline_datetime:
            delta = (
                assignment.deadline_datetime
                - fields.Datetime.now()
            )
            time_left = max(0, int(delta.total_seconds()))

        flagged_payload = json.loads(
            getattr(assignment, "flagged_question_ids", False) or "[]"
        ) if hasattr(assignment, "flagged_question_ids") else []

        return return_Response(
            message="OK",
            status=200,
            data={
                "day_index": day_index,
                "total_questions": len(day_qs),
                "time_left_seconds": time_left,
                "questions": [
                    _serialize_workspace_question(q) for q in day_qs
                ],
                "drafts": [
                    _serialize_response_draft(drafts_by_qid[qid])
                    for qid in drafts_by_qid
                ],
                "flagged_ids": flagged_payload,
                "assignment": {
                    "id": assignment.id,
                    "state": assignment.state,
                    "is_locked": bool(assignment.is_locked),
                },
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/day/<int:day_index>/autosave",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "question_id": {"type": "integer", "required": True, "minimum": 1},
    })
    def day_autosave(self, day_index, **kwargs):
        body = (kwargs.get("jdata") or {})
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment or assignment.is_locked:
            return return_Response(
                message="Assignment not available for autosave.", status=400,
            )

        question_id = coerce_int(body.get("question_id"), 0)
        if not question_id:
            return return_Response(
                message="'question_id' is required.", status=400,
            )

        # Question must belong to the requested day.
        assessment = assignment.assessment_id
        day_qs = _day_questions(assessment, day_index)
        if question_id not in day_qs.ids:
            return return_Response(
                message="Question does not belong to this day.", status=403,
            )

        response, err = _upsert_response(
            assignment,
            question_id,
            draft_payload=body.get("draft_payload") or {},
            selections=body.get("selections") or [],
            justification=(body.get("justification") or "").strip(),
            submit=False,
        )
        if err is not None:
            return err

        return return_Response(
            message="Saved",
            status=200,
            data={
                "saved_at": fields.Datetime.now().isoformat(),
                "response": _serialize_response_draft(response),
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/day/<int:day_index>/submit-question",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "question_id": {"type": "integer", "required": True, "minimum": 1},
    })
    def day_submit_question(self, day_index, **kwargs):
        body = (kwargs.get("jdata") or {})
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment or assignment.is_locked:
            return return_Response(
                message="Assignment not available for submit.", status=400,
            )

        question_id = coerce_int(body.get("question_id"), 0)
        if not question_id:
            return return_Response(
                message="'question_id' is required.", status=400,
            )
        assessment = assignment.assessment_id
        if question_id not in _day_questions(assessment, day_index).ids:
            return return_Response(
                message="Question does not belong to this day.", status=403,
            )

        response, err = _upsert_response(
            assignment,
            question_id,
            draft_payload=body.get("draft_payload") or {},
            selections=body.get("selections") or [],
            justification=(body.get("justification") or "").strip(),
            submit=True,
        )
        if err is not None:
            return err

        return return_Response(
            message="Response submitted",
            status=200,
            data={"response": _serialize_response_draft(response)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/day/<int:day_index>/submit",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def day_submit(self, day_index, **kwargs):
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment or assignment.is_locked:
            return return_Response(
                message="Assignment not available for submit.", status=400,
            )

        assessment = assignment.assessment_id
        day_qs = _day_questions(assessment, day_index)
        if not day_qs:
            return return_Response(
                message=f"No questions configured for day {day_index}.",
                status=404,
            )

        Response = request.env["etp.assessment.response"].sudo()
        submitted_count = 0
        for q in day_qs:
            draft = Response.search([
                ("assessment_evaluator_id", "=", assignment.id),
                ("question_id", "=", q.id),
                ("state", "=", "draft"),
            ], limit=1)
            if not draft:
                continue
            try:
                draft.action_submit()
                submitted_count += 1
            except (UserError, ValidationError):
                continue

        # If every question across the assignment is now submitted, lock it.
        total_qs = len(assessment.question_ids)
        all_submitted = Response.search_count([
            ("assessment_evaluator_id", "=", assignment.id),
            ("state", "=", "submitted"),
        ])
        if total_qs and all_submitted >= total_qs:
            assignment.write({"state": "submitted", "is_locked": True})

        return return_Response(
            message=f"Day {day_index} submitted ({submitted_count} response(s)).",
            status=200,
            data={
                "day_index": day_index,
                "submitted": submitted_count,
                "assignment_state": assignment.state,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/candidate/me/day/<int:day_index>/flag",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "question_id": {"type": "integer", "required": True, "minimum": 1},
    })
    def day_flag(self, day_index, **kwargs):
        body = (kwargs.get("jdata") or {})
        employee, err = _current_employee()
        if err is not None:
            return err

        assignment = _assignment_for_employee(
            employee, states=("pending", "in_progress"),
        )
        if not assignment:
            return return_Response(
                message="Assignment not available.", status=400,
            )

        question_id = coerce_int(body.get("question_id"), 0)
        flagged = coerce_bool(body.get("flagged"), True)
        if not question_id:
            return return_Response(
                message="'question_id' is required.", status=400,
            )

        # Store the flagged set on the assignment if the model has it, else
        # silently no-op so the candidate flow keeps working.
        if hasattr(assignment, "flagged_question_ids"):
            try:
                current = set(json.loads(
                    assignment.flagged_question_ids or "[]"
                ))
            except Exception:
                current = set()
            if flagged:
                current.add(question_id)
            else:
                current.discard(question_id)
            assignment.write({
                "flagged_question_ids": json.dumps(sorted(current)),
            })

        return return_Response(
            message="OK",
            status=200,
            data={"question_id": question_id, "flagged": bool(flagged)},
        )
