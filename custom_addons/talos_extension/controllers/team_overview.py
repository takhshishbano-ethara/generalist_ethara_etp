from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard_overview import _user_role_tag

DONE_STATUSES = ("Submitted", "completed")
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_attr(record, name, default=None):
    if name in record._fields:
        return getattr(record, name, default)
    return default


def _employee_role(employee):
    user = employee.user_id
    if not user:
        return 0, ""
    role = _safe_attr(user, "user_role")
    if role:
        return role.id or 0, role.name or ""
    if user.has_group("talos.group_talos_admin"):
        return 0, "Talos Admin"
    if user.has_group("etp_user_roles.group_quality_lead"):
        return 0, "QR"
    if user.has_group("etp_user_roles.group_project_lead"):
        return 0, "PL"
    if user.has_group("etp_user_roles.group_tasker"):
        return 0, "Tasker"
    return 0, ""


def _employee_pl(employee):
    for field in ("task_forge_pl_id", "task_forge_project_lead_id", "parent_id"):
        pl = _safe_attr(employee, field)
        if pl:
            return pl.id or 0, pl.name or ""
    return 0, ""


def _employee_qr(employee):
    for field in ("task_forge_qr_id", "task_forge_qc_reviewer_id"):
        qr = _safe_attr(employee, field)
        if qr:
            return qr.id or 0, qr.name or ""
    return 0, ""


def _offboarding_state(employee):
    for field in ("offboarding_state", "task_forge_state", "hr_state"):
        value = _safe_attr(employee, field)
        if value:
            return str(value)
    return "active" if employee.active else "inactive"


def _employee_email(employee):
    user = employee.user_id
    return (
        _safe_attr(user, "email")
        or _safe_attr(employee, "work_email")
        or ""
    )


def _employee_job(employee):
    job = _safe_attr(employee, "job_id")
    if job:
        return job.id or 0, job.name or ""
    title = _safe_attr(employee, "job_title") or ""
    return 0, title


def _employee_department(employee):
    dept = _safe_attr(employee, "department_id")
    if dept:
        return dept.id or 0, dept.name or ""
    return 0, ""


def _employee_since(employee):
    for field in ("hire_date", "first_contract_date", "create_date"):
        value = _safe_attr(employee, field)
        if value:
            return value.date().isoformat() if hasattr(value, "date") else str(value)
    return ""


def _count_done_tasks(env, employee):
    Talos = env["talos.talos"].sudo()
    return Talos.search_count([
        ("employee_id", "=", employee.id),
        ("task_status", "in", list(DONE_STATUSES)),
    ])


def _avg_time(env, employee):
    Talos = env["talos.talos"].sudo()
    records = Talos.search([
        ("employee_id", "=", employee.id),
        ("task_status", "in", list(DONE_STATUSES)),
        ("create_date", "!=", False),
        ("write_date", "!=", False),
    ])
    if not records:
        return 0
    durations = [
        (rec.write_date - rec.create_date).total_seconds()
        for rec in records
        if rec.write_date and rec.create_date
    ]
    if not durations:
        return 0
    return round(sum(durations) / len(durations), 2)


def _employee_extra_counts(env, employee):
    Talos = env["talos.talos"].sudo()
    base = [("employee_id", "=", employee.id)]
    in_progress = Talos.search_count(
        base + [("task_status", "in", ("in_progress", "Submitted"))]
    )
    pending_qc = Talos.search_count(
        base + [("task_status", "=", "completed"), ("qc_status", "=", "pending")]
    )
    golden_pending = Talos.search_count(
        base + [("golden_status", "in", ("idle", "generating", "error"))]
    )
    personas = Talos.search(base).mapped("persona_id.name")
    return {
        "in_progress_count": in_progress,
        "pending_qc_count": pending_qc,
        "golden_pending_count": golden_pending,
        "personas_worked_on": sorted({p for p in personas if p}),
    }


def _serialize_employee(env, employee):
    role_id, role_label = _employee_role(employee)
    pl_id, pl_name = _employee_pl(employee)
    qr_id, qr_name = _employee_qr(employee)
    job_id, job_title = _employee_job(employee)
    dept_id, dept_name = _employee_department(employee)
    extras = _employee_extra_counts(env, employee)
    return {
        "id": employee.id,
        "name": employee.name or "",
        "email": _employee_email(employee),
        "job_title_id": job_id,
        "job_title": job_title,
        "department_id": dept_id,
        "department": dept_name,
        "offboarding_state": _offboarding_state(employee),
        "role_id": role_id,
        "role": role_label,
        "total_done_task": _count_done_tasks(env, employee),
        "pl_id": pl_id,
        "pl_name": pl_name,
        "qr_id": qr_id,
        "qr_name": qr_name,
        "active": employee.active,
        "avg_time": _avg_time(env, employee),
        "since": _employee_since(employee),
        "in_progress_count": extras["in_progress_count"],
        "pending_qc_count": extras["pending_qc_count"],
        "golden_pending_count": extras["golden_pending_count"],
        "personas_worked_on": extras["personas_worked_on"],
    }


def _scoped_employees(env):
    Employee = env["hr.employee"].sudo()
    role_tag = _user_role_tag(env)
    if role_tag == "tasker":
        return Employee.search([("user_id", "=", env.user.id)])
    return Employee.search([])


class TalosTeamOverviewController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/team_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_team_overview(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos team data.",
                status=403,
            )

        params = request.params or {}
        limit = max(1, min(_coerce_int(params.get("limit"), DEFAULT_LIMIT), MAX_LIMIT))
        offset = max(0, _coerce_int(params.get("offset"), 0))
        search = (params.get("search") or "").strip()

        employees = _scoped_employees(env)
        if search:
            search_lower = search.lower()
            employees = employees.filtered(
                lambda e: search_lower in (e.name or "").lower()
                or search_lower in (_employee_email(e) or "").lower()
            )

        total = len(employees)
        page = employees.sorted(lambda e: e.id)[offset:offset + limit]
        data = [_serialize_employee(env, emp) for emp in page]

        return return_Response(
            message=f"{total} employees found",
            status=200,
            data={
                "data": data,
                "total": total,
            },
        )
