"""Question bank CRUD endpoints (with dimension + correct-option payloads)."""

import base64
import csv
import io

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .portal import _serve_question_image_bytes

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
    """Atomically replace the question's dimension lines with the payload.

    Each item: {"dimension_id": int, "sequence": int,
                "options": [{"master_option_id": int, "is_correct": bool,
                             "sequence": int}, ...]}

    If an item has no `options` key (or `options` is not a non-empty list),
    options are auto-populated from the master dimension (default model
    behavior - see `EtpAssessmentQuestionDimension._populate_options`).

    Returns `(ok, error_response)`. On model-level rejection
    (e.g. two `is_correct=True` for the same dimension) the entire
    replacement is rolled back via a savepoint so the question keeps its
    pre-call dimension state, and `(False, return_Response(..., status=400))`
    is returned so the caller can bail.
    """
    QDim = request.env["etp.assessment.question.dimension"].sudo()
    OptLine = request.env["etp.assessment.question.dimension.option"].sudo()

    try:
        with request.env.cr.savepoint():
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
    except (UserError, ValidationError) as exc:
        return False, return_Response(
            message=str(exc.args[0] if exc.args else exc),
            status=400,
        )
    return True, None


QUESTION_CSV_TEMPLATE_BODY = (
    "name,question_type,prompt,category_name,description,"
    "image_a_url,image_b_url,code_snippet,code_language,video_url,sequence\n"
    "Compare the two outputs,image_comparison,Which image is better?,"
    "Image Evaluation,Optional rubric,"
    "https://example.com/a.png,https://example.com/b.png,,,,,10\n"
    "Explain the snippet,coding,What does this Python do?,Coding,,"
    ",,\"def f(x):\\n    return x*2\",python,,20\n"
    "Watch and rate,video,Rate the clip,Video QA,,"
    ",,,,https://example.com/v.mp4,30\n"
)


class EtpAssessmentQuestionController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/questions/csv_template",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_question_csv_template(self, **kwargs):
        """Download the CSV template accepted by
        `POST /questions/bulk_import`. Symmetric with
        `/candidates/csv_template`.

        Columns - required: `name`, `question_type`, `prompt`.
        Optional: `category_id`, `category_name`, `description`,
        `image_a_url`, `image_b_url`, `code_snippet`, `code_language`,
        `video_url`, `sequence`.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        payload = QUESTION_CSV_TEMPLATE_BODY.encode("utf-8")
        return request.make_response(
            payload,
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                (
                    "Content-Disposition",
                    'attachment; filename="question_import_template.csv"',
                ),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
            ],
        )

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
        try:
            question = Question.create(vals)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        dimensions_payload = jdata.get("dimensions")
        if isinstance(dimensions_payload, list) and dimensions_payload:
            ok, err = _apply_dimensions(question, dimensions_payload)
            if not ok:
                return err

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
            try:
                question.write(vals)
            except (UserError, ValidationError) as exc:
                return return_Response(
                    message=str(exc.args[0] if exc.args else exc),
                    status=400,
                )

        if "dimensions" in jdata and isinstance(jdata["dimensions"], list):
            ok, err = _apply_dimensions(question, jdata["dimensions"])
            if not ok:
                return err

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

    @http.route(
        "/api/v1/etp_assessment_ext/questions/<int:question_id>/image/<string:field>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_question_image(self, question_id, field, **kwargs):
        """Serve `image_a` / `image_b` bytes of a question for the admin
        question editor (gateway-token auth). Mirrors the portal image
        route in `portal.py`."""
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden
        if field not in ("image_a", "image_b"):
            return return_Response(
                message="Field must be 'image_a' or 'image_b'.",
                status=400,
            )

        question = request.env["etp.assessment.question"].sudo().browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        response = _serve_question_image_bytes(question, field)
        if response is None:
            return return_Response(message="Image not set", status=404)
        return response

    @http.route(
        "/api/v1/etp_assessment_ext/questions/<int:question_id>/image/<string:field>",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def upload_question_image(self, question_id, field, **kwargs):
        """Upload / replace a question's binary `image_a` or `image_b`.

        Accepts either a multipart `file=<binary>` upload or a JSON body
        with `{"file_b64": "..."}`. Stores base64-encoded bytes in the
        binary field (Odoo convention)."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden
        if field not in ("image_a", "image_b"):
            return return_Response(
                message="Field must be 'image_a' or 'image_b'.",
                status=400,
            )

        question = request.env["etp.assessment.question"].sudo().browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        file_obj = request.httprequest.files.get("file")
        b64_payload = (request.params or {}).get("file_b64")
        if file_obj:
            raw_bytes = file_obj.read()
        elif b64_payload:
            try:
                raw_bytes = base64.b64decode(b64_payload)
            except Exception:
                return return_Response(
                    message="'file_b64' must be a valid base64 string.",
                    status=400,
                )
        else:
            return return_Response(
                message=(
                    "Upload an image via multipart 'file' or send a base64 "
                    "string in 'file_b64'."
                ),
                status=400,
            )

        if not raw_bytes:
            return return_Response(message="Uploaded file is empty.", status=400)

        encoded = base64.b64encode(raw_bytes)
        question.write({field: encoded})

        return return_Response(
            message="Image updated",
            status=200,
            data={
                "question_id": question.id,
                "field": field,
                "size_bytes": len(raw_bytes),
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/questions/bulk_import",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def bulk_import_questions(self, **kwargs):
        """Create questions from a CSV file.

        Required columns: `name`, `question_type`, `prompt`.
        Optional: `category_id`, `category_name`, `description`,
        `image_a_url`, `image_b_url`, `code_snippet`, `code_language`,
        `video_url`, `sequence`.

        Dimensions are NOT configured by CSV - add them per-question via
        `PUT /questions/<id>` after import.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        file_obj = request.httprequest.files.get("file")
        b64_payload = (request.params or {}).get("file_b64")
        if file_obj:
            csv_bytes = file_obj.read()
        elif b64_payload:
            try:
                csv_bytes = base64.b64decode(b64_payload)
            except Exception:
                return return_Response(
                    message="'file_b64' must be a valid base64 string.",
                    status=400,
                )
        else:
            return return_Response(
                message=(
                    "Upload a CSV via multipart 'file' or send a base64 "
                    "string in 'file_b64'."
                ),
                status=400,
            )

        try:
            decoded = csv_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return return_Response(
                message="Invalid CSV file: must be UTF-8 encoded.",
                status=400,
            )

        reader = csv.DictReader(io.StringIO(decoded))
        fieldnames = set(reader.fieldnames or [])
        required = {"name", "question_type", "prompt"}
        missing = required - fieldnames
        if missing:
            return return_Response(
                message=(
                    "CSV is missing required column(s): "
                    f"{', '.join(sorted(missing))}. "
                    f"Found: {', '.join(sorted(fieldnames)) or '(none)'}"
                ),
                status=400,
            )

        env = request.env
        Question = env["etp.assessment.question"].sudo()
        Category = env["etp.assessment.category"].sudo()

        imported_ids = []
        errors = []

        for idx, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            qtype = (row.get("question_type") or "").strip()
            prompt = (row.get("prompt") or "").strip()
            if not name or not qtype or not prompt:
                errors.append(
                    f"Row {idx}: 'name', 'question_type' and 'prompt' "
                    "are required."
                )
                continue
            if qtype not in QUESTION_TYPES:
                errors.append(
                    f"Row {idx}: invalid question_type '{qtype}'. "
                    f"Allowed: {', '.join(QUESTION_TYPES)}."
                )
                continue

            vals = {
                "name": name,
                "question_type": qtype,
                "prompt": prompt,
                "description": (row.get("description") or "") or "",
                "image_a_url": (row.get("image_a_url") or "") or "",
                "image_b_url": (row.get("image_b_url") or "") or "",
                "code_snippet": (row.get("code_snippet") or "") or "",
                "video_url": (row.get("video_url") or "") or "",
            }
            code_language = (row.get("code_language") or "").strip()
            if code_language:
                vals["code_language"] = code_language
            seq_raw = (row.get("sequence") or "").strip()
            if seq_raw:
                try:
                    vals["sequence"] = int(seq_raw)
                except ValueError:
                    errors.append(
                        f"Row {idx}: 'sequence' must be an integer "
                        f"(got '{seq_raw}')."
                    )
                    continue

            category_id_raw = (row.get("category_id") or "").strip()
            category_name = (row.get("category_name") or "").strip()
            if category_id_raw:
                try:
                    cat_id = int(category_id_raw)
                except ValueError:
                    errors.append(
                        f"Row {idx}: 'category_id' must be an integer "
                        f"(got '{category_id_raw}')."
                    )
                    continue
                cat = Category.browse(cat_id)
                if not cat.exists():
                    errors.append(
                        f"Row {idx}: category_id {cat_id} not found."
                    )
                    continue
                vals["category_id"] = cat.id
            elif category_name:
                cat = Category.search(
                    [("name", "=", category_name)], limit=1,
                )
                if not cat:
                    errors.append(
                        f"Row {idx}: unknown category_name "
                        f"'{category_name}'."
                    )
                    continue
                vals["category_id"] = cat.id

            try:
                with env.cr.savepoint():
                    created = Question.create(vals)
            except (UserError, ValidationError) as exc:
                errors.append(
                    f"Row {idx}: {exc.args[0] if exc.args else exc}"
                )
                continue
            except Exception as exc:
                errors.append(f"Row {idx}: {exc}")
                continue
            imported_ids.append(created.id)

        return return_Response(
            message=(
                f"{len(imported_ids)} question(s) imported, "
                f"{len(errors)} row error(s)."
            ),
            status=200,
            errors=errors,
            data={
                "imported_question_ids": imported_ids,
                "imported_count": len(imported_ids),
            },
        )
