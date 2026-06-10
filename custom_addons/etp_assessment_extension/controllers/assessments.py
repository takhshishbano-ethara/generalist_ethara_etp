"""Assessment CRUD + lifecycle action endpoints."""

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    ASSESSMENT_STATES,
    coerce_bool,
    coerce_int,
    jsonrpc_error,
    jsonrpc_response,
    m2o_link,
    paginate,
    pagination_block,
    parse_json_body,
    pct,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
    x2many_links,
)

ASSESSMENT_COLUMNS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "state_label", "label": "State", "type": "string"},
    {"key": "category_name", "label": "Category", "type": "string"},
    {"key": "evaluators_total", "label": "Candidates", "type": "integer"},
    {"key": "evaluators_done", "label": "Submitted", "type": "integer"},
    {"key": "progress_percent", "label": "Progress %", "type": "float"},
    {"key": "duration_minutes", "label": "Duration", "type": "integer"},
    {"key": "start_date", "label": "Start", "type": "datetime"},
    {"key": "end_date", "label": "End", "type": "datetime"},
]

SORT_FIELDS = {
    "name": "name",
    "create_date": "create_date",
    "start_date": "start_date",
    "end_date": "end_date",
    "state": "state",
}


def _serialize_assessment(rec, state_labels):
    total = len(rec.assessment_evaluator_ids)
    done = sum(1 for ev in rec.assessment_evaluator_ids if ev.state == "submitted")
    return {
        "id": rec.id,
        "name": rec.name or "",
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "category_id": rec.category_id.id if rec.category_id else 0,
        "category_name": rec.category_id.name if rec.category_id else "",
        "question_limit": rec.question_limit or 0,
        "total_questions_available": rec.total_questions_available or 0,
        "duration_minutes": rec.duration_minutes or 0,
        "start_date": rec.start_date.isoformat() if rec.start_date else None,
        "end_date": rec.end_date.isoformat() if rec.end_date else None,
        "deadline": rec.deadline.isoformat() if rec.deadline else None,
        "question_ids": rec.question_ids.ids,
        "candidate_ids": rec.evaluator_ids.ids,
        "evaluators_total": total,
        "evaluators_done": done,
        "progress_percent": pct(done, total),
        "response_count": rec.response_count or 0,
        "create_date": rec.create_date.isoformat() if rec.create_date else None,
        "write_date": rec.write_date.isoformat() if rec.write_date else None,
    }


def _build_assessment_domain(params):
    domain = []
    search = (params.get("search") or "").strip()
    if search:
        domain.append(("name", "ilike", search))
    state = (params.get("state") or "").strip()
    if state:
        if state not in ASSESSMENT_STATES:
            return None, return_Response(
                message=(
                    f"Invalid state '{state}'. "
                    f"Allowed: {', '.join(ASSESSMENT_STATES)}."
                ),
                status=400,
            )
        domain.append(("state", "=", state))
    category_id = coerce_int(params.get("category_id"), 0)
    if category_id:
        domain.append(("category_id", "=", category_id))
    date_from = (params.get("date_from") or "").strip()
    if date_from:
        domain.append(("start_date", ">=", date_from))
    date_to = (params.get("date_to") or "").strip()
    if date_to:
        domain.append(("end_date", "<=", date_to))
    return domain, None


def _build_assessment_vals(jdata, partial=False):
    vals = {}
    if "name" in jdata:
        vals["name"] = (jdata.get("name") or "").strip()
    if "category_id" in jdata:
        vals["category_id"] = coerce_int(jdata["category_id"], 0) or False
    if "question_limit" in jdata:
        vals["question_limit"] = coerce_int(jdata["question_limit"], 0)
    if "duration_minutes" in jdata:
        vals["duration_minutes"] = coerce_int(jdata["duration_minutes"], 0)
    if "start_date" in jdata:
        vals["start_date"] = jdata.get("start_date") or False
    if "end_date" in jdata:
        vals["end_date"] = jdata.get("end_date") or False
    if "deadline" in jdata:
        vals["deadline"] = jdata.get("deadline") or False
    if "candidate_ids" in jdata and isinstance(jdata["candidate_ids"], list):
        ids = [coerce_int(c, 0) for c in jdata["candidate_ids"]]
        ids = [i for i in ids if i]
        vals["evaluator_ids"] = [(6, 0, ids)]

    if not partial:
        if not vals.get("name"):
            return None, return_Response(
                message="'name' is required", status=400,
            )
        if not vals.get("category_id"):
            return None, return_Response(
                message="'category_id' is required", status=400,
            )

    return vals, None


def _run_state_action(assessment_id, method_name, success_message):
    forbidden = require_assessment_manager()
    if forbidden is not None:
        return forbidden

    assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
    if not assessment.exists():
        return return_Response(message="Assessment not found", status=404)
    try:
        getattr(assessment, method_name)()
    except (UserError, ValidationError) as exc:
        return return_Response(message=str(exc.args[0] if exc.args else exc), status=400)
    except Exception as exc:
        return return_Response(message=str(exc), status=400)

    state_labels = dict(
        request.env["etp.assessment"]._fields["state"].selection
    )
    return return_Response(
        message=success_message,
        status=200,
        data={"assessment": _serialize_assessment(assessment, state_labels)},
    )


class EtpAssessmentController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/assessments",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_assessments(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain, error = _build_assessment_domain(params)
        if error is not None:
            return error
        order, error = resolve_order(params, SORT_FIELDS, "create_date", "desc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Assessment = env["etp.assessment"].sudo()
        total = Assessment.search_count(domain)
        records = Assessment.search(domain, limit=limit, offset=offset, order=order)
        state_labels = dict(Assessment._fields["state"].selection)
        rows = [_serialize_assessment(r, state_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Assessments",
                    "columns": ASSESSMENT_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        Assessment = request.env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)
        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="OK",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/detail",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_assessment_detail(self, assessment_id, **kwargs):
        """JSON-RPC 2.0 `web_read`-style payload for a single assessment.

        Returns the assessment record with Many2one fields expanded to
        `{id, display_name}` and *2many fields expanded to lists of
        `{id, display_name}` - the same shape Odoo's web client gets
        back from `web_read`.

        Envelope:

        ```json
        {
          "jsonrpc": "2.0",
          "id": 1,
          "result": [{...assessment...}]
        }
        ```

        Errors return:

        ```json
        {
          "jsonrpc": "2.0",
          "id": 1,
          "error": {"code": 404, "message": "Assessment not found"}
        }
        ```

        Optional `?id=<int>` query param echoes the JSON-RPC request id
        for clients that need request/response pairing.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        Assessment = env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return jsonrpc_error(404, "Assessment not found", http_status=404)

        record = {
            "id": assessment.id,
            "state": assessment.state,
            "name": assessment.name or "",
            "display_name": (
                assessment.display_name or assessment.name or ""
            ),
            "category_id": m2o_link(assessment.category_id),
            "question_limit": assessment.question_limit or 0,
            "total_questions_available": (
                assessment.total_questions_available or 0
            ),
            "duration_minutes": assessment.duration_minutes or 0,
            "start_date": (
                assessment.start_date.strftime("%Y-%m-%d %H:%M:%S")
                if assessment.start_date else False
            ),
            "end_date": (
                assessment.end_date.strftime("%Y-%m-%d %H:%M:%S")
                if assessment.end_date else False
            ),
            "deadline": (
                assessment.deadline.strftime("%Y-%m-%d")
                if assessment.deadline else False
            ),
            "response_count": assessment.response_count or 0,
            "assessment_evaluator_ids": (
                x2many_links(assessment.assessment_evaluator_ids)
            ),
            "evaluator_ids": x2many_links(assessment.evaluator_ids),
            "candidate_csv_file": False,
            "candidate_csv_filename": (
                assessment.candidate_csv_filename or False
            ),
            "question_ids": x2many_links(assessment.question_ids),
            "response_ids": x2many_links(assessment.response_ids),
        }

        return jsonrpc_response([record])

    @http.route(
        "/api/v1/etp_assessment_ext/assessments",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "name": {"type": "string", "required": True},
        "category_id": {"type": "int", "required": True},
    })
    def create_assessment(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        vals, error = _build_assessment_vals(jdata, partial=False)
        if error is not None:
            return error

        Assessment = request.env["etp.assessment"].sudo()
        try:
            assessment = Assessment.create(vals)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="Assessment created",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Assessment = request.env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        jdata = parse_json_body()
        vals, error = _build_assessment_vals(jdata, partial=True)
        if error is not None:
            return error
        if vals:
            try:
                assessment.write(vals)
            except (UserError, ValidationError) as exc:
                return return_Response(
                    message=str(exc.args[0] if exc.args else exc),
                    status=400,
                )

        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="Assessment updated",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        if assessment.state not in ("draft", "cancelled"):
            return return_Response(
                message=(
                    "Only draft or cancelled assessments can be deleted "
                    "(current state: "
                    f"{assessment.state})."
                ),
                status=400,
            )
        try:
            assessment.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete assessment: {exc}",
                status=400,
            )
        return return_Response(message="Assessment deleted", status=200)

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/start",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def start_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_start",
            "Assessment started and invitations sent",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/done",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def done_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_done", "Assessment marked done",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/cancel",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def cancel_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_cancel", "Assessment cancelled",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/reset_draft",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def reset_draft_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_reset_draft", "Assessment reset to draft",
        )
