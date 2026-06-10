"""Response listing + per-dimension analytics endpoints."""

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    RESPONSE_STATES,
    coerce_int,
    paginate,
    pagination_block,
    parse_json_body,
    pct,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
)

RESPONSE_COLUMNS = [
    {"key": "evaluator_name", "label": "Candidate", "type": "string"},
    {"key": "assessment_name", "label": "Assessment", "type": "string"},
    {"key": "question_name", "label": "Question", "type": "string"},
    {"key": "state_label", "label": "State", "type": "string"},
    {"key": "score", "label": "Score", "type": "integer"},
    {"key": "max_score", "label": "Max", "type": "integer"},
    {"key": "create_date", "label": "Submitted", "type": "datetime"},
]

DIMENSION_ANALYTICS_COLUMNS = [
    {"key": "name", "label": "Dimension", "type": "string"},
    {"key": "total", "label": "Total", "type": "integer"},
    {"key": "correct", "label": "Correct", "type": "integer"},
    {"key": "accuracy", "label": "Accuracy %", "type": "float"},
]

SORT_FIELDS = {
    "create_date": "create_date",
    "score": "score",
    "state": "state",
}


def _serialize_response_line(line):
    return {
        "id": line.id,
        "dimension_id": line.dimension_id.id if line.dimension_id else 0,
        "dimension_name": line.dimension_id.name if line.dimension_id else "",
        "selected_option_id": (
            line.selected_option_id.id if line.selected_option_id else 0
        ),
        "selected_option_name": (
            line.selected_option_id.name if line.selected_option_id else ""
        ),
    }


def _serialize_response(rec, state_labels):
    return {
        "id": rec.id,
        "assessment_id": rec.assessment_id.id if rec.assessment_id else 0,
        "assessment_name": rec.assessment_id.name if rec.assessment_id else "",
        "assessment_evaluator_id": (
            rec.assessment_evaluator_id.id if rec.assessment_evaluator_id else 0
        ),
        "evaluator_id": rec.evaluator_id.id if rec.evaluator_id else 0,
        "evaluator_name": rec.evaluator_id.name if rec.evaluator_id else "",
        "question_id": rec.question_id.id if rec.question_id else 0,
        "question_name": rec.question_id.name if rec.question_id else "",
        "justification": rec.justification or "",
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "score": rec.score or 0,
        "max_score": rec.max_score or 0,
        "lines": [_serialize_response_line(l) for l in rec.line_ids],
        "create_date": rec.create_date.isoformat() if rec.create_date else None,
        "write_date": rec.write_date.isoformat() if rec.write_date else None,
    }


def _build_response_domain(params):
    domain = []
    assessment_id = coerce_int(params.get("assessment_id"), 0)
    if assessment_id:
        domain.append(("assessment_id", "=", assessment_id))
    evaluator_id = coerce_int(params.get("evaluator_id"), 0)
    if evaluator_id:
        domain.append(("evaluator_id", "=", evaluator_id))
    assignment_id = coerce_int(params.get("assignment_id"), 0)
    if assignment_id:
        domain.append(("assessment_evaluator_id", "=", assignment_id))
    question_id = coerce_int(params.get("question_id"), 0)
    if question_id:
        domain.append(("question_id", "=", question_id))
    state = (params.get("state") or "").strip()
    if state:
        if state not in RESPONSE_STATES:
            return None, return_Response(
                message=(
                    f"Invalid state '{state}'. "
                    f"Allowed: {', '.join(RESPONSE_STATES)}."
                ),
                status=400,
            )
        domain.append(("state", "=", state))
    return domain, None


class EtpAssessmentResponseController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/responses",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_responses(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain, error = _build_response_domain(params)
        if error is not None:
            return error
        order, error = resolve_order(params, SORT_FIELDS, "create_date", "desc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Response = env["etp.assessment.response"].sudo()
        total = Response.search_count(domain)
        records = Response.search(domain, limit=limit, offset=offset, order=order)
        state_labels = dict(Response._fields["state"].selection)
        rows = [_serialize_response(r, state_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Responses",
                    "columns": RESPONSE_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/responses/<int:response_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_response(self, response_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        Response = request.env["etp.assessment.response"].sudo()
        record = Response.browse(response_id)
        if not record.exists():
            return return_Response(message="Response not found", status=404)
        state_labels = dict(Response._fields["state"].selection)
        return return_Response(
            message="OK",
            status=200,
            data={"response": _serialize_response(record, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/analytics/dimensions",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def dimension_analytics(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        assessment_id = coerce_int(params.get("assessment_id"), 0)

        ResponseLine = env["etp.assessment.response.line"].sudo()
        line_domain = [("response_id.state", "=", "submitted")]
        if assessment_id:
            line_domain.append(("response_id.assessment_id", "=", assessment_id))

        lines = ResponseLine.search(line_domain)
        if not lines:
            return return_Response(
                message="OK",
                status=200,
                data={
                    "role": user_role_tag(env),
                    "blocks": [{
                        "type": "table",
                        "title": "Per-dimension accuracy",
                        "columns": DIMENSION_ANALYTICS_COLUMNS,
                        "rows": [],
                    }],
                },
            )

        QDimOption = env["etp.assessment.question.dimension.option"].sudo()

        agg = {}
        for line in lines:
            dim = line.dimension_id
            if not dim:
                continue
            bucket = agg.setdefault(
                dim.id,
                {"name": dim.name or "", "total": 0, "correct": 0},
            )
            bucket["total"] += 1
            if not line.selected_option_id:
                continue
            correct_opt = QDimOption.search([
                ("question_dimension_id.question_id", "=", line.response_id.question_id.id),
                ("question_dimension_id.dimension_id", "=", dim.id),
                ("is_correct", "=", True),
            ], limit=1)
            if (
                correct_opt
                and correct_opt.master_option_id.id == line.selected_option_id.id
            ):
                bucket["correct"] += 1

        rows = []
        for dim_id, bucket in agg.items():
            rows.append({
                "dimension_id": dim_id,
                "name": bucket["name"],
                "total": bucket["total"],
                "correct": bucket["correct"],
                "accuracy": pct(bucket["correct"], bucket["total"]),
            })
        rows.sort(key=lambda r: r["accuracy"], reverse=True)

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Per-dimension accuracy",
                    "columns": DIMENSION_ANALYTICS_COLUMNS,
                    "rows": rows,
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/responses",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def create_response(self, **kwargs):
        """Admin creates a draft response on behalf of a candidate.

        Body (JSON):

        - `assessment_evaluator_id` (int, required) - the candidate
          assignment that owns this response.
        - `question_id` (int, required) - the question being answered.
        - `justification` (str, optional) - free text.
        - `selections` (list, optional) - list of
          `{dimension_id, option_id}` to fill `line_ids`. If omitted, the
          response is created with no dimension lines and the admin can
          PUT later before submitting.

        Returns the freshly-created response with `state=draft`.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        body = parse_json_body()
        assignment_id = coerce_int(body.get("assessment_evaluator_id"), 0)
        question_id = coerce_int(body.get("question_id"), 0)
        if not assignment_id or not question_id:
            return return_Response(
                message=(
                    "`assessment_evaluator_id` and `question_id` are both "
                    "required."
                ),
                status=400,
            )

        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )
        if assignment.is_locked:
            return return_Response(
                message=(
                    "Candidate assignment is locked - unlock it first or "
                    "create the response on a different candidate."
                ),
                status=400,
            )

        Question = env["etp.assessment.question"].sudo()
        question = Question.browse(question_id)
        if not question.exists():
            return return_Response(
                message="Question not found", status=404,
            )

        line_vals = []
        selections = body.get("selections") or []
        if isinstance(selections, list):
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

        Response = env["etp.assessment.response"].sudo()
        try:
            record = Response.create({
                "assessment_id": assignment.assessment_id.id,
                "assessment_evaluator_id": assignment.id,
                "evaluator_id": assignment.employee_id.id,
                "question_id": question.id,
                "justification": body.get("justification") or "",
                "line_ids": line_vals,
            })
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc),
                status=400,
            )

        state_labels = dict(Response._fields["state"].selection)
        return return_Response(
            message="Response created",
            status=200,
            data={"response": _serialize_response(record, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/responses/<int:response_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_response(self, response_id, **kwargs):
        """Admin updates a draft response - justification + selections.

        Rejects requests when the response is `submitted` (callers must
        call `/reset_draft` first) or the parent assignment is locked.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Response = request.env["etp.assessment.response"].sudo()
        record = Response.browse(response_id)
        if not record.exists():
            return return_Response(message="Response not found", status=404)
        if record.state == "submitted":
            return return_Response(
                message=(
                    "Cannot edit a submitted response - call "
                    "`/responses/<id>/reset_draft` first."
                ),
                status=400,
            )
        if (
            record.assessment_evaluator_id
            and record.assessment_evaluator_id.is_locked
        ):
            return return_Response(
                message=(
                    "Candidate assignment is locked - unlock it before "
                    "editing this response."
                ),
                status=400,
            )

        body = parse_json_body()
        vals = {}
        if "justification" in body:
            vals["justification"] = body.get("justification") or ""

        selections = body.get("selections")
        if isinstance(selections, list):
            record.line_ids.unlink()
            new_lines = []
            for sel in selections:
                if not isinstance(sel, dict):
                    continue
                dim_id = coerce_int(sel.get("dimension_id"), 0)
                opt_id = coerce_int(sel.get("option_id"), 0)
                if not dim_id or not opt_id:
                    continue
                new_lines.append((0, 0, {
                    "dimension_id": dim_id,
                    "selected_option_id": opt_id,
                }))
            if new_lines:
                vals["line_ids"] = new_lines

        if vals:
            try:
                record.write(vals)
            except (UserError, ValidationError) as exc:
                return return_Response(
                    message=str(exc.args[0] if exc.args else exc),
                    status=400,
                )

        state_labels = dict(Response._fields["state"].selection)
        return return_Response(
            message="Response updated",
            status=200,
            data={"response": _serialize_response(record, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/responses/<int:response_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_response(self, response_id, **kwargs):
        """Admin deletes a response (any state). The candidate's progress
        recomputes automatically because `answered_count` /
        `total_score` are stored computed fields on the assignment."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        record = (
            request.env["etp.assessment.response"].sudo().browse(response_id)
        )
        if not record.exists():
            return return_Response(message="Response not found", status=404)
        try:
            record.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete response: {exc}", status=400,
            )
        return return_Response(message="Response deleted", status=200)

    @http.route(
        "/api/v1/etp_assessment_ext/responses/<int:response_id>/reset_draft",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def reset_response_draft(self, response_id, **kwargs):
        """Revert a `submitted` response back to `draft` so it can be
        edited or re-submitted. Manager-only.

        Note: if the parent assignment was previously auto-locked via
        `_check_all_submitted`, that lock stays in place - call
        `/evaluators/<id>/unlock` separately if you also need to clear
        the lock.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Response = request.env["etp.assessment.response"].sudo()
        record = Response.browse(response_id)
        if not record.exists():
            return return_Response(message="Response not found", status=404)
        if record.state != "submitted":
            return return_Response(
                message=(
                    f"Response is already in state '{record.state}' - "
                    "nothing to reset."
                ),
                status=400,
            )
        if (
            record.assessment_evaluator_id
            and record.assessment_evaluator_id.is_locked
        ):
            return return_Response(
                message=(
                    "Candidate assignment is locked - unlock it before "
                    "resetting this response (the model rejects "
                    "non-submitted state on locked assignments)."
                ),
                status=400,
            )
        try:
            record.write({"state": "draft"})
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc),
                status=400,
            )

        state_labels = dict(Response._fields["state"].selection)
        return return_Response(
            message="Response reset to draft",
            status=200,
            data={"response": _serialize_response(record, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/responses/<int:response_id>/submit",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def submit_response(self, response_id, **kwargs):
        """Admin equivalent of the response form's "Submit" button
        (`etp.assessment.response.action_submit`).

        Mirrors the manual flow available in the Odoo backoffice when an
        admin wants to mark a draft response submitted without going
        through the candidate portal. Will trigger the model's
        `_check_all_submitted` hook, which can auto-lock the candidate
        and auto-complete the parent assessment - same as the candidate's
        own final submit.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Response = request.env["etp.assessment.response"].sudo()
        record = Response.browse(response_id)
        if not record.exists():
            return return_Response(message="Response not found", status=404)
        try:
            record.action_submit()
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc),
                status=400,
            )
        state_labels = dict(Response._fields["state"].selection)
        return return_Response(
            message="Response submitted",
            status=200,
            data={"response": _serialize_response(record, state_labels)},
        )
