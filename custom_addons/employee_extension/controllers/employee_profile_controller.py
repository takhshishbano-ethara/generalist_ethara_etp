import logging

from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

PROJECT_ROLE_FIELDS = (
    "project_lead",
    "project_qc_reviewer",
    "project_tasker",
    "project_aire",
    "project_swe",
)

PERFORMANCE_COUNT_KEYS = (
    "total_task_count",
    "task_done",
    "approved_count",
    "rework_count",
    "qc_reviewed_count",
    "aht_measured_count",
)


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _joining_date(employee):
    if "joining_date" in employee._fields and employee.joining_date:
        return employee.joining_date
    if "version_id" in employee._fields and employee.version_id:
        starts = [d for d in employee.version_id.mapped("date_start") if d]
        if starts:
            return min(starts)
    return employee.create_date.date() if employee.create_date else False


class EmployeeProfileController(http.Controller):

    @http.route(
        "/api/v1/employees/complete_info",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    @validate_token
    def get_employee_complete_info(self, **kwargs):
        """Return one employee's basic info, cross-project performance, leave summary, running projects and history in a single response."""
        try:
            env = request.env
            Employee = env["hr.employee"].sudo()
            raw_id = (request.params or {}).get("employee_id")
            if raw_id:
                try:
                    employee_id = int(raw_id)
                except (TypeError, ValueError):
                    return return_Response(
                        message="Invalid employee_id; expected an integer.",
                        status=400,
                    )
            else:
                employee_id = env.user.employee_id.id

            if not employee_id:
                return return_Response(
                    message="No employee is linked to the current user.",
                    status=404,
                )

            employee = Employee.browse(employee_id)
            if not employee.exists():
                return return_Response(message="Employee not found", status=404)

            projects = self._employee_projects(employee)
            data = {
                "basic_information": self._build_basic_info(employee),
                "performance_overview": self._build_performance(employee, projects),
                "leave_summary": self._build_leave_summary(employee),
                "current_running_projects": self._build_running_projects(
                    employee, projects
                ),
                "project_history": self._build_project_history(employee),
            }
            return return_Response(message="OK", status=200, data={"data": data})
        except Exception as exc:
            _logger.exception("Employee complete_info failed: %s", exc)
            return return_Response(message=str(exc), status=400)

    def _employee_projects(self, employee):
        Project = request.env["project.project"].sudo()
        domain = ["|"] * (len(PROJECT_ROLE_FIELDS) - 1)
        for field_name in PROJECT_ROLE_FIELDS:
            domain.append((field_name, "in", employee.ids))
        return Project.search(domain)

    def _build_basic_info(self, employee):
        joining = _joining_date(employee)
        return {
            "emp_id": employee.id,
            "employee_name": employee.name or "",
            "joining_date": joining.isoformat() if joining else None,
            "pl_name": employee.task_forge_pl_id.name or "",
            "qr_name": employee.task_forge_qr_id.name or "",
            "user_role": employee.user_id.user_role.name or "",
        }

    def _build_performance(self, employee, projects):
        """Aggregate get_performance_metrics() across distinct connected models.

        connected_table model names are de-duplicated (the method is
        model-wide, so per-project calls would multiply-count); percentages and
        a measure-count-weighted handling time are recomputed from summed totals.
        """
        env = request.env
        aggregated = {key: 0 for key in PERFORMANCE_COUNT_KEYS}
        weighted_seconds = 0.0
        evaluated = []
        skipped = []

        model_names = set()
        if "connected_table" in env["project.project"]._fields:
            for project in projects:
                table = (project.connected_table or "").strip()
                if table:
                    model_names.add(table)

        target_user = employee.user_id
        if target_user:
            for model_name in sorted(model_names):
                if model_name not in env:
                    skipped.append(model_name)
                    continue
                model = env[model_name].with_user(target_user)
                if not hasattr(model, "get_performance_metrics"):
                    skipped.append(model_name)
                    continue
                try:
                    metrics = model.get_performance_metrics()
                except Exception as exc:
                    _logger.warning(
                        "get_performance_metrics failed for %s: %s", model_name, exc
                    )
                    skipped.append(model_name)
                    continue
                for key in PERFORMANCE_COUNT_KEYS:
                    aggregated[key] += metrics.get(key) or 0
                weighted_seconds += (
                    metrics.get("avg_handling_time_seconds") or 0.0
                ) * (metrics.get("aht_measured_count") or 0)
                evaluated.append(model_name)
        else:
            skipped = sorted(model_names)

        measured = aggregated["aht_measured_count"]
        avg_seconds = round(weighted_seconds / measured, 2) if measured else 0.0
        overview = dict(aggregated)
        overview.update(
            {
                "approval_percentage": _pct(
                    aggregated["approved_count"], aggregated["qc_reviewed_count"]
                ),
                "rework_percentage": _pct(
                    aggregated["rework_count"], aggregated["qc_reviewed_count"]
                ),
                "avg_handling_time_seconds": avg_seconds,
                "avg_handling_time_minutes": round(avg_seconds / 60.0, 2),
                "projects_assigned": len(projects),
                "models_evaluated": evaluated,
                "models_skipped": skipped,
            }
        )
        return overview

    def _build_leave_summary(self, employee):
        env = request.env
        Allocation = env["hr.leave.allocation"].sudo()
        Leave = env["hr.leave"].sudo()
        summary = {}

        alloc_groups = Allocation._read_group(
            [("employee_id", "=", employee.id), ("state", "=", "validate")],
            ["holiday_status_id"],
            ["number_of_days:sum"],
        )
        for leave_type, allocated in alloc_groups:
            if not leave_type:
                continue
            summary[leave_type.id] = {
                "leave_type_id": leave_type.id,
                "leave_type_name": leave_type.name or "",
                "allocated_leaves": round(allocated or 0.0, 2),
                "used_leaves": 0.0,
                "remaining_leaves": 0.0,
            }

        leave_groups = Leave._read_group(
            [("employee_id", "=", employee.id), ("state", "=", "validate")],
            ["holiday_status_id"],
            ["number_of_days:sum"],
        )
        for leave_type, used in leave_groups:
            if not leave_type:
                continue
            entry = summary.get(leave_type.id)
            if not entry:
                entry = {
                    "leave_type_id": leave_type.id,
                    "leave_type_name": leave_type.name or "",
                    "allocated_leaves": 0.0,
                    "used_leaves": 0.0,
                    "remaining_leaves": 0.0,
                }
                summary[leave_type.id] = entry
            entry["used_leaves"] = round(used or 0.0, 2)

        for entry in summary.values():
            entry["remaining_leaves"] = round(
                entry["allocated_leaves"] - entry["used_leaves"], 2
            )
        return list(summary.values())

    def _build_running_projects(self, employee, projects):
        running = []
        for project in projects:
            if project.non_stemp_project_status != "production":
                continue
            history = project.member_history_ids.filtered(
                lambda h: h.employee_id.id == employee.id
            )
            active = history.filtered(lambda h: h.state == "active")
            chosen = active[:1] or history[:1]
            running.append(
                {
                    "project_id": project.id,
                    "project_name": project.name or "",
                    "pl_name": ", ".join(project.project_lead.mapped("name")),
                    "qr_name": ", ".join(project.project_qc_reviewer.mapped("name")),
                    "status": project.non_stemp_project_status or "",
                    "allocated_date": chosen.start_date.isoformat()
                    if chosen and chosen.start_date
                    else None,
                }
            )
        return running

    def _build_project_history(self, employee):
        History = request.env["project.member.history"].sudo()
        records = History.search([("employee_id", "=", employee.id)])
        role_labels = dict(History._fields["role"].selection)
        history = []
        for record in records:
            history.append(
                {
                    "history_id": record.id,
                    "project_id": record.project_id.id,
                    "project_name": record.project_id.name or "",
                    "start_date": record.start_date.isoformat()
                    if record.start_date
                    else None,
                    "end_date": record.end_date.isoformat()
                    if record.end_date
                    else None,
                    "role": record.role or "",
                    "activity": role_labels.get(record.role, ""),
                    "state": record.state or "",
                }
            )
        return history
