"""Response listing + per-dimension analytics endpoints."""

from odoo import http
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
    pct,
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
