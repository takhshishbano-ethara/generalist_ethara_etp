"""Standalone evaluator (candidate assignment) endpoints.

Mirrors the assignment-level operations that exist on `etp.assessment.evaluator`
but were not yet exposed via HTTP: per-candidate detail, invitation resend,
manager unlock, access-token rotation and a by-token lookup helper.

Lives in its own controller because these routes target a candidate
assignment directly (`/evaluators/<id>`) rather than going through an
assessment id in the path.
"""

import logging
import uuid
from datetime import timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    coerce_bool,
    coerce_int,
    parse_json_body,
    pct,
    require_assessment_manager,
    require_assessment_user,
    user_role_tag,
)

_logger = logging.getLogger(__name__)


def _serialize_assignment(rec, state_labels):
    """Same shape as `candidates._serialize_assignment` - kept inline so
    the two controllers don't have a sibling-import cycle."""
    emp = rec.employee_id
    return {
        "id": rec.id,
        "assessment_id": rec.assessment_id.id if rec.assessment_id else 0,
        "assessment_name": (
            rec.assessment_id.name if rec.assessment_id else ""
        ),
        "employee_id": emp.id if emp else 0,
        "employee_name": emp.name if emp else "",
        "employee_email": (emp.work_email or emp.private_email) if emp else "",
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "access_token": rec.access_token or "",
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "deadline_datetime": (
            rec.deadline_datetime.isoformat()
            if rec.deadline_datetime else None
        ),
        "total_questions": rec.total_questions or 0,
        "answered_count": rec.answered_count or 0,
        "progress_percent": pct(rec.answered_count, rec.total_questions),
        "total_score": rec.total_score or 0,
        "max_possible_score": rec.max_possible_score or 0,
        "is_locked": bool(rec.is_locked),
        "is_violated": bool(rec.is_violated),
        "violation_reason": rec.violation_reason or "",
        "violation_datetime": (
            rec.violation_datetime.isoformat()
            if rec.violation_datetime else None
        ),
    }


def _serialize_assessment_brief(a):
    return {
        "id": a.id,
        "name": a.name or "",
        "state": a.state,
        "duration_minutes": a.duration_minutes or 0,
        "start_date": a.start_date.isoformat() if a.start_date else None,
        "end_date": a.end_date.isoformat() if a.end_date else None,
    }


def _build_evaluator_detail(env, assignment):
    """Build the deep detail payload (header + responses + summary)."""
    Evaluator = env["etp.assessment.evaluator"].sudo()
    state_labels = dict(Evaluator._fields["state"].selection)

    Response = env["etp.assessment.response"].sudo()
    resp_state_labels = dict(Response._fields["state"].selection)
    Question = env["etp.assessment.question"].sudo()
    type_labels = dict(Question._fields["question_type"].selection)

    responses = Response.search(
        [("assessment_evaluator_id", "=", assignment.id)],
        order="create_date asc, id asc",
    )

    response_rows = []
    for r in responses:
        q = r.question_id
        lines = []
        for line in r.line_ids:
            lines.append({
                "id": line.id,
                "dimension_id": (
                    line.dimension_id.id if line.dimension_id else 0
                ),
                "dimension_name": (
                    line.dimension_id.name if line.dimension_id else ""
                ),
                "selected_option_id": (
                    line.selected_option_id.id
                    if line.selected_option_id else 0
                ),
                "selected_option_name": (
                    line.selected_option_id.name
                    if line.selected_option_id else ""
                ),
            })
        response_rows.append({
            "id": r.id,
            "question_id": q.id if q else 0,
            "question_name": q.name if q else "",
            "question_type": q.question_type if q else "",
            "question_type_label": (
                type_labels.get(q.question_type or "", "") if q else ""
            ),
            "category_id": q.category_id.id if q and q.category_id else 0,
            "category_name": (
                q.category_id.name if q and q.category_id else ""
            ),
            "justification": r.justification or "",
            "state": r.state,
            "state_label": resp_state_labels.get(r.state, ""),
            "score": r.score or 0,
            "max_score": r.max_score or 0,
            "lines": lines,
            "create_date": (
                r.create_date.isoformat() if r.create_date else None
            ),
        })

    return {
        "role": user_role_tag(env),
        "candidate": _serialize_assignment(assignment, state_labels),
        "assessment": _serialize_assessment_brief(assignment.assessment_id),
        "responses": response_rows,
        "summary": {
            "total_questions": assignment.total_questions or 0,
            "answered_count": assignment.answered_count or 0,
            "total_score": assignment.total_score or 0,
            "max_possible_score": assignment.max_possible_score or 0,
            "progress_percent": pct(
                assignment.answered_count, assignment.total_questions,
            ),
            "is_violated": bool(assignment.is_violated),
            "violation_reason": assignment.violation_reason or "",
        },
    }


class EtpAssessmentEvaluatorController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_evaluator_detail(self, assignment_id, **kwargs):
        """Standalone version of
        `/assessments/<id>/candidates/<employee_id>/detail` - useful when
        the caller only knows the assignment id."""
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        assignment = (
            env["etp.assessment.evaluator"].sudo().browse(assignment_id)
        )
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )

        return return_Response(
            message="OK", status=200, data=_build_evaluator_detail(env, assignment),
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/by_token/<string:token>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_evaluator_by_token(self, token, **kwargs):
        """Manager-only lookup: find an assignment by candidate access_token.
        Used for support / troubleshooting flows."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        assignment = (
            env["etp.assessment.evaluator"]
            .sudo()
            .search([("access_token", "=", (token or "").strip())], limit=1)
        )
        if not assignment:
            return return_Response(
                message="No candidate assignment owns that token.",
                status=404,
            )
        return return_Response(
            message="OK", status=200,
            data=_build_evaluator_detail(env, assignment),
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>/resend_invitation",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def resend_invitation(self, assignment_id, **kwargs):
        """Re-fire `etp_assessment.email_assessment_invitation` for one
        candidate assignment."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )
        if assignment.assessment_id.state != "in_progress":
            return return_Response(
                message=(
                    "Invitations can only be sent while the assessment is "
                    "in progress (current state: "
                    f"{assignment.assessment_id.state})."
                ),
                status=400,
            )

        template = env.ref(
            "etp_assessment.email_assessment_invitation",
            raise_if_not_found=False,
        )
        if not template:
            return return_Response(
                message=(
                    "Email template 'etp_assessment.email_assessment_invitation' "
                    "is missing. Re-install or upgrade the etp_assessment module."
                ),
                status=500,
            )

        emp = assignment.employee_id
        recipient_email = (
            (emp.work_email or emp.private_email)
            if emp else ""
        )
        if not recipient_email and emp and emp.user_id:
            recipient_email = emp.user_id.email or ""
        if not recipient_email:
            return return_Response(
                message=(
                    "Candidate has no email (work_email, private_email and "
                    "user email are all empty)."
                ),
                status=400,
            )

        try:
            template.send_mail(
                assignment.id,
                force_send=True,
                raise_exception=True,
                email_values={"email_to": recipient_email},
            )
        except Exception:
            _logger.exception(
                "Failed to re-send invitation to %s (assignment %s)",
                recipient_email, assignment.id,
            )
            return return_Response(
                message=(
                    "Failed to send invitation. Check server logs for "
                    "the underlying mail-transport error."
                ),
                status=500,
            )

        state_labels = dict(Evaluator._fields["state"].selection)
        return return_Response(
            message="Invitation email re-sent.",
            status=200,
            data={
                "evaluator": _serialize_assignment(assignment, state_labels),
                "recipient_email": recipient_email,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>/unlock",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def unlock_evaluator(self, assignment_id, **kwargs):
        """Manager override that lets a candidate resume taking the assessment.

        What this does:
          1. Clears `is_locked` on the assignment.
          2. Deletes auto-submitted responses (justification starts with
             `[Auto-submitted:`). These come from timeout / violation
             auto-submits in `portal.py` and the base portal - they are
             system-generated, not real candidate input. Removing them
             re-opens those questions for the candidate.
          3. Resets the assignment state to `in_progress` if it was
             `submitted`.
          4. Reopens the parent assessment (back to `in_progress`) if it
             was auto-marked `done` by `_check_all_submitted` /
             `_check_assessment_complete`.

        What this does NOT do:
          - Does NOT delete manually-submitted responses (the candidate's
            real answers).
          - Does NOT clear `is_violated` / `violation_reason` /
            `violation_datetime` - the audit trail stays intact. Use a
            separate endpoint if you need to clear those.

        After this call, the candidate can hit `/portal/<token>` again and
        the portal will show the next un-answered question (if any) or
        `done` if every question still has a real submitted response.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )

        Response = env["etp.assessment.response"].sudo()
        auto_submitted = Response.search([
            ("assessment_evaluator_id", "=", assignment.id),
            ("state", "=", "submitted"),
            ("justification", "=like", "[Auto-submitted:%"),
        ])
        deleted_count = len(auto_submitted)
        if auto_submitted:
            auto_submitted.unlink()

        update = {"is_locked": False}
        if assignment.state == "submitted":
            update["state"] = "in_progress"
        assignment.write(update)

        reopened_assessment = False
        parent = assignment.assessment_id
        if parent and parent.state == "done":
            still_open = any(
                a.state != "submitted"
                for a in parent.assessment_evaluator_ids
            )
            if still_open:
                parent.write({"state": "in_progress"})
                reopened_assessment = True

        state_labels = dict(Evaluator._fields["state"].selection)
        return return_Response(
            message="Candidate assignment unlocked.",
            status=200,
            data={
                "evaluator": _serialize_assignment(assignment, state_labels),
                "deleted_auto_submitted_count": deleted_count,
                "reopened_assessment": reopened_assessment,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>/regenerate_token",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def regenerate_token(self, assignment_id, **kwargs):
        """Rotate the candidate's `access_token` so the old portal link stops
        working. Returns the new token so the admin can copy / re-share it."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )

        new_token = str(uuid.uuid4())
        assignment.write({"access_token": new_token})

        state_labels = dict(Evaluator._fields["state"].selection)
        return return_Response(
            message="Access token regenerated.",
            status=200,
            data={
                "evaluator": _serialize_assignment(assignment, state_labels),
                "new_access_token": new_token,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>/clear_violation",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def clear_violation(self, assignment_id, **kwargs):
        """Manager-only: clears `is_violated`, `violation_reason`, and
        `violation_datetime` on the assignment.

        Complements `/unlock`. Use this when a violation was a false
        positive (browser quirk, accidental tab-switch by an honest
        candidate) and the audit trail should be cleaned up.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )

        if not assignment.is_violated:
            return return_Response(
                message="Assignment has no recorded violation to clear.",
                status=400,
            )

        assignment.write({
            "is_violated": False,
            "violation_reason": False,
            "violation_datetime": False,
        })

        state_labels = dict(Evaluator._fields["state"].selection)
        return return_Response(
            message="Violation cleared.",
            status=200,
            data={
                "evaluator": _serialize_assignment(assignment, state_labels),
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/evaluators/<int:assignment_id>/extend_deadline",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def extend_deadline(self, assignment_id, **kwargs):
        """Manager-only: extend the candidate's deadline.

        `deadline_datetime` is computed as `started_at + duration_minutes`,
        so this endpoint moves `started_at` to give the candidate more
        time. Two modes:

        - `reset_started_at=true` (recommended for "give them a fresh
          start"): sets `started_at` to now(), giving the candidate the
          full `duration_minutes` from now.
        - `minutes_to_add=<N>` (numeric): pushes `started_at` forward by
          N minutes, which moves `deadline_datetime` forward by N
          minutes. Requires the candidate to have already started.

        If both supplied, `reset_started_at` wins.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )

        body = parse_json_body()
        reset = coerce_bool(body.get("reset_started_at"), False)
        minutes_to_add = coerce_int(body.get("minutes_to_add"), 0)

        if reset:
            assignment.write({"started_at": fields.Datetime.now()})
        elif minutes_to_add > 0:
            if not assignment.started_at:
                return return_Response(
                    message=(
                        "Candidate has not started yet - use "
                        "`reset_started_at: true` or wait for the "
                        "candidate to begin."
                    ),
                    status=400,
                )
            new_started_at = (
                assignment.started_at + timedelta(minutes=minutes_to_add)
            )
            assignment.write({"started_at": new_started_at})
        else:
            return return_Response(
                message=(
                    "Either `reset_started_at: true` or "
                    "`minutes_to_add: <positive int>` must be supplied."
                ),
                status=400,
            )

        state_labels = dict(Evaluator._fields["state"].selection)
        return return_Response(
            message="Deadline extended.",
            status=200,
            data={
                "evaluator": _serialize_assignment(assignment, state_labels),
            },
        )
