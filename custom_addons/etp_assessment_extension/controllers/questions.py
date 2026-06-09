"""Question bank CRUD endpoints (with dimension + correct-option payloads)."""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    QUESTION_TYPES,
    coerce_bool,
    coerce_int,
    paginate,
    pagination_block,
    parse_json_body,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
)

QUESTION_COLUMNS = [
    {"key": "name", "label": "Title", "type": "string"},
    {"key": "question_type_label", "label": "Type", "type": "string"},
    {"key": "category_name", "label": "Category", "type": "string"},
    {"key": "dimension_count", "label": "Dimensions", "type": "integer"},
    {"key": "sequence", "label": "Sequence", "type": "integer"},
    {"key": "active", "label": "Active", "type": "boolean"},
    {"key": "create_date", "label": "Created", "type": "datetime"},
]

SORT_FIELDS = {
    "name": "name",
    "sequence": "sequence",
    "create_date": "create_date",
    "write_date": "write_date",
}


def _serialize_option_line(line):
    return {
        "id": line.id,
        "master_option_id": (
            line.master_option_id.id if line.master_option_id else 0
        ),
        "name": line.name or "",
        "sequence": line.sequence or 0,
        "is_correct": bool(line.is_correct),
        "score": line.score or 0,
    }


def _serialize_question_dimension(qd):
    return {
        "id": qd.id,
        "dimension_id": qd.dimension_id.id if qd.dimension_id else 0,
        "dimension_name": qd.dimension_id.name if qd.dimension_id else "",
        "sequence": qd.sequence or 0,
        "options": [
            _serialize_option_line(line)
            for line in qd.option_line_ids.sorted("sequence")
        ],
    }


def _serialize_question(rec, type_labels):
    return {
        "id": rec.id,
        "name": rec.name or "",
        "sequence": rec.sequence or 0,
        "question_type": rec.question_type or "",
        "question_type_label": type_labels.get(rec.question_type or "", ""),
        "prompt": rec.prompt or "",
        "description": rec.description or "",
        "active": bool(rec.active),
        "category_id": rec.category_id.id if rec.category_id else 0,
        "category_name": rec.category_id.name if rec.category_id else "",
        "image_a_url": rec.image_a_url or "",
        "image_b_url": rec.image_b_url or "",
        "code_snippet": rec.code_snippet or "",
        "code_language": rec.code_language or "",
        "video_url": rec.video_url or "",
        "dimension_count": len(rec.question_dimension_ids),
        "dimensions": [
            _serialize_question_dimension(qd)
            for qd in rec.question_dimension_ids.sorted("sequence")
        ],
        "create_date": rec.create_date.isoformat() if rec.create_date else None,
        "write_date": rec.write_date.isoformat() if rec.write_date else None,
    }


def _build_question_domain(params):
    domain = []
    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|",
            ("name", "ilike", search),
            ("prompt", "ilike", search),
            ("description", "ilike", search),
        ]
    category_id = coerce_int(params.get("category_id"), 0)
    if category_id:
        domain.append(("category_id", "=", category_id))
    question_type = (params.get("question_type") or "").strip()
    if question_type:
        if question_type not in QUESTION_TYPES:
            return None, return_Response(
                message=(
                    f"Invalid question_type '{question_type}'. "
                    f"Allowed: {', '.join(QUESTION_TYPES)}."
                ),
                status=400,
            )
        domain.append(("question_type", "=", question_type))
    active = coerce_bool(params.get("active"))
    if active is True:
        domain.append(("active", "=", True))
    elif active is False:
        domain.append(("active", "=", False))
    return domain, None


def _validate_question_type(value):
    if value not in QUESTION_TYPES:
        return return_Response(
            message=(
                f"Invalid question_type '{value}'. "
                f"Allowed: {', '.join(QUESTION_TYPES)}."
            ),
            status=400,
        )
    return None


def _build_question_vals(jdata, partial=False):
    """Translate JSON payload into create/write vals for etp.assessment.question."""
    vals = {}
    if "name" in jdata:
        vals["name"] = (jdata.get("name") or "").strip()
    if "sequence" in jdata:
        vals["sequence"] = coerce_int(jdata["sequence"], 10)
    if "question_type" in jdata:
        qtype = (jdata.get("question_type") or "").strip()
        error = _validate_question_type(qtype)
        if error is not None:
            return None, error
        vals["question_type"] = qtype
    if "prompt" in jdata:
        vals["prompt"] = jdata.get("prompt") or ""
    if "description" in jdata:
        vals["description"] = jdata.get("description") or ""
    if "category_id" in jdata:
        vals["category_id"] = coerce_int(jdata["category_id"], 0) or False
    if "image_a_url" in jdata:
        vals["image_a_url"] = jdata.get("image_a_url") or ""
    if "image_b_url" in jdata:
        vals["image_b_url"] = jdata.get("image_b_url") or ""
    if "code_snippet" in jdata:
        vals["code_snippet"] = jdata.get("code_snippet") or ""
    if "code_language" in jdata:
        vals["code_language"] = jdata.get("code_language") or "python"
    if "video_url" in jdata:
        vals["video_url"] = jdata.get("video_url") or ""
    if "active" in jdata:
        active = coerce_bool(jdata["active"])
        if active is not None:
            vals["active"] = active

    if not partial:
        if not vals.get("name"):
            return None, return_Response(
                message="'name' is required", status=400,
            )
        if not vals.get("prompt"):
            return None, return_Response(
                message="'prompt' is required", status=400,
            )

    return vals, None


def _apply_dimensions(question, dimensions_payload):
    """Replace the question's dimension lines with the supplied payload.

    Each item: {"dimension_id": int, "sequence": int,
                "options": [{"master_option_id": int, "is_correct": bool,
                             "sequence": int}, ...]}

    If an item has no `options` list, options are auto-populated from the
    master dimension (default model behavior).
    """
    QDim = request.env["etp.assessment.question.dimension"].sudo()
    OptLine = request.env["etp.assessment.question.dimension.option"].sudo()

    question.question_dimension_ids.unlink()

    for item in dimensions_payload:
        if not isinstance(item, dict):
            continue
        dim_id = coerce_int(item.get("dimension_id"), 0)
        if not dim_id:
            continue
        qd = QDim.create({
            "question_id": question.id,
            "dimension_id": dim_id,
            "sequence": coerce_int(item.get("sequence"), 10),
        })

        options_payload = item.get("options")
        if isinstance(options_payload, list) and options_payload:
            qd.option_line_ids.unlink()
            for opt in options_payload:
                if not isinstance(opt, dict):
                    continue
                master_id = coerce_int(opt.get("master_option_id"), 0)
                if not master_id:
                    continue
                OptLine.create({
                    "question_dimension_id": qd.id,
                    "master_option_id": master_id,
                    "sequence": coerce_int(opt.get("sequence"), 10),
                    "is_correct": coerce_bool(opt.get("is_correct"), False) or False,
                })


class EtpAssessmentQuestionController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/questions",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_questions(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain, error = _build_question_domain(params)
        if error is not None:
            return error
        order, error = resolve_order(params, SORT_FIELDS, "sequence", "asc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Question = env["etp.assessment.question"].sudo()
        total = Question.search_count(domain)
        records = Question.search(domain, limit=limit, offset=offset, order=order)

        type_labels = dict(Question._fields["question_type"].selection)
        rows = [_serialize_question(r, type_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Question bank",
                    "columns": QUESTION_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/questions/<int:question_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_question(self, question_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        Question = request.env["etp.assessment.question"].sudo()
        question = Question.browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)
        type_labels = dict(Question._fields["question_type"].selection)
        return return_Response(
            message="OK",
            status=200,
            data={"question": _serialize_question(question, type_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/questions",
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
        "prompt": {"type": "string", "required": True},
        "question_type": {"type": "string", "required": True},
    })
    def create_question(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        vals, error = _build_question_vals(jdata, partial=False)
        if error is not None:
            return error

        Question = request.env["etp.assessment.question"].sudo()
        question = Question.create(vals)

        dimensions_payload = jdata.get("dimensions")
        if isinstance(dimensions_payload, list) and dimensions_payload:
            _apply_dimensions(question, dimensions_payload)

        type_labels = dict(Question._fields["question_type"].selection)
        return return_Response(
            message="Question created",
            status=200,
            data={"question": _serialize_question(question, type_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/questions/<int:question_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_question(self, question_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Question = request.env["etp.assessment.question"].sudo()
        question = Question.browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        jdata = parse_json_body()
        vals, error = _build_question_vals(jdata, partial=True)
        if error is not None:
            return error

        if vals:
            question.write(vals)

        if "dimensions" in jdata and isinstance(jdata["dimensions"], list):
            _apply_dimensions(question, jdata["dimensions"])

        type_labels = dict(Question._fields["question_type"].selection)
        return return_Response(
            message="Question updated",
            status=200,
            data={"question": _serialize_question(question, type_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/questions/<int:question_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_question(self, question_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        question = request.env["etp.assessment.question"].sudo().browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)
        try:
            question.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete question: {exc}", status=400,
            )
        return return_Response(message="Question deleted", status=200)
