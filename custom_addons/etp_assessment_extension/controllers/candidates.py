"""Candidate (evaluator) assignment endpoints for an assessment.

Mirrors `etp.assessment.action_import_candidates_csv` over HTTP and exposes
the per-assessment candidate roster (assignments + progress + score).
"""

import base64
import csv
import io
import json

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


CSV_TEMPLATE_BODY = (
    "name,email,job_title,department\n"
    "John Doe,john.doe@example.com,Evaluator,Engineering\n"
    "Jane Smith,jane.smith@example.com,Senior Evaluator,Design\n"
)


class EtpAssessmentCandidateController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/candidates/csv_template",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_candidate_csv_template(self, **kwargs):
        """Stream the same CSV body served by
        `etp.assessment.action_download_candidate_template()` so the
        frontend can offer a one-click template download."""
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        payload = CSV_TEMPLATE_BODY.encode("utf-8")
        return request.make_response(
            payload,
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                (
                    "Content-Disposition",
                    'attachment; filename="candidate_import_template.csv"',
                ),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
            ],
        )

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
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/candidates/<int:employee_id>/detail",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_candidate_detail(self, assessment_id, employee_id, **kwargs):
        """Per-candidate deep view: their responses, question by question.

        `employee_id` is the `hr.employee` PK of the candidate. The
        endpoint resolves the candidate's `etp.assessment.evaluator`
        assignment within the given assessment, then returns its responses
        in JSON-RPC envelope.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        Evaluator = env["etp.assessment.evaluator"].sudo()
        assignment = Evaluator.search(
            [
                ("assessment_id", "=", assessment_id),
                ("employee_id", "=", employee_id),
            ],
            limit=1,
        )
        if not assignment:
            return return_Response(
                message="Candidate not found in this assessment", status=404,
            )

        Response = env["etp.assessment.response"].sudo()
        responses = Response.search(
            [("assessment_evaluator_id", "=", assignment.id)],
            order="id desc",
        )

        response_ids_payload = []
        for r in responses:
            q = r.question_id
            response_ids_payload.append({
                "id": r.id,
                "question_id": (
                    {"id": q.id, "display_name": q.display_name or q.name or ""}
                    if q else False
                ),
                "score": r.score or 0,
                "state": r.state,
            })

        assessment = assignment.assessment_id
        employee = assignment.employee_id

        def _dt(value):
            return value.strftime("%Y-%m-%d %H:%M:%S") if value else False

        result_row = {
            "id": assignment.id,
            "state": assignment.state,
            "violation_reason": assignment.violation_reason or False,
            "violation_datetime": _dt(assignment.violation_datetime),
            "assessment_id": (
                {"id": assessment.id, "display_name": assessment.display_name or assessment.name or ""}
                if assessment else False
            ),
            "employee_id": (
                {"id": employee.id, "display_name": employee.display_name or employee.name or ""}
                if employee else False
            ),
            "total_questions": assignment.total_questions or 0,
            "answered_count": assignment.answered_count or 0,
            "total_score": assignment.total_score or 0,
            "max_possible_score": assignment.max_possible_score or 0,
            "is_locked": bool(assignment.is_locked),
            "is_violated": bool(assignment.is_violated),
            "response_ids": response_ids_payload,
            "started_at": _dt(assignment.started_at),
            "deadline_datetime": _dt(assignment.deadline_datetime),
        }

        rpc_id = kwargs.get("id")
        try:
            rpc_id = int(rpc_id) if rpc_id is not None else None
        except (TypeError, ValueError):
            pass

        payload = {"jsonrpc": "2.0", "id": rpc_id, "result": [result_row]}
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json")],
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
                with env.cr.savepoint():
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
