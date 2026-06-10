"""Lightweight `hr.employee` lookup for the assessment candidate picker.

Returns only the fields the frontend needs to render a "Add candidate"
combobox / autocomplete. Optionally hides employees that are already
assigned to a given assessment so the picker shows only addable rows.
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
    require_assessment_user,
    user_role_tag,
)

EMPLOYEE_COLUMNS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "email", "label": "Email", "type": "string"},
    {"key": "job_title", "label": "Job Title", "type": "string"},
    {"key": "department_name", "label": "Department", "type": "string"},
]


def _serialize_employee(emp):
    return {
        "id": emp.id,
        "name": emp.name or "",
        "email": emp.work_email or emp.private_email or "",
        "job_title": emp.job_title or "",
        "department_id": emp.department_id.id if emp.department_id else 0,
        "department_name": emp.department_id.name if emp.department_id else "",
    }


class EtpAssessmentEmployeeController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/employees",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_employees(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}

        domain = []
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|",
                ("name", "ilike", search),
                ("work_email", "ilike", search),
            ]

        exclude_id = coerce_int(params.get("exclude_assessment_id"), 0)
        if exclude_id:
            assessment = (
                env["etp.assessment"].sudo().browse(exclude_id)
            )
            if assessment.exists():
                existing_ids = assessment.evaluator_ids.ids
                if existing_ids:
                    domain.append(("id", "not in", existing_ids))

        page, limit, offset = paginate(params)
        Employee = env["hr.employee"].sudo()
        total = Employee.search_count(domain)
        records = Employee.search(
            domain, limit=limit, offset=offset, order="name asc, id asc",
        )
        rows = [_serialize_employee(e) for e in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Employees",
                    "columns": EMPLOYEE_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )
