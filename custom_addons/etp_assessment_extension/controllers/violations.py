"""Violations dashboard endpoints.

Reads live from `etp.assessment.evaluator` (the candidate assignment model)
where `is_violated = True`. Exposes:

  - GET /api/v1/etp_assessment_ext/violations         (paginated table)
  - GET /api/v1/etp_assessment_ext/violations/summary (dashboard cards)
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    coerce_int,
    paginate,
    pagination_block,
    pct,
    require_assessment_user,
    resolve_order,
    user_role_tag,
)

VIOLATION_COLUMNS = [
    {"key": "employee_name", "label": "Candidate", "type": "string"},
    {"key": "employee_email", "label": "Email", "type": "string"},
    {"key": "assessment_name", "label": "Assessment", "type": "string"},
    {"key": "violation_reason_label", "label": "Reason", "type": "string"},
    {"key": "violation_datetime", "label": "When", "type": "datetime"},
    {"key": "state_label", "label": "Candidate State", "type": "string"},
    {"key": "is_locked", "label": "Locked", "type": "boolean"},
]

SORT_FIELDS = {
    "violation_datetime": "violation_datetime",
    "create_date": "create_date",
    "employee_name": "employee_id",
    "assessment_name": "assessment_id",
}

REASON_LABELS = {
    "tab_switch": "Tab Switch",
    "right_click": "Right Click Attempt",
    "screenshot_attempt": "Screenshot Attempt",
    "screen_capture": "Screen Capture",
    "devtools_open": "DevTools Opened",
    "window_blur": "Window Lost Focus",
    "copy_attempt": "Copy / Paste",
}


def _reason_label(raw):
    raw = (raw or "").strip()
    if not raw:
        return "Unspecified"
    return REASON_LABELS.get(raw, raw.replace("_", " ").title())


def _serialize_violation(rec, state_labels):
    emp = rec.employee_id
    return {
        "id": rec.id,
        "assessment_id": rec.assessment_id.id if rec.assessment_id else 0,
        "assessment_name": rec.assessment_id.name if rec.assessment_id else "",
        "employee_id": emp.id if emp else 0,
        "employee_name": emp.name if emp else "",
        "employee_email": (
            (emp.work_email or emp.private_email) if emp else ""
        ),
        "violation_reason": rec.violation_reason or "",
        "violation_reason_label": _reason_label(rec.violation_reason),
        "violation_datetime": (
            rec.violation_datetime.isoformat()
            if rec.violation_datetime else None
        ),
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "is_locked": bool(rec.is_locked),
        "answered_count": rec.answered_count or 0,
        "total_questions": rec.total_questions or 0,
        "total_score": rec.total_score or 0,
        "max_possible_score": rec.max_possible_score or 0,
    }


def _build_domain(params):
    domain = [("is_violated", "=", True)]
    assessment_id = coerce_int(params.get("assessment_id"), 0)
    if assessment_id:
        domain.append(("assessment_id", "=", assessment_id))
    employee_id = coerce_int(params.get("employee_id"), 0)
    if employee_id:
        domain.append(("employee_id", "=", employee_id))
    reason = (params.get("violation_reason") or "").strip()
    if reason:
        domain.append(("violation_reason", "=", reason))
    date_from = (params.get("date_from") or "").strip()
    if date_from:
        domain.append(("violation_datetime", ">=", date_from))
    date_to = (params.get("date_to") or "").strip()
    if date_to:
        domain.append(("violation_datetime", "<=", date_to))
    return domain


class EtpAssessmentViolationController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/violations",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_violations(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain = _build_domain(params)
        order, error = resolve_order(
            params, SORT_FIELDS, "violation_datetime", "desc",
        )
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Evaluator = env["etp.assessment.evaluator"].sudo()
        total = Evaluator.search_count(domain)
        records = Evaluator.search(
            domain, limit=limit, offset=offset, order=order,
        )
        state_labels = dict(Evaluator._fields["state"].selection)
        rows = [_serialize_violation(r, state_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Violations",
                    "columns": VIOLATION_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/violations/summary",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def violations_summary(self, **kwargs):
        """Dashboard cards: totals, by-reason breakdown and recent table."""
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain = _build_domain(params)

        Evaluator = env["etp.assessment.evaluator"].sudo()
        records = Evaluator.search(domain, order="violation_datetime desc")

        total = len(records)
        affected_candidates = len({
            r.employee_id.id for r in records if r.employee_id
        })
        affected_assessments = len({
            r.assessment_id.id for r in records if r.assessment_id
        })
        locked_count = sum(1 for r in records if r.is_locked)

        by_reason = {}
        for r in records:
            key = r.violation_reason or "unspecified"
            by_reason[key] = by_reason.get(key, 0) + 1

        kpi_items = [
            {
                "key": "total_violations",
                "label": "Total Violations",
                "value": str(total),
                "sub_string": f"{locked_count} candidate(s) locked",
            },
            {
                "key": "affected_candidates",
                "label": "Affected Candidates",
                "value": str(affected_candidates),
                "sub_string": "",
            },
            {
                "key": "affected_assessments",
                "label": "Affected Assessments",
                "value": str(affected_assessments),
                "sub_string": "",
            },
        ]
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            kpi_items.append({
                "key": f"reason_{reason}",
                "label": _reason_label(reason),
                "value": str(count),
                "sub_string": f"{pct(count, total)}%",
            })

        chart_items = [
            {
                "label": _reason_label(k),
                "key": k,
                "value": v,
                "percent": pct(v, total),
            }
            for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])
        ]

        state_labels = dict(Evaluator._fields["state"].selection)
        recent_rows = [
            _serialize_violation(r, state_labels) for r in records[:10]
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [
                    {"type": "kpi", "items": kpi_items},
                    {
                        "type": "chart",
                        "variant": "doughnut",
                        "title": "Violations by reason",
                        "items": chart_items,
                    },
                    {
                        "type": "table",
                        "title": "Recent violations",
                        "columns": VIOLATION_COLUMNS,
                        "rows": recent_rows,
                    },
                ],
            },
        )
