"""Assessment dashboard overview: block-based schema (kpi + chart + table)
that the frontend renders generically. Mirrors `etp.assessment.get_dashboard_data`
but reshaped into the same block contract used by aurora_extension /
crowley_extension.
"""

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    QUESTION_TYPES,
    coerce_int,
    pct,
    require_assessment_user,
    user_role_tag,
)

QUESTION_TYPE_LABELS = {
    "image_comparison": "Image Comparison",
    "text": "Text",
    "coding": "Coding",
    "image_text": "Image + Text",
    "video": "Video",
}

ACTIVE_WORK_COLUMNS = [
    {"key": "name", "label": "Assessment", "type": "string"},
    {"key": "evaluators_total", "label": "Candidates", "type": "integer"},
    {"key": "evaluators_done", "label": "Submitted", "type": "integer"},
    {"key": "end_date", "label": "End Date", "type": "datetime"},
]

TOP_CANDIDATE_COLUMNS = [
    {"key": "name", "label": "Candidate", "type": "string"},
    {"key": "assessment_name", "label": "Assessment", "type": "string"},
    {"key": "total_score", "label": "Score", "type": "integer"},
    {"key": "max_possible", "label": "Max", "type": "integer"},
    {"key": "total_questions", "label": "Questions", "type": "integer"},
]


def _kpi_item(key, label, value, sub_string=""):
    return {
        "key": key,
        "label": label,
        "value": str(value),
        "sub_string": sub_string,
    }


def _build_assessment_domain(params):
    domain = []
    assessment_id = coerce_int(params.get("assessment_id"), 0)
    if assessment_id:
        domain.append(("id", "=", assessment_id))
    state = (params.get("state") or "").strip()
    if state:
        domain.append(("state", "=", state))
    date_from = (params.get("date_from") or "").strip()
    if date_from:
        domain.append(("start_date", ">=", date_from))
    date_to = (params.get("date_to") or "").strip()
    if date_to:
        domain.append(("end_date", "<=", date_to))
    category_id = coerce_int(params.get("category_id"), 0)
    if category_id:
        domain.append(("category_id", "=", category_id))
    return domain


def _kpi_block(env, assessment_domain, has_filters, filtered_ids):
    Assessment = env["etp.assessment"].sudo()
    Question = env["etp.assessment.question"].sudo()
    Evaluator = env["etp.assessment.evaluator"].sudo()
    Response = env["etp.assessment.response"].sudo()

    if has_filters:
        ev_domain = [("assessment_id", "in", filtered_ids)]
        resp_domain = [("assessment_id", "in", filtered_ids)]
        question_ids = (
            Assessment.search(assessment_domain).mapped("question_ids").ids
        )
        q_domain = [("id", "in", question_ids), ("active", "=", True)]
        total_assessments = len(filtered_ids)
        in_progress_count = Assessment.search_count(
            assessment_domain + [("state", "=", "in_progress")]
        )
        done_count = Assessment.search_count(
            assessment_domain + [("state", "=", "done")]
        )
    else:
        ev_domain = []
        resp_domain = []
        q_domain = [("active", "=", True)]
        total_assessments = Assessment.search_count([])
        in_progress_count = Assessment.search_count([("state", "=", "in_progress")])
        done_count = Assessment.search_count([("state", "=", "done")])

    total_questions = Question.search_count(q_domain)
    total_evaluators = Evaluator.search_count(ev_domain)
    evaluators_submitted = Evaluator.search_count(
        ev_domain + [("state", "=", "submitted")]
    )
    total_responses = Response.search_count(resp_domain)
    total_violators = Evaluator.search_count(
        ev_domain + [("is_violated", "=", True)]
    )
    completion_rate = pct(evaluators_submitted, total_evaluators)

    return [
        _kpi_item(
            "total_assessments", "Total Assessments", total_assessments,
            f"{in_progress_count} in progress, {done_count} done",
        ),
        _kpi_item("total_questions", "Questions", total_questions),
        _kpi_item(
            "total_candidates", "Candidates", total_evaluators,
            f"{evaluators_submitted} submitted",
        ),
        _kpi_item("total_responses", "Responses", total_responses),
        _kpi_item(
            "completion_rate", "Completion Rate", f"{completion_rate}%",
            f"{total_violators} violation(s)",
        ),
    ]


def _question_types_chart(env, has_filters, filtered_ids):
    Question = env["etp.assessment.question"].sudo()
    if has_filters:
        question_ids = (
            env["etp.assessment"].sudo().browse(filtered_ids).mapped("question_ids").ids
        )
        base_domain = [("id", "in", question_ids), ("active", "=", True)]
    else:
        base_domain = [("active", "=", True)]

    total = Question.search_count(base_domain)
    items = []
    for qtype in QUESTION_TYPES:
        count = Question.search_count(base_domain + [("question_type", "=", qtype)])
        items.append({
            "label": QUESTION_TYPE_LABELS[qtype],
            "key": qtype,
            "value": count,
            "percent": pct(count, total),
        })
    return items


def _categories_chart(env, has_filters, filtered_ids):
    Question = env["etp.assessment.question"].sudo()
    Category = env["etp.assessment.category"].sudo()

    if has_filters:
        question_ids = (
            env["etp.assessment"].sudo().browse(filtered_ids).mapped("question_ids").ids
        )
        base_domain = [("id", "in", question_ids), ("active", "=", True)]
    else:
        base_domain = [("active", "=", True)]

    total = Question.search_count(base_domain)
    items = []
    for cat in Category.search([("active", "=", True)], order="sequence, id"):
        count = Question.search_count(base_domain + [("category_id", "=", cat.id)])
        items.append({
            "label": cat.name or "",
            "key": str(cat.id),
            "value": count,
            "percent": pct(count, total),
        })
    return items


def _active_work_rows(env, has_filters, filtered_ids):
    Assessment = env["etp.assessment"].sudo()
    Evaluator = env["etp.assessment.evaluator"].sudo()
    if has_filters:
        domain = [("id", "in", filtered_ids)]
    else:
        domain = [("state", "=", "in_progress")]

    rows = []
    for a in Assessment.search(domain, limit=10, order="start_date desc, id desc"):
        total = Evaluator.search_count([("assessment_id", "=", a.id)])
        done = Evaluator.search_count(
            [("assessment_id", "=", a.id), ("state", "=", "submitted")]
        )
        rows.append({
            "id": a.id,
            "name": a.name or "",
            "state": a.state,
            "evaluators_total": total,
            "evaluators_done": done,
            "end_date": a.end_date.isoformat() if a.end_date else None,
            "progress_percent": pct(done, total),
        })
    return rows


def _top_candidate_rows(env, ev_domain):
    Evaluator = env["etp.assessment.evaluator"].sudo()
    submitted_domain = ev_domain + [("state", "=", "submitted")]
    submitted = Evaluator.search(submitted_domain, limit=20, order="total_score desc")
    rows = []
    for ev in submitted:
        rows.append({
            "id": ev.id,
            "name": ev.employee_id.name or "",
            "assessment_name": ev.assessment_id.name or "",
            "total_score": ev.total_score or 0,
            "max_possible": ev.max_possible_score or 0,
            "total_questions": ev.total_questions or 0,
        })
    return rows


class EtpAssessmentDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def etp_assessment_ext_dashboard_overview(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}

        assessment_domain = _build_assessment_domain(params)
        has_filters = bool(assessment_domain)
        filtered_ids = (
            env["etp.assessment"].sudo().search(assessment_domain).ids
            if has_filters else []
        )
        ev_domain = (
            [("assessment_id", "in", filtered_ids)] if has_filters else []
        )

        blocks = [
            {
                "type": "kpi",
                "items": _kpi_block(
                    env, assessment_domain, has_filters, filtered_ids,
                ),
            },
            {
                "type": "chart",
                "variant": "doughnut",
                "title": "Question types",
                "items": _question_types_chart(env, has_filters, filtered_ids),
            },
            {
                "type": "chart",
                "variant": "bar",
                "title": "Questions per category",
                "items": _categories_chart(env, has_filters, filtered_ids),
            },
            {
                "type": "table",
                "title": "Active assessments",
                "columns": ACTIVE_WORK_COLUMNS,
                "rows": _active_work_rows(env, has_filters, filtered_ids),
            },
            {
                "type": "table",
                "title": "Top candidates",
                "columns": TOP_CANDIDATE_COLUMNS,
                "rows": _top_candidate_rows(env, ev_domain),
            },
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": blocks,
            },
        )
