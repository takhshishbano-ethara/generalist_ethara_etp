# -*- coding: utf-8 -*-
"""Shared helpers for the assessment_extension controllers.

Pattern mirrors etp_assessment_extension/common.py + aurora_extension /
crowley_extension. Every endpoint goes through:
  - api_auth_gateway.validate_token (Access-Token header)
  - one of the role-gate helpers below
  - return_Response envelope
"""
import json

from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response


DEFAULT_LIMIT = 25
MAX_LIMIT = 200

TASK_TYPES = ("eval_compare", "prompt_writing", "bbox_labeling")
TASK_TYPE_LABELS = {
    "eval_compare": "Eval Compare",
    "prompt_writing": "Prompt Writing",
    "bbox_labeling": "BBox Labeling",
}
TASK_TYPE_PILL = {
    "eval_compare": {"bg": "#EFF6FF", "text": "#1D4ED8", "dot": "#1D4ED8"},
    "prompt_writing": {"bg": "#F0EDFF", "text": "#3927BF", "dot": "#3927BF"},
    "bbox_labeling": {"bg": "#ECFEFF", "text": "#0E7490", "dot": "#0E7490"},
}

OVERRIDE_REASONS = ("llm_synonym", "misdetected_boxes", "justification_valid", "other")


def coerce_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def parse_json_body():
    """Merge JSON body + query params (mirrors etp_assessment_extension)."""
    data = {}
    if request.params:
        data.update(request.params)
    try:
        jdata = json.loads(request.httprequest.stream.read())
    except Exception:
        try:
            jdata = json.loads(request.httprequest.data)
        except Exception:
            jdata = {}
    if jdata:
        data.update(jdata)
    return data


def role_tag(env):
    """Coarse role tag for the caller. WORKFLOW §13.

    'cto' — full sign-off incl. self-case approvals
    'hr'  — org-wide oversight (HR Admin)
    'pl'  — own assessments only
    None  — no access
    """
    user = env.user
    if user.has_group("assessment_extension.group_assessment_cto"):
        return "cto"
    if user.has_group("assessment_extension.group_assessment_hr_admin"):
        return "hr"
    if user.has_group("assessment_extension.group_assessment_pl"):
        return "pl"
    # Fallback: any existing etp_assessment manager is treated as a PL
    if user.has_group("etp_assessment.group_assessment_manager"):
        return "pl"
    return None


def require_monitor_user():
    """403 if the caller has no monitoring access."""
    if role_tag(request.env) is None:
        return return_Response(
            message="You are not allowed to access assessment monitoring data.",
            status=403,
        )
    return None


def require_cto():
    """403 if the caller is not a CTO. SCR-098 sign-off."""
    if role_tag(request.env) not in ("cto", "hr"):
        return return_Response(
            message="Only the CTO (or HR Admin) can act on override approvals.",
            status=403,
        )
    return None


def scoped_assessment_domain(env):
    """Restrict to the caller's own assessments unless HR/CTO.

    PL only sees the assessments they created/own (their direct reports'
    work). HR Admin and CTO see everything. Mirrors WORKFLOW §13.
    """
    if role_tag(env) in ("hr", "cto"):
        return []
    # PL: scope by create_uid (the PL is the owner)
    return [("create_uid", "=", env.user.id)]


def paginate(params):
    page = max(1, coerce_int(params.get("page"), 1))
    limit = min(
        max(1, coerce_int(params.get("limit"), DEFAULT_LIMIT)),
        MAX_LIMIT,
    )
    offset = (page - 1) * limit
    return page, limit, offset


def pagination_block(total, page, limit):
    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "total_records": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


def iso_or_none(value):
    return value.isoformat() if value else None


def score_band(score, pass_threshold=70):
    """Colour band for a score per the design alignment (SCR-096 §Design).

    ≥80 success / pass..79 info / 60..pass-1 warning / <60 destructive / None muted.
    """
    if score is None:
        return "muted"
    if score >= 80:
        return "success"
    if score >= pass_threshold:
        return "info"
    if score >= 60:
        return "warning"
    return "destructive"


def confidence_band(value):
    """Colour band for an LLM confidence value (0..1). §6.3."""
    if value is None:
        return "muted"
    if value < 0.6:
        return "warning"
    return "info"


def status_pill_recipe(status):
    """Map an internal status code to the WORKFLOW §7 pill recipe.

    Returns a {bg, text, dot, label} dict so the client doesn't need to know
    every recipe. Same recipes shared across SCR-095/096/099.
    """
    m = {
        # Assignment lifecycle (§7.3)
        "assigned": {"bg": "#F3F3F5", "text": "#717182", "dot": "#9CA3AF", "label": "Assigned"},
        "in_progress": {"bg": "#EFF6FF", "text": "#1D4ED8", "dot": "#1D4ED8", "label": "In progress"},
        "submitted": {"bg": "#F0EDFF", "text": "#3927BF", "dot": "#3927BF", "label": "Submitted"},
        "scored": {"bg": "#F0EDFF", "text": "#3927BF", "dot": "#3927BF", "label": "Scored"},
        "passed": {"bg": "#ECFDF5", "text": "#047857", "dot": "#10B981", "label": "Passed"},
        "failed": {"bg": "#FEF2F2", "text": "#B91C1C", "dot": "#EF4444", "label": "Failed"},
        "at_risk": {"bg": "#FFF7ED", "text": "#C2410C", "dot": "#F59E0B", "label": "At-risk"},
        "incomplete": {"bg": "#FFF7ED", "text": "#C2410C", "dot": "#F59E0B", "label": "Incomplete"},
        "locked": {"bg": "#F3F3F5", "text": "#717182", "dot": None, "label": "Locked"},
        "overridden": {"bg": "#F0EDFF", "text": "#3927BF", "dot": "#3927BF", "label": "Overridden"},
        "not_submitted": {"bg": "#F3F3F5", "text": "#9CA3AF", "dot": "#9CA3AF", "label": "Not submitted"},
        "pending_cto": {"bg": "#FFF7ED", "text": "#C2410C", "dot": "#F59E0B", "label": "Override pending CTO"},
    }
    return m.get(status, {"bg": "#F3F3F5", "text": "#717182", "dot": None, "label": (status or "").replace("_", " ").title()})


def task_type_pill(task_type):
    pill = TASK_TYPE_PILL.get(task_type, {"bg": "#F3F3F5", "text": "#717182", "dot": "#9CA3AF"})
    return {
        "task_type": task_type or None,
        "label": TASK_TYPE_LABELS.get(task_type, task_type or ""),
        **pill,
    }


def employee_card(employee, mono_id_field="barcode"):
    """Serialize an hr.employee → {id, name, code, initials}."""
    if not employee:
        return None
    name = employee.name or ""
    initials = "".join(part[:1].upper() for part in name.split()[:2]) or "?"
    code = ""
    if employee:
        code = (
            employee[mono_id_field]
            if mono_id_field in employee._fields and employee[mono_id_field]
            else ""
        )
        if not code:
            code = "EMP-%04d" % employee.id
    return {
        "id": employee.id,
        "name": name,
        "code": code,
        "initials": initials,
        "department": (
            employee.department_id.name
            if employee.department_id else ""
        ),
        "job_title": employee.job_title or "",
    }


def question_code(question):
    if not question:
        return ""
    if "code" in question._fields and question.code:
        return question.code
    return "QST-%05d" % question.id


def assessment_code(assessment):
    if not assessment:
        return ""
    if "code" in assessment._fields and assessment.code:
        return assessment.code
    return "ASM-%04d" % assessment.id


def submission_code(submission):
    if not submission:
        return ""
    if "code" in submission._fields and submission.code:
        return submission.code
    return "SUB-%06d" % submission.id


def reason_label(reason):
    return {
        "llm_synonym": "LLM penalized a correct synonym",
        "misdetected_boxes": "Mis-detected boxes",
        "justification_valid": "Justification valid",
        "other": "Other",
    }.get(reason, reason or "")
