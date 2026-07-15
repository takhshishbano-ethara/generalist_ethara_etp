import json
import logging
import os

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_token, validate_request, return_Response,
)
from odoo.addons.etp_applicant_assessment.models._common import MCQ_TYPES

_logger = logging.getLogger(__name__)


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
