import logging

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_token, validate_request, return_Response,
)
from odoo.addons.etp_applicant_assessment.models._common import (
    MCQ_TYPES,
    VALID_QUESTION_TYPE_KEYS,
)

_logger = logging.getLogger(__name__)


VALID_DIFFICULTY = {"easy", "medium", "hard"}
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100

_STR_FIELDS = ("name", "prompt", "tags", "skills")
_INT_FIELDS = ("marks", "negative_marks")
_SELECT_FIELDS = ("question_type", "difficulty")


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def _coerce_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bank_vals(payload, partial=False):
    vals = {}

    for key in _STR_FIELDS:
        if key in payload:
            value = payload[key]
            if value is None:
                if key in ("name", "prompt") and not partial:
                    raise ValidationError("%s cannot be null." % key)
                vals[key] = False
            else:
                if not isinstance(value, str):
                    raise ValidationError("%s must be a string." % key)
                vals[key] = value.strip()

    for key in _INT_FIELDS:
        if key in payload and payload[key] is not None:
            coerced = _coerce_int(payload[key])
            if coerced is None:
                raise ValidationError("%s must be an integer." % key)
            vals[key] = coerced

    if "question_type" in payload and payload["question_type"] is not None:
        qtype = payload["question_type"]
        if qtype not in VALID_QUESTION_TYPE_KEYS:
            raise ValidationError(
                "question_type %r is not one of %s."
                % (qtype, sorted(VALID_QUESTION_TYPE_KEYS))
            )
        vals["question_type"] = qtype

    if "difficulty" in payload and payload["difficulty"] is not None:
        diff = payload["difficulty"]
        if diff not in VALID_DIFFICULTY:
            raise ValidationError(
                "difficulty %r is not one of %s."
                % (diff, sorted(VALID_DIFFICULTY))
            )
        vals["difficulty"] = diff

    if "active" in payload:
        vals["active"] = _to_bool(payload["active"])

    if not partial:
        if not (vals.get("name") or "").strip():
            raise ValidationError("name is required.")
        if not (vals.get("prompt") or "").strip():
            raise ValidationError("prompt is required.")
        if "marks" in vals and vals["marks"] <= 0:
            raise ValidationError("marks must be a positive integer.")
        if "negative_marks" in vals and vals["negative_marks"] < 0:
            raise ValidationError("negative_marks must be >= 0.")

    return vals


def _validate_options_payload(options, question_type):
    if not isinstance(options, list):
        raise ValidationError("options must be a list.")

    if question_type in MCQ_TYPES:
        if len(options) < 2:
            raise ValidationError(
                "MCQ-style questions need at least two options."
            )
        any_correct = False
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                raise ValidationError(
                    "options[%d] must be an object." % i
                )
            if not (opt.get("label") or "").strip():
                raise ValidationError(
                    "options[%d].label is required." % i
                )
            if _to_bool(opt.get("is_correct")):
                any_correct = True
        if not any_correct:
            raise ValidationError(
                "MCQ-style questions need at least one correct option."
            )
    else:
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                raise ValidationError(
                    "options[%d] must be an object." % i
                )
            if not (opt.get("label") or "").strip():
                raise ValidationError(
                    "options[%d].label is required." % i
                )


def _build_option_cmds(options):
    cmds = []
    for opt in options:
        opt_vals = {
            "label": opt["label"].strip(),
            "is_correct": _to_bool(opt.get("is_correct")),
        }
        seq = _coerce_int(opt.get("sequence"))
        if seq is not None:
            opt_vals["sequence"] = seq
        cmds.append((0, 0, opt_vals))
    return cmds


def _serialize_option(opt):
    return {
        "id": opt.id,
        "sequence": opt.sequence,
        "label": opt.label or "",
        "is_correct": bool(opt.is_correct),
    }


def _serialize_bank_full(bank):
    return {
        "id": bank.id,
        "name": bank.name or "",
        "active": bool(bank.active),
        "prompt": bank.prompt or "",
        "question_type": bank.question_type or "",
        "marks": bank.marks or 0,
        "negative_marks": bank.negative_marks or 0,
        "difficulty": bank.difficulty or "",
        "tags": bank.tags or "",
        "skills": bank.skills or "",
        "options": [
            _serialize_option(o)
            for o in bank.option_ids.sorted(key=lambda x: (x.sequence, x.id))
        ],
        "create_date": (
            bank.create_date.isoformat() if bank.create_date else ""
        ),
        "write_date": (
            bank.write_date.isoformat() if bank.write_date else ""
        ),
        "create_uid": bank.create_uid.id if bank.create_uid else False,
        "create_user": bank.create_uid.name if bank.create_uid else "",
        "write_uid": bank.write_uid.id if bank.write_uid else False,
        "write_user": bank.write_uid.name if bank.write_uid else "",
    }


def _serialize_bank_row(bank):
    return {
        "id": bank.id,
        "name": bank.name or "",
        "question": bank.prompt or "",
        "tags": bank.tags or "",
        "skills": bank.skills or "",
        "marks": bank.marks or 0,
        "difficulty": bank.difficulty or "",
        "question_type": bank.question_type or "",
        "active": bool(bank.active),
    }


class QuestionBankApi(http.Controller):

    @validate_token
    @http.route(
        "/api/v1/question-bank/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({
        "name": {"type": "string", "required": True},
        "prompt": {"type": "string", "required": True},
    })
    def create_question_bank(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            vals = _coerce_bank_vals(jdata, partial=False)

            qtype = vals.get("question_type") or "mcq_single"
            options = jdata.get("options") or []
            if options or qtype in MCQ_TYPES:
                _validate_options_payload(options, qtype)
                vals["option_ids"] = _build_option_cmds(options)

            bank = request.env[
                "etp.applicant.assessment.question.bank"
            ].sudo().create(vals)
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to create question bank via API.")
            return return_Response(
                message="Failed to create question bank.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Question bank created successfully.",
            status=200,
            data={"data": _serialize_bank_full(bank)},
        )

    @validate_token
    @http.route(
        "/api/v1/question-bank/update",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({"id": {"type": "int", "required": True}})
    def update_question_bank(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            bank_id = _coerce_int(jdata.get("id"))
            if not bank_id or bank_id <= 0:
                return return_Response(
                    message="'id' must be a positive integer.", status=400,
                )

            bank = request.env[
                "etp.applicant.assessment.question.bank"
            ].sudo().with_context(active_test=False).browse(bank_id)
            if not bank.exists():
                return return_Response(
                    message="Question bank id=%d not found." % bank_id,
                    status=404,
                )

            vals = _coerce_bank_vals(jdata, partial=True)

            replace_options = "options" in jdata
            new_qtype = vals.get("question_type") or bank.question_type

            if replace_options:
                options = jdata.get("options") or []
                _validate_options_payload(options, new_qtype)
                vals["option_ids"] = [(5, 0, 0)] + _build_option_cmds(options)
            elif "question_type" in vals and new_qtype in MCQ_TYPES:
                existing = [
                    {
                        "label": o.label,
                        "is_correct": o.is_correct,
                    }
                    for o in bank.option_ids
                ]
                _validate_options_payload(existing, new_qtype)

            if vals:
                bank.write(vals)
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to update question bank via API.")
            return return_Response(
                message="Failed to update question bank.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Question bank updated successfully.",
            status=200,
            data={"data": _serialize_bank_full(bank)},
        )

    @validate_token
    @http.route(
        "/api/v1/question-bank/detail",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({"id": {"type": "int", "required": True}})
    def question_bank_detail(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            bank_id = _coerce_int(jdata.get("id"))
            if not bank_id or bank_id <= 0:
                return return_Response(
                    message="'id' must be a positive integer.", status=400,
                )

            bank = request.env[
                "etp.applicant.assessment.question.bank"
            ].sudo().with_context(active_test=False).browse(bank_id)
            if not bank.exists():
                return return_Response(
                    message="Question bank id=%d not found." % bank_id,
                    status=404,
                )
        except Exception as e:
            _logger.exception("Failed to fetch question bank detail via API.")
            return return_Response(
                message="Failed to fetch question bank.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Question bank fetched successfully.",
            status=200,
            data={"data": _serialize_bank_full(bank)},
        )

    @validate_token
    @http.route(
        "/api/v1/question-bank/list",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    def question_bank_list(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            if not jdata:
                import json
                try:
                    raw = request.httprequest.get_data(as_text=True) or ""
                    jdata = json.loads(raw) if raw else {}
                except Exception:
                    jdata = {}

            page = max(1, _coerce_int(jdata.get("page"), 1) or 1)
            per_page = _coerce_int(jdata.get("limit"), LIST_DEFAULT_LIMIT)
            per_page = max(1, min(per_page or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT))
            offset = (page - 1) * per_page

            domain = []

            search = (jdata.get("search") or "").strip()
            if search:
                domain += [
                    "|", "|", "|",
                    ("name", "ilike", search),
                    ("prompt", "ilike", search),
                    ("tags", "ilike", search),
                    ("skills", "ilike", search),
                ]

            qtype = jdata.get("question_type")
            if qtype:
                if qtype not in VALID_QUESTION_TYPE_KEYS:
                    return return_Response(
                        message=(
                            "question_type %r is not one of %s."
                            % (qtype, sorted(VALID_QUESTION_TYPE_KEYS))
                        ),
                        status=400,
                    )
                domain.append(("question_type", "=", qtype))

            difficulty = jdata.get("difficulty")
            if difficulty:
                if difficulty not in VALID_DIFFICULTY:
                    return return_Response(
                        message=(
                            "difficulty %r is not one of %s."
                            % (difficulty, sorted(VALID_DIFFICULTY))
                        ),
                        status=400,
                    )
                domain.append(("difficulty", "=", difficulty))

            include_archived = _to_bool(jdata.get("include_archived"))
            archived_only = _to_bool(jdata.get("archived_only"))

            Bank = request.env[
                "etp.applicant.assessment.question.bank"
            ].sudo()
            if archived_only:
                Bank = Bank.with_context(active_test=False)
                domain.append(("active", "=", False))
            elif include_archived:
                Bank = Bank.with_context(active_test=False)

            total = Bank.search_count(domain)
            total_pages = (total + per_page - 1) // per_page if total else 0
            records = Bank.search(
                domain, limit=per_page, offset=offset, order="id desc",
            )
        except Exception as e:
            _logger.exception("Failed to list question bank via API.")
            return return_Response(
                message="Failed to list question bank.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Question bank list fetched successfully.",
            status=200,
            data={
                "data": [_serialize_bank_row(r) for r in records],
                "pagination": {
                    "current_page": page,
                    "per_page": per_page,
                    "total": total,
                    "total_pages": total_pages,
                },
            },
        )

    @validate_token
    @http.route(
        "/api/v1/question-bank/archive",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({"id": {"type": "int", "required": True}})
    def archive_question_bank(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            bank_id = _coerce_int(jdata.get("id"))
            if not bank_id or bank_id <= 0:
                return return_Response(
                    message="'id' must be a positive integer.", status=400,
                )

            archive_flag = jdata.get("archive")
            active_target = (
                not _to_bool(archive_flag) if archive_flag is not None
                else False
            )

            bank = request.env[
                "etp.applicant.assessment.question.bank"
            ].sudo().with_context(active_test=False).browse(bank_id)
            if not bank.exists():
                return return_Response(
                    message="Question bank id=%d not found." % bank_id,
                    status=404,
                )

            if bank.active != active_target:
                bank.write({"active": active_target})
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to archive question bank via API.")
            return return_Response(
                message="Failed to update archive state.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message=(
                "Question bank archived successfully."
                if not active_target
                else "Question bank unarchived successfully."
            ),
            status=200,
            data={
                "data": {
                    "id": bank.id,
                    "active": bool(bank.active),
                },
            },
        )
