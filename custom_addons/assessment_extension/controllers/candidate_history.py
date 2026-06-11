# -*- coding: utf-8 -*-
"""SCR-099 · Candidate History (one person's results across all assessments).

Endpoint:

  GET /api/v1/assessment_ext/candidate_history?candidate_id=N

A read-only roll-up reached from the SCR-096 drill-in header
('View full history').
"""
from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    assessment_code,
    coerce_int,
    employee_card,
    iso_or_none,
    require_monitor_user,
    role_tag,
    score_band,
    status_pill_recipe,
)


def _history_status(evaluator, pass_threshold):
    """Map a candidate's assessment state to the SCR-099 status pill."""
    a = evaluator.assessment_id
    if a.state == "in_progress":
        return "in_progress"
    if evaluator.submitted_total == 0:
        return "in_progress"
    if evaluator.overall_mean >= pass_threshold:
        return "passed"
    return "failed"


class CandidateHistoryController(http.Controller):

    @http.route(
        "/api/v1/assessment_ext/candidate_history",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def candidate_history(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        candidate_id = coerce_int(params.get("candidate_id"))
        if not candidate_id:
            return return_Response(
                message="'candidate_id' is required.", status=400,
            )

        employee = env["hr.employee"].sudo().browse(candidate_id)
        if not employee.exists():
            return return_Response(message="Candidate not found", status=404)

        Evaluator = env["etp.assessment.evaluator"].sudo()
        domain = [("employee_id", "=", employee.id)]
        # PL only sees their own assessments
        if role_tag(env) == "pl":
            domain.append(("assessment_id.create_uid", "=", env.user.id))
        evaluators = Evaluator.search(domain, order="create_date desc")

        rows = []
        passed_count = 0
        total_score_sum = 0
        scored_n = 0
        for ev in evaluators:
            a = ev.assessment_id
            if not a:
                continue
            pt = a.pass_threshold or 70
            status = _history_status(ev, pt)
            mean = round(ev.overall_mean, 1) if ev.submitted_total else None
            if status == "passed":
                passed_count += 1
            if mean is not None and a.state in ("done", "cancelled"):
                total_score_sum += mean
                scored_n += 1
            rows.append({
                "evaluator_id": ev.id,
                "assessment": {
                    "id": a.id,
                    "code": assessment_code(a),
                    "name": a.name or "",
                    "cohort_label": a.cohort_label or "",
                },
                "window": {
                    "start": iso_or_none(a.start_date),
                    "end": iso_or_none(a.end_date),
                },
                "status": status,
                "status_pill": status_pill_recipe(status),
                "score": mean,
                "score_band": score_band(mean, pt),
                "submitted_total": ev.submitted_total,
                "submissions_expected": ev.submissions_expected,
            })

        total_taken = len(rows)
        avg_score = round(total_score_sum / scored_n, 1) if scored_n else None
        emp_card = employee_card(employee)
        header_summary = (
            f"Passed {passed_count} of {total_taken}" if total_taken else "No assessments yet"
        )

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "candidate": {
                    **(emp_card or {}),
                    "joined": iso_or_none(employee.create_date),
                },
                "header": {
                    "summary_chip": header_summary,
                    "passed_count": passed_count,
                    "total_count": total_taken,
                },
                "kpis": [
                    {
                        "key": "assessments_taken",
                        "label": "Assessments taken",
                        "value": total_taken,
                        "band": "neutral",
                    },
                    {
                        "key": "passed",
                        "label": "Passed",
                        "value": passed_count,
                        "band": "success",
                    },
                    {
                        "key": "avg_score",
                        "label": "Avg score",
                        "value": avg_score,
                        "band": "info",
                        "sub_context": "across completed",
                    },
                ],
                "rows": rows,
                "row_count": total_taken,
                "empty_state": (
                    "This person hasn't taken any assessments yet."
                    if total_taken == 0 else ""
                ),
            },
        )
