"""Candidate (evaluator) assignment endpoints for an assessment.

Mirrors `etp.assessment.action_import_candidates_csv` over HTTP and exposes
the per-assessment candidate roster (assignments + progress + score).
"""

import base64
import csv
import io

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    EVALUATOR_STATES,
    coerce_int,
    paginate,
    pagination_block,
    pct,
    require_assessment_manager,
    require_assessment_user,
    user_role_tag,
)

CANDIDATE_COLUMNS = [
    {"key": "employee_name", "label": "Candidate", "type": "string"},
    {"key": "employee_email", "label": "Email", "type": "string"},
    {"key": "state_label", "label": "State", "type": "string"},
    {"key": "answered_count", "label": "Answered", "type": "integer"},
    {"key": "total_questions", "label": "Total", "type": "integer"},
    {"key": "progress_percent", "label": "Progress %", "type": "float"},
    {"key": "total_score", "label": "Score", "type": "integer"},
    {"key": "max_possible_score", "label": "Max", "type": "integer"},
    {"key": "is_violated", "label": "Violated", "type": "boolean"},
    {"key": "started_at", "label": "Started", "type": "datetime"},
    {"key": "deadline_datetime", "label": "Deadline", "type": "datetime"},
]


def _serialize_assignment(rec, state_labels):
    emp = rec.employee_id
    return {
        "id": rec.id,
        "assessment_id": rec.assessment_id.id if rec.assessment_id else 0,
        "employee_id": emp.id if emp else 0,
        "employee_name": emp.name if emp else "",
        "employee_email": (emp.work_email or emp.private_email) if emp else "",
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "access_token": rec.access_token or "",
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "deadline_datetime": (
            rec.deadline_datetime.isoformat()
            if rec.deadline_datetime else None
        ),
        "total_questions": rec.total_questions or 0,
        "answered_count": rec.answered_count or 0,
        "progress_percent": pct(rec.answered_count, rec.total_questions),
        "total_score": rec.total_score or 0,
        "max_possible_score": rec.max_possible_score or 0,
        "is_locked": bool(rec.is_locked),
        "is_violated": bool(rec.is_violated),
        "violation_reason": rec.violation_reason or "",
        "violation_datetime": (
            rec.violation_datetime.isoformat()
            if rec.violation_datetime else None
        ),
    }


def _import_csv_rows(csv_bytes):
    """Read a candidates CSV stream into a list of clean dict rows.

    Required columns: name, email. Optional: job_title, department.
    Returns (rows, errors) tuples where rows are dicts {name, email,
    job_title, department}.
    """
    try:
        decoded = csv_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["Invalid CSV file: must be UTF-8 encoded."]

    reader = csv.DictReader(io.StringIO(decoded))
    fieldnames = set(reader.fieldnames or [])
    required = {"name", "email"}
    if not required.issubset(fieldnames):
        return None, [
            "CSV must contain 'name' and 'email' columns. "
            f"Found: {', '.join(sorted(fieldnames)) or '(none)'}"
        ]

    rows = []
    errors = []
    for idx, row in enumerate(reader, start=2):
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        if not name or not email:
            errors.append(f"Row {idx}: 'name' and 'email' are required.")
            continue
        rows.append({
            "name": name,
            "email": email,
            "job_title": (row.get("job_title") or "").strip(),
            "department": (row.get("department") or "").strip(),
        })
    return rows, errors


def _resolve_or_create_employee(env, row):
    Employee = env["hr.employee"].sudo()
    employee = Employee.search([("work_email", "=", row["email"])], limit=1)
    if employee:
        return employee, False

    vals = {"name": row["name"], "work_email": row["email"]}
    if row.get("job_title"):
        vals["job_title"] = row["job_title"]
    if row.get("department"):
        dept = (
            env["hr.department"].sudo()
            .search([("name", "=", row["department"])], limit=1)
        )
        if dept:
            vals["department_id"] = dept.id
    return Employee.create(vals), True


class EtpAssessmentCandidateController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_candidates(self, assessment_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        assessment = env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        params = request.params or {}
        Evaluator = env["etp.assessment.evaluator"].sudo()
        state_labels = dict(Evaluator._fields["state"].selection)

        domain = [("assessment_id", "=", assessment_id)]
        state = (params.get("state") or "").strip()
        if state:
            if state not in EVALUATOR_STATES:
                return return_Response(
                    message=(
                        f"Invalid state '{state}'. "
                        f"Allowed: {', '.join(EVALUATOR_STATES)}."
                    ),
                    status=400,
                )
            domain.append(("state", "=", state))

        page, limit, offset = paginate(params)
        total = Evaluator.search_count(domain)
        records = Evaluator.search(
            domain, limit=limit, offset=offset, order="create_date desc, id desc",
        )
        rows = [_serialize_assignment(r, state_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Candidates",
                    "columns": CANDIDATE_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates/<int:assignment_id>/detail",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_candidate_detail(self, assessment_id, assignment_id, **kwargs):
        """Per-candidate deep view: their responses, question by question.

        `assignment_id` is the `etp.assessment.evaluator` PK (i.e. the row
        the candidate represents in the candidate list of an assessment).
        Returns the candidate header, plus one entry per question they were
        served, with score / state / per-dimension selected option.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.browse(assignment_id)
        if not assignment.exists():
            return return_Response(
                message="Candidate assignment not found", status=404,
            )
        if assignment.assessment_id.id != assessment_id:
            return return_Response(
                message="Candidate assignment does not belong to this assessment",
                status=404,
            )

        state_labels = dict(Evaluator._fields["state"].selection)
        Response = env["etp.assessment.response"].sudo()
        resp_state_labels = dict(Response._fields["state"].selection)
        Question = env["etp.assessment.question"].sudo()
        type_labels = dict(Question._fields["question_type"].selection)

        responses = Response.search(
            [("assessment_evaluator_id", "=", assignment_id)],
            order="create_date asc, id asc",
        )

        response_rows = []
        for r in responses:
            q = r.question_id
            lines = []
            for line in r.line_ids:
                lines.append({
                    "id": line.id,
                    "dimension_id": (
                        line.dimension_id.id if line.dimension_id else 0
                    ),
                    "dimension_name": (
                        line.dimension_id.name if line.dimension_id else ""
                    ),
                    "selected_option_id": (
                        line.selected_option_id.id
                        if line.selected_option_id else 0
                    ),
                    "selected_option_name": (
                        line.selected_option_id.name
                        if line.selected_option_id else ""
                    ),
                })
            response_rows.append({
                "id": r.id,
                "question_id": q.id if q else 0,
                "question_name": q.name if q else "",
                "question_type": q.question_type if q else "",
                "question_type_label": (
                    type_labels.get(q.question_type or "", "") if q else ""
                ),
                "category_id": q.category_id.id if q and q.category_id else 0,
                "category_name": (
                    q.category_id.name if q and q.category_id else ""
                ),
                "justification": r.justification or "",
                "state": r.state,
                "state_label": resp_state_labels.get(r.state, ""),
                "score": r.score or 0,
                "max_score": r.max_score or 0,
                "lines": lines,
                "create_date": (
                    r.create_date.isoformat() if r.create_date else None
                ),
            })

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "candidate": _serialize_assignment(assignment, state_labels),
                "assessment": {
                    "id": assignment.assessment_id.id,
                    "name": assignment.assessment_id.name or "",
                    "duration_minutes": (
                        assignment.assessment_id.duration_minutes or 0
                    ),
                    "state": assignment.assessment_id.state,
                },
                "responses": response_rows,
                "summary": {
                    "total_questions": assignment.total_questions or 0,
                    "answered_count": assignment.answered_count or 0,
                    "total_score": assignment.total_score or 0,
                    "max_possible_score": assignment.max_possible_score or 0,
                    "progress_percent": pct(
                        assignment.answered_count, assignment.total_questions,
                    ),
                    "is_violated": bool(assignment.is_violated),
                    "violation_reason": assignment.violation_reason or "",
                },
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "employee_ids": {"type": "list", "required": True},
    })
    def add_candidates(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        assessment = (
            request.env["etp.assessment"].sudo().browse(assessment_id)
        )
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        requested_ids = [
            coerce_int(eid, 0) for eid in jdata.get("employee_ids") or []
        ]
        requested_ids = [i for i in requested_ids if i]
        if not requested_ids:
            return return_Response(
                message="'employee_ids' must be a non-empty list of integers.",
                status=400,
            )

        existing_ids = set(assessment.evaluator_ids.ids)
        new_ids = [eid for eid in requested_ids if eid not in existing_ids]
        if new_ids:
            assessment.write({
                "evaluator_ids": [(4, eid) for eid in new_ids],
            })

        return return_Response(
            message=f"{len(new_ids)} candidate(s) added",
            status=200,
            data={
                "added_employee_ids": new_ids,
                "already_assigned_ids": [
                    eid for eid in requested_ids if eid in existing_ids
                ],
                "candidate_ids": assessment.evaluator_ids.ids,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates/<int:employee_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def remove_candidate(self, assessment_id, employee_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = (
            request.env["etp.assessment"].sudo().browse(assessment_id)
        )
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)
        if employee_id not in assessment.evaluator_ids.ids:
            return return_Response(
                message="Employee is not assigned to this assessment",
                status=404,
            )
        if assessment.state != "draft":
            return return_Response(
                message=(
                    "Candidates can only be removed while the assessment is "
                    "in draft state."
                ),
                status=400,
            )
        assessment.write({"evaluator_ids": [(3, employee_id)]})
        return return_Response(
            message="Candidate removed",
            status=200,
            data={"candidate_ids": assessment.evaluator_ids.ids},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates/bulk_import",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def bulk_import_candidates(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        env = request.env
        assessment = env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

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
                    "Upload a CSV via multipart 'file' or send a base64 string "
                    "in 'file_b64'."
                ),
                status=400,
            )

        rows, parse_errors = _import_csv_rows(csv_bytes)
        if rows is None:
            return return_Response(
                message=parse_errors[0] if parse_errors else "Invalid CSV",
                status=400,
                errors=parse_errors,
            )

        created_employees = []
        imported_ids = []
        row_errors = list(parse_errors)
        for row in rows:
            try:
                employee, was_created = _resolve_or_create_employee(env, row)
                imported_ids.append(employee.id)
                if was_created:
                    created_employees.append({
                        "id": employee.id,
                        "name": employee.name,
                        "email": employee.work_email,
                    })
            except Exception as exc:
                row_errors.append(f"Row '{row['email']}': {exc}")

        existing_ids = set(assessment.evaluator_ids.ids)
        new_ids = [eid for eid in imported_ids if eid not in existing_ids]
        already = [eid for eid in imported_ids if eid in existing_ids]
        if new_ids:
            assessment.write({
                "evaluator_ids": [(4, eid) for eid in new_ids],
            })

        return return_Response(
            message=(
                f"{len(new_ids)} candidate(s) added, "
                f"{len(already)} already assigned, "
                f"{len(created_employees)} new employee(s) created."
            ),
            status=200,
            errors=row_errors,
            data={
                "added_employee_ids": new_ids,
                "already_assigned_ids": already,
                "created_employees": created_employees,
                "candidate_ids": assessment.evaluator_ids.ids,
            },
        )
