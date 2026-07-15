import json
import logging
import os

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_token, validate_request, return_Response,
)
from odoo.addons.etp_applicant_assessment.models._common import MCQ_TYPES

_logger = logging.getLogger(__name__)


LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100

_TEMPLATE_STATUS_KEYS = {"draft", "public", "archive"}

_ASSESSMENT_STATE_KEYS = {
    "draft", "sent", "in_progress", "submitted", "scored", "cancelled",
}
_ASSESSMENT_RESULT_KEYS = {"pending", "pass", "fail"}

_TEMPLATE_STR_FIELDS = ("name", "description",)
_TEMPLATE_INT_FIELDS = ("duration_minutes", "warning_cap",)
_TEMPLATE_FLOAT_FIELDS = (
    "pass_mark_percent",
    "penalty_other_person", "penalty_mobile_phone", "penalty_lip_movement",
    "penalty_window_change", "penalty_no_face", "penalty_look_away",
)
_TEMPLATE_BOOL_FIELDS = (
    "active",
    "require_webcam", "require_mic", "require_fullscreen",
    "block_copy_paste", "block_right_click", "detect_window_switch",
    "detect_no_face", "detect_other_person", "detect_look_away",
    "detect_lip_movement", "detect_mobile_phone",
    "shuffle_questions",
)
from odoo.addons.etp_applicant_assessment.models._common import (
    VALID_QUESTION_TYPE_KEYS as _VALID_QUESTION_TYPES,
)


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


def _dt(value):
    return value.isoformat() if value else False


def _serialize_assessment_row(rec):
    applicant = rec.applicant_id
    job = rec.job_id
    template = rec.template_id
    return {
        "id": rec.id,
        "state": rec.state,
        "result": rec.result,
        "applicant": {
            "id": applicant.id if applicant else False,
            "name": applicant.partner_name or "" if applicant else "",
            "email": applicant.email_from or "" if applicant else "",
        },
        "job": {
            "id": job.id if job else False,
            "name": job.name if job else "",
        },
        "template": {
            "id": template.id if template else False,
            "name": template.name if template else "",
        },
        "duration_minutes": rec.duration_minutes,
        "pass_mark_percent": rec.pass_mark_percent,
        "question_count": rec.question_count,
        "final_score": rec.final_score,
        "sent_at": _dt(rec.create_date),
        "started_at": _dt(rec.started_at),
        "submitted_at": _dt(rec.submitted_at),
        "deadline_at": _dt(rec.deadline_at),
        "test_link": rec.portal_url,
    }


def _serialize_option(opt):
    return {
        "id": opt.id,
        "sequence": opt.sequence,
        "label": opt.label,
        "is_correct": opt.is_correct,
    }


def _serialize_question(q):
    return {
        "id": q.id,
        "section_id": q.section_id.id if q.section_id else False,
        "sequence": q.sequence,
        "prompt": q.prompt,
        "question_type": q.question_type,
        "marks": q.marks,
        "negative_marks": q.negative_marks,
        "options": [_serialize_option(o) for o in q.option_ids],
    }


def _serialize_section(s):
    return {
        "id": s.id,
        "sequence": s.sequence,
        "name": s.name,
        "description": s.description or "",
        "question_ids": s.question_ids.ids,
    }


def _serialize_answer(a):
    return {
        "id": a.id,
        "question_id": a.question_id.id if a.question_id else False,
        "question_type": a.question_type,
        "selected_option_ids": a.selected_option_ids.ids,
        "text_answer": a.text_answer or "",
        "is_answered": a.is_answered,
        "is_correct": a.is_correct,
        "needs_review": a.needs_review,
        "manual_score": a.manual_score,
        "manual_score_set": a.manual_score_set,
        "score": a.score,
        "submitted_at": _dt(a.submitted_at),
    }


def _serialize_warning(w):
    return {
        "id": w.id,
        "kind": w.kind,
        "penalty_percent": w.penalty_percent,
        "detector_confidence": w.detector_confidence,
        "chunk_start_at": _dt(w.chunk_start_at),
        "chunk_end_at": _dt(w.chunk_end_at),
        "s3_url": w.s3_url or "",
        "s3_key": w.s3_key or "",
        "snapshot_url": w.snapshot_url or "",
        "snapshot_key": w.snapshot_key or "",
    }


def _serialize_snapshot(s):
    return {
        "id": s.id,
        "reason": s.reason,
        "url": s.url,
        "signed_url": s.signed_url or s.url,
        "s3_key": s.s3_key or "",
        "captured_at": _dt(s.captured_at),
    }


def _serialize_assessment_detail(rec):
    row = _serialize_assessment_row(rec)
    applicant = rec.applicant_id
    template = rec.template_id
    row["applicant"].update({
        "mobile": (applicant.partner_phone or "") if applicant else "",
        "status": applicant.status if applicant else "",
        "candidate_id": (
            applicant.candidate_id.id
            if applicant and applicant.candidate_id else False
        ),
        "candidate_name": (
            applicant.candidate_id.name
            if applicant and applicant.candidate_id else ""
        ),
    })
    row["template"].update({
        "status": template.status if template else "",
    })
    row["access_token"] = rec.access_token
    row["max_score"] = rec.max_score
    row["objective_score"] = rec.objective_score
    row["warning_penalty"] = rec.warning_penalty
    row["has_pending_review"] = rec.has_pending_review
    row["answered_count"] = rec.answered_count
    row["warning_count"] = rec.warning_count
    row["snapshot_count"] = rec.snapshot_count
    row["consent_at"] = _dt(rec.consent_at)
    row["consent_version"] = rec.consent_version or ""
    row["media_error_count"] = rec.media_error_count
    row["proctoring"] = {
        "require_webcam": rec.require_webcam,
        "require_mic": rec.require_mic,
        "require_fullscreen": rec.require_fullscreen,
        "block_copy_paste": rec.block_copy_paste,
        "block_right_click": rec.block_right_click,
        "detect_window_switch": rec.detect_window_switch,
        "detect_no_face": rec.detect_no_face,
        "detect_other_person": rec.detect_other_person,
        "detect_look_away": rec.detect_look_away,
        "detect_lip_movement": rec.detect_lip_movement,
        "detect_mobile_phone": rec.detect_mobile_phone,
        "shuffle_questions": rec.shuffle_questions,
        "warning_cap": rec.warning_cap,
    }
    row["sections"] = [_serialize_section(s) for s in rec.section_ids]
    row["questions"] = [_serialize_question(q) for q in rec.question_ids]
    row["answers"] = [_serialize_answer(a) for a in rec.answer_ids]
    row["warnings"] = [_serialize_warning(w) for w in rec.warning_ids]
    row["snapshots"] = [_serialize_snapshot(s) for s in rec.snapshot_ids]
    try:
        row["media_errors"] = json.loads(rec.media_errors_json or "[]")
    except (TypeError, ValueError):
        row["media_errors"] = []
    return row


def _serialize_template_row(rec, assigned_map):
    return {
        "id": rec.id,
        "title": rec.name,
        "job_id": rec.job_id.id if rec.job_id else False,
        "job_position_name": rec.job_id.name if rec.job_id else "",
        "question_count": rec.question_count,
        "marks": rec.max_score,
        "assigned_count": assigned_map.get(rec.id, 0),
        "status": rec.status,
        "active": rec.active,
    }


def _coerce_template_vals(tpl):
    vals = {}
    for key in _TEMPLATE_STR_FIELDS:
        if key in tpl and tpl[key] is not None:
            vals[key] = tpl[key]
    for key in _TEMPLATE_INT_FIELDS:
        if key in tpl and tpl[key] is not None:
            try:
                vals[key] = int(tpl[key])
            except (TypeError, ValueError):
                raise ValidationError("template.%s must be an integer." % key)
    for key in _TEMPLATE_FLOAT_FIELDS:
        if key in tpl and tpl[key] is not None:
            try:
                vals[key] = float(tpl[key])
            except (TypeError, ValueError):
                raise ValidationError("template.%s must be a number." % key)
    for key in _TEMPLATE_BOOL_FIELDS:
        if key in tpl:
            vals[key] = _to_bool(tpl[key])
    return vals


def _resolve_job_id(tpl):
    job_id = tpl.get("job_id")
    if isinstance(job_id, int) and job_id > 0:
        job = request.env["hr.job"].sudo().browse(job_id)
        if not job.exists():
            raise ValidationError("job_id %d does not exist." % job_id)
        return job.id
    job_name = tpl.get("job_name")
    if isinstance(job_name, str) and job_name.strip():
        job = request.env["hr.job"].sudo().search(
            [("name", "=", job_name.strip())], limit=1,
        )
        if not job:
            raise ValidationError(
                "No hr.job found with name %r." % job_name.strip()
            )
        return job.id
    return False


def _resolve_bank_reference(q):
    bqid = q.get("bank_question_id")
    bqname = q.get("bank_question_name")
    Bank = request.env["etp.applicant.assessment.question.bank"].sudo()
    bank = Bank.browse()
    if bqid:
        try:
            bqid_i = int(bqid)
        except (TypeError, ValueError):
            raise ValidationError("bank_question_id must be an integer.")
        bank = Bank.browse(bqid_i)
        if not bank.exists():
            raise ValidationError(
                "Bank question id=%d does not exist." % bqid_i
            )
    elif bqname:
        if not isinstance(bqname, str) or not bqname.strip():
            raise ValidationError(
                "bank_question_name must be a non-empty string."
            )
        bank = Bank.search([("name", "=", bqname.strip())], limit=1)
        if not bank:
            raise ValidationError(
                "No bank question found with name %r." % bqname.strip()
            )
    else:
        return False
    if not bank.active:
        raise ValidationError(
            "Bank question %r is archived." % (bank.name or bank.id)
        )
    q.setdefault("prompt", bank.prompt)
    q.setdefault("question_type", bank.question_type)
    if "marks" not in q:
        q["marks"] = bank.marks
    if "negative_marks" not in q:
        q["negative_marks"] = bank.negative_marks
    if "options" not in q:
        q["options"] = [
            {
                "sequence": o.sequence,
                "label": o.label,
                "is_correct": o.is_correct,
            }
            for o in bank.option_ids
        ]
    q["_resolved_bank_id"] = bank.id
    return bank.id


def _validate_sections_payload(sections):
    if not isinstance(sections, list):
        raise ValidationError("'sections' must be a list.")
    for i, section in enumerate(sections):
        prefix = "sections[%d]" % i
        if not isinstance(section, dict):
            raise ValidationError("%s must be an object." % prefix)
        if not (section.get("name") or "").strip():
            raise ValidationError("%s.name is required." % prefix)
        questions = section.get("questions") or []
        if not isinstance(questions, list):
            raise ValidationError("%s.questions must be a list." % prefix)
        for j, q in enumerate(questions):
            qprefix = "%s.questions[%d]" % (prefix, j)
            if not isinstance(q, dict):
                raise ValidationError("%s must be an object." % qprefix)
            try:
                _resolve_bank_reference(q)
            except ValidationError as ve:
                raise ValidationError("%s: %s" % (qprefix, ve))
            if not (q.get("prompt") or "").strip():
                raise ValidationError("%s.prompt is required." % qprefix)
            qtype = q.get("question_type") or "mcq_single"
            if qtype not in _VALID_QUESTION_TYPES:
                raise ValidationError(
                    "%s.question_type %r is not one of %s."
                    % (qprefix, qtype, sorted(_VALID_QUESTION_TYPES))
                )
            try:
                marks = int(q.get("marks", 1))
                if marks <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValidationError(
                    "%s.marks must be a positive integer." % qprefix
                )
            try:
                neg = int(q.get("negative_marks", 0))
                if neg < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValidationError(
                    "%s.negative_marks must be >= 0." % qprefix
                )
            options = q.get("options") or []
            if qtype in MCQ_TYPES:
                if not isinstance(options, list) or len(options) < 2:
                    raise ValidationError(
                        "%s.options must have at least 2 items for MCQ."
                        % qprefix
                    )
                any_correct = False
                for k, opt in enumerate(options):
                    optprefix = "%s.options[%d]" % (qprefix, k)
                    if not isinstance(opt, dict):
                        raise ValidationError(
                            "%s must be an object." % optprefix
                        )
                    if not (opt.get("label") or "").strip():
                        raise ValidationError(
                            "%s.label is required." % optprefix
                        )
                    if _to_bool(opt.get("is_correct")):
                        any_correct = True
                if not any_correct:
                    raise ValidationError(
                        "%s must have at least one option with "
                        "is_correct=true." % qprefix
                    )


def _create_template_tree(payload):
    Template = request.env["etp.applicant.assessment.template"].sudo()
    Section = request.env["etp.applicant.assessment.template.section"].sudo()
    Question = request.env["etp.applicant.assessment.template.question"].sudo()

    tpl_vals = _coerce_template_vals(payload)
    job_id = _resolve_job_id(payload)
    if job_id:
        tpl_vals["job_id"] = job_id

    template = Template.create(tpl_vals)

    for section in payload.get("sections") or []:
        section_vals = {
            "template_id": template.id,
            "name": section["name"].strip(),
        }
        if "sequence" in section and section["sequence"] is not None:
            section_vals["sequence"] = int(section["sequence"])
        if section.get("description"):
            section_vals["description"] = section["description"]
        sec_rec = Section.create(section_vals)

        for q in section.get("questions") or []:
            q_vals = {
                "template_id": template.id,
                "section_id": sec_rec.id,
                "prompt": q["prompt"].strip(),
                "question_type": q.get("question_type") or "mcq_single",
                "marks": int(q.get("marks") or 1),
                "negative_marks": int(q.get("negative_marks") or 0),
            }
            if q.get("_resolved_bank_id"):
                q_vals["bank_question_id"] = q["_resolved_bank_id"]
            if "sequence" in q and q["sequence"] is not None:
                q_vals["sequence"] = int(q["sequence"])
            option_cmds = []
            for opt in q.get("options") or []:
                opt_vals = {
                    "label": opt["label"].strip(),
                    "is_correct": _to_bool(opt.get("is_correct")),
                }
                if "sequence" in opt and opt["sequence"] is not None:
                    opt_vals["sequence"] = int(opt["sequence"])
                option_cmds.append((0, 0, opt_vals))
            if option_cmds:
                q_vals["option_ids"] = option_cmds
            Question.create(q_vals)

    return template


class ApplicantAssessmentApi(http.Controller):

    @validate_token
    @http.route(
        "/api/v1/assessment-template/create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({"template": {"type": "dict", "required": True}})
    def create_assessment_template(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            tpl_payload = jdata.get("template") or {}
            if not (tpl_payload.get("name") or "").strip():
                return return_Response(
                    message="template.name is required.", status=400,
                )
            _validate_sections_payload(tpl_payload.get("sections") or [])
            template = _create_template_tree(tpl_payload)
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to create assessment template via API.")
            return return_Response(
                message="Failed to create template.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Assessment template created successfully.",
            status=200,
            data={
                "data": {
                    "id": template.id,
                    "name": template.name,
                    "job_id": template.job_id.id if template.job_id else False,
                    "job_name": (
                        template.job_id.name if template.job_id else ""
                    ),
                    "section_count": len(template.section_ids),
                    "question_count": template.question_count,
                    "max_score": template.max_score,
                },
            },
        )

    @validate_token
    @http.route(
        "/api/v1/applicant-assessment/bulk-create",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({
        "template_id": {"type": "int", "required": True},
        "applicant_ids": {"type": "list", "required": True},
    })
    def bulk_create_applicant_assessments(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            template_id = jdata.get("template_id")
            applicant_ids = jdata.get("applicant_ids") or []
            result = request.env["etp.applicant.assessment"].sudo(
            ).create_bulk_for_template(template_id, applicant_ids)
        except (UserError, ValidationError) as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Bulk applicant-assessment creation failed.")
            return return_Response(
                message=str(e) or "Bulk creation failed.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Bulk assessment invitations processed.",
            status=200,
            data={"data": result},
        )

    @validate_token
    @http.route(
        "/api/v1/applicant-assessment/list",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({})
    def list_applicant_assessments(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}

            page = max(1, _coerce_int(jdata.get("page"), 1) or 1)
            per_page = _coerce_int(jdata.get("limit"), LIST_DEFAULT_LIMIT)
            per_page = max(1, min(per_page or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT))
            offset = (page - 1) * per_page

            domain = []

            state = jdata.get("state")
            if isinstance(state, str) and state.strip():
                s = state.strip()
                if s not in _ASSESSMENT_STATE_KEYS:
                    return return_Response(
                        message="Invalid state %r." % s, status=400,
                    )
                domain.append(("state", "=", s))
            elif isinstance(state, list) and state:
                for s in state:
                    if s not in _ASSESSMENT_STATE_KEYS:
                        return return_Response(
                            message="Invalid state %r." % s, status=400,
                        )
                domain.append(("state", "in", state))

            result = jdata.get("result")
            if isinstance(result, str) and result.strip():
                r = result.strip()
                if r not in _ASSESSMENT_RESULT_KEYS:
                    return return_Response(
                        message="Invalid result %r." % r, status=400,
                    )
                domain.append(("result", "=", r))
            elif isinstance(result, list) and result:
                for r in result:
                    if r not in _ASSESSMENT_RESULT_KEYS:
                        return return_Response(
                            message="Invalid result %r." % r, status=400,
                        )
                domain.append(("result", "in", result))

            job_id = _coerce_int(jdata.get("job_id"))
            if job_id:
                domain.append(("job_id", "=", job_id))

            template_id = _coerce_int(jdata.get("template_id"))
            if template_id:
                domain.append(("template_id", "=", template_id))

            applicant_id = _coerce_int(jdata.get("applicant_id"))
            if applicant_id:
                domain.append(("applicant_id", "=", applicant_id))

            search = (jdata.get("search") or "").strip()
            if search:
                domain += [
                    "|", "|",
                    ("applicant_id.partner_name", "ilike", search),
                    ("applicant_id.email_from", "ilike", search),
                    ("template_id.name", "ilike", search),
                ]

            Assessment = request.env["etp.applicant.assessment"].sudo()
            total = Assessment.search_count(domain)
            total_pages = (total + per_page - 1) // per_page if total else 0

            records = Assessment.search(
                domain, offset=offset, limit=per_page,
                order="create_date desc",
            )
            serialized = [_serialize_assessment_row(r) for r in records]
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to list applicant assessments via API.")
            return return_Response(
                message="Failed to list applicant assessments.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Applicant assessments fetched successfully.",
            status=200,
            data={
                "records": serialized,
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
        "/api/v1/applicant-assessment/detail",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({})
    def get_applicant_assessment_detail(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            assessment_id = _coerce_int(jdata.get("id"))
            token = jdata.get("access_token")

            Assessment = request.env["etp.applicant.assessment"].sudo()
            record = Assessment.browse()

            if assessment_id and assessment_id > 0:
                record = Assessment.browse(assessment_id).exists()
            elif isinstance(token, str) and token.strip():
                record = Assessment.search(
                    [("access_token", "=", token.strip())], limit=1,
                )
            else:
                return return_Response(
                    message="Either 'id' or 'access_token' is required.",
                    status=400,
                )

            if not record:
                return return_Response(
                    message="Applicant assessment not found.", status=404,
                )

            detail = _serialize_assessment_detail(record)
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to load applicant-assessment detail.")
            return return_Response(
                message="Failed to load applicant assessment.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Applicant assessment fetched successfully.",
            status=200,
            data={"data": detail},
        )

    @http.route(
        "/api/v1/assessment-template/sample-json",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    def sample_template_json(self, **kwargs):
        module_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        sample_path = os.path.join(
            module_root, "data", "samples",
            "assessment_template_sample.json",
        )
        try:
            with open(sample_path, "r", encoding="utf-8") as f:
                payload = f.read()
        except OSError as e:
            _logger.error("Sample JSON not readable: %s", e)
            return return_Response(
                message="Sample JSON not available.", status=500,
            )
        try:
            json.loads(payload)
        except json.JSONDecodeError as e:
            _logger.error("Sample JSON invalid: %s", e)
            return return_Response(
                message="Sample JSON is malformed on server.", status=500,
            )
        return http.Response(
            payload, status=200, mimetype="application/json",
        )

    @validate_token
    @http.route(
        "/api/v1/assessment-template/list",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({})
    def list_assessment_templates(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}

            page = max(1, _coerce_int(jdata.get("page"), 1) or 1)
            per_page = _coerce_int(jdata.get("limit"), LIST_DEFAULT_LIMIT)
            per_page = max(1, min(per_page or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT))
            offset = (page - 1) * per_page

            domain = []
            search = (jdata.get("search") or "").strip()
            if search:
                domain.append(("name", "ilike", search))

            status = jdata.get("status")
            if isinstance(status, str) and status.strip():
                status = status.strip()
                if status not in _TEMPLATE_STATUS_KEYS:
                    return return_Response(
                        message="Invalid status %r." % status, status=400,
                    )
                domain.append(("status", "=", status))
            elif isinstance(status, list) and status:
                for s in status:
                    if s not in _TEMPLATE_STATUS_KEYS:
                        return return_Response(
                            message="Invalid status %r." % s, status=400,
                        )
                domain.append(("status", "in", status))

            job_id = _coerce_int(jdata.get("job_id"))
            if job_id:
                domain.append(("job_id", "=", job_id))

            include_archived = _to_bool(jdata.get("include_archived"))
            archived_only = _to_bool(jdata.get("archived_only"))
            if archived_only:
                domain.append(("active", "=", False))
            elif include_archived:
                domain = ["|", ("active", "=", True), ("active", "=", False)] + domain

            Template = request.env["etp.applicant.assessment.template"].sudo()
            total = Template.search_count(domain)
            total_pages = (total + per_page - 1) // per_page if total else 0

            templates = Template.search(
                domain, offset=offset, limit=per_page, order="name asc",
            )

            Assessment = request.env["etp.applicant.assessment"].sudo()
            assigned_map = {}
            if templates:
                grouped = Assessment.read_group(
                    [("template_id", "in", templates.ids)],
                    ["template_id"],
                    ["template_id"],
                )
                assigned_map = {
                    g["template_id"][0]: g["template_id_count"] for g in grouped
                }

            records = [_serialize_template_row(t, assigned_map) for t in templates]
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to list assessment templates via API.")
            return return_Response(
                message="Failed to list templates.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Assessment templates fetched successfully.",
            status=200,
            data={
                "records": records,
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
        "/api/v1/assessment-template/publish",
        methods=["POST"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_request({"id": {"type": "int", "required": True}})
    def publish_assessment_template(self, **kwargs):
        try:
            jdata = kwargs.get("jdata") or {}
            tpl_id = _coerce_int(jdata.get("id"))
            if not tpl_id or tpl_id <= 0:
                return return_Response(message="id is required.", status=400)

            Template = request.env["etp.applicant.assessment.template"].sudo()
            template = Template.with_context(active_test=False).browse(tpl_id)
            if not template.exists():
                return return_Response(
                    message="Template id=%d not found." % tpl_id, status=404,
                )

            if template.question_count <= 0:
                return return_Response(
                    message="Cannot publish a template with no questions.",
                    status=400,
                )

            template.write({"status": "public", "active": True})
        except ValidationError as ve:
            return return_Response(message=str(ve), status=400)
        except Exception as e:
            _logger.exception("Failed to publish assessment template via API.")
            return return_Response(
                message="Failed to publish template.",
                status=400,
                errors=[str(e)],
            )

        return return_Response(
            message="Assessment template published successfully.",
            status=200,
            data={
                "data": {
                    "id": template.id,
                    "name": template.name,
                    "status": template.status,
                    "active": template.active,
                },
            },
        )
