import logging
from datetime import date, datetime, timedelta

import pytz

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

DELIVERED_STATES = ("done", "submitted")
ON_TIME_VERDICTS = ("shippable", "fixes")
DECIDED_VERDICTS = ("shippable", "fixes", "not_shippable")
DEFAULT_WINDOW_DAYS = 30

DELIVERY_STATUS_BY_PROJECT_STATUS = {
    "production": "in_progress",
    "not_started": "in_progress",
    "draft": "in_progress",
    "closed": "on_time",
    "cancel": "missed",
    "paused": "delayed",
}

PRIVILEGED_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)
PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)
QC_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)


def _resolve_role_ids(env, xmlids):
    ids = set()
    for xid in xmlids:
        role = env.ref(xid, raise_if_not_found=False)
        if role:
            ids.add(role.id)
    return ids


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _parse_date_param(raw):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _format_duration_human(td):
    total_seconds = max(int(td.total_seconds()), 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes}m"


def _joining_date(employee):
    if "joining_date" in employee._fields and employee.joining_date:
        return employee.joining_date
    if "version_id" in employee._fields and employee.version_id:
        starts = [d for d in employee.version_id.mapped("date_start") if d]
        if starts:
            return min(starts)
    return employee.create_date.date() if employee.create_date else False


def _indian_fiscal_year(today):
    if today.month >= 4:
        start = date(today.year, 4, 1)
        end = date(today.year + 1, 3, 31)
    else:
        start = date(today.year - 1, 4, 1)
        end = date(today.year, 3, 31)
    return start, end


def _derive_delivery_status(history_record):
    project_status = history_record.project_id.non_stemp_project_status or ""
    if history_record.state == "active":
        if project_status == "cancel":
            return "missed"
        if project_status == "paused":
            return "delayed"
        return "in_progress"
    return DELIVERY_STATUS_BY_PROJECT_STATUS.get(project_status, "in_progress")


def _project_work_type(project):
    if "task_template_type" in project._fields and project.task_template_type:
        return project.task_template_type.name or ""
    return ""


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
        """Return one employee's basic info, cross-project performance, leave summary, running projects and history in a single response.

        Query params:
        - employee_id (optional): target employee. Defaults to caller's employee.
        - start_date / end_date (optional, ``YYYY-MM-DD``): global date filter
          applied to performance_overview, current_running_projects and
          project_history. Defaults to the last 30 days.

        Project scope (driven by caller's role):
        - CTO caller: all projects.
        - TPM caller: projects whose ``project_lead`` includes any employee
          whose ``task_forge_tpm_id`` is the caller.
        - Other roles: projects where the *target* employee is a member.
        """
        try:
            env = request.env
            Employee = env["hr.employee"].sudo()
            params = request.params or {}
            raw_id = params.get("employee_id")
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

            today = date.today()
            end_dt = _parse_date_param(params.get("end_date")) or today
            start_dt = _parse_date_param(params.get("start_date")) or (
                end_dt - timedelta(days=DEFAULT_WINDOW_DAYS)
            )
            if start_dt > end_dt:
                return return_Response(
                    message="start_date must be on or before end_date.",
                    status=400,
                )
            window = (
                datetime.combine(start_dt, datetime.min.time()),
                datetime.combine(end_dt, datetime.max.time()),
            )

            projects = self._employee_projects(employee)
            fy_start, fy_end = _indian_fiscal_year(today)
            data = {
                "basic_information": self._build_basic_info(employee),
                "performance_overview": self._build_performance(
                    employee, projects, window
                ),
                "leave_summary": self._build_leave_summary(employee),
                "leave_fiscal_year_start": fy_start.isoformat(),
                "leave_fiscal_year_end": fy_end.isoformat(),
                "current_running_projects": self._build_running_projects(
                    employee, projects, window
                ),
                "task_list": self._build_task_list(employee, projects, window),
                "project_history": self._build_project_history(employee, window),
                "filter_window": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat(),
                },
            }
            return return_Response(message="OK", status=200, data={"data": data})
        except Exception as exc:
            _logger.exception("Employee complete_info failed: %s", exc)
            return return_Response(message=str(exc), status=400)

    def _caller_scope_projects(self):
        env = request.env
        Project = env["project.project"].sudo()
        caller = env.user
        caller_role_id = caller.user_role.id if caller.user_role else False

        cto_role = env.ref(
            "api_auth_gateway.role_cto_technical", raise_if_not_found=False
        )
        if cto_role and caller_role_id == cto_role.id:
            return Project.search([])

        caller_emp = caller.employee_id
        if not caller_emp:
            return Project.browse()

        tpm_role = env.ref(
            "api_auth_gateway.role_tpm_technical", raise_if_not_found=False
        )
        if tpm_role and caller_role_id == tpm_role.id:
            pl_emps = env["hr.employee"].sudo().search(
                [("task_forge_tpm_id", "=", caller_emp.id)]
            )
            if not pl_emps:
                return Project.browse()
            return Project.search([("project_lead", "in", pl_emps.ids)])

        pl_ids = _resolve_role_ids(env, PL_ROLE_XMLIDS)
        if caller_role_id in pl_ids:
            return Project.search([("project_lead", "in", caller_emp.ids)])

        qc_ids = _resolve_role_ids(env, QC_ROLE_XMLIDS)
        if caller_role_id in qc_ids:
            return Project.search([("project_qc_reviewer", "in", caller_emp.ids)])

        domain = ["|"] * (len(PROJECT_ROLE_FIELDS) - 1)
        for field_name in PROJECT_ROLE_FIELDS:
            domain.append((field_name, "in", caller_emp.ids))
        return Project.search(domain)

    def _caller_can_view(self, employee):
        env = request.env
        caller = env.user
        caller_role_id = caller.user_role.id if caller.user_role else False
        privileged_ids = _resolve_role_ids(env, PRIVILEGED_ROLE_XMLIDS)
        pl_ids = _resolve_role_ids(env, PL_ROLE_XMLIDS)
        qc_ids = _resolve_role_ids(env, QC_ROLE_XMLIDS)
        if caller_role_id in privileged_ids:
            return True
        if caller_role_id in pl_ids or caller_role_id in qc_ids:
            return True
        return bool(caller.employee_id) and caller.employee_id.id == employee.id

    def _employee_projects(self, employee):
        if not self._caller_can_view(employee):
            return request.env["project.project"].browse()
        scope = self._caller_scope_projects()

        def emp_in_project(project):
            for field_name in PROJECT_ROLE_FIELDS:
                if employee.id in getattr(project, field_name).ids:
                    return True
            return False

        return scope.filtered(emp_in_project)

    def _employee_session(self, employee):
        Attendance = request.env["hr.attendance"].sudo()
        today = date.today()
        att = Attendance.search(
            [
                ("employee_id", "=", employee.id),
                ("check_in", ">=", datetime.combine(today, datetime.min.time())),
                ("check_in", "<", datetime.combine(today, datetime.max.time())),
                ("attendance_status", "=", "present"),
            ],
            limit=1,
            order="check_in desc",
        )
        if not att or not att.check_in:
            return "Offline", "Offline"
        tz_name = request.env.user.tz or "UTC"
        try:
            user_tz = pytz.timezone(tz_name)
        except Exception:
            user_tz = pytz.UTC
        check_in = att.check_in
        if check_in.tzinfo is None:
            check_in = pytz.UTC.localize(check_in)
        check_in_local = check_in.astimezone(user_tz)
        end_time = att.check_out
        if end_time:
            if end_time.tzinfo is None:
                end_time = pytz.UTC.localize(end_time)
            end_time_local = end_time.astimezone(user_tz)
        else:
            end_time_local = datetime.now(user_tz)
        duration = end_time_local - check_in_local
        duration_str = _format_duration_human(duration)
        time_str = check_in_local.strftime("%I:%M %p").lstrip("0")
        return f"Online Punched in {time_str} {duration_str}", "Online"

    def _build_basic_info(self, employee):
        joining = _joining_date(employee)
        session_str, working_status = self._employee_session(employee)
        return {
            "emp_id": employee.id,
            "employee_name": employee.name or "",
            "joining_date": joining.isoformat() if joining else None,
            "pl_name": employee.task_forge_pl_id.name or "",
            "qr_name": employee.task_forge_qr_id.name or "",
            "user_role": employee.user_id.user_role.name or "",
            "user_session": session_str,
            "working_status": working_status,
        }

    def _build_performance(self, employee, projects, window):
        """Aggregate metrics across the employee's connected_table models.

        ``on_time_delivery`` is computed directly against those connected
        tables: of rows the target employee delivered (``state`` in
        ``DELIVERED_STATES`` with ``completed_at`` inside ``window``), the
        percentage accepted at QC (``qc_verdict`` in ``ON_TIME_VERDICTS``);
        if the model has no ``qc_verdict`` field, every delivered row counts.
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
        delivered_total = 0
        on_time_total = 0
        if target_user:
            for model_name in sorted(model_names):
                if model_name not in env:
                    skipped.append(model_name)
                    continue
                model_for_metrics = env[model_name].with_user(target_user)
                if not hasattr(model_for_metrics, "get_performance_metrics"):
                    skipped.append(model_name)
                    continue
                try:
                    metrics = model_for_metrics.get_performance_metrics()
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

                model = env[model_name].sudo()
                model_fields = model._fields
                if all(
                    f in model_fields
                    for f in ("user_id", "state", "completed_at")
                ):
                    delivered = model.search(
                        [
                            ("user_id", "=", target_user.id),
                            ("state", "in", list(DELIVERED_STATES)),
                            ("completed_at", ">=", window[0]),
                            ("completed_at", "<=", window[1]),
                        ]
                    )
                    delivered_total += len(delivered)
                    if "qc_verdict" in model_fields:
                        on_time_total += len(
                            delivered.filtered(
                                lambda r: r.qc_verdict in ON_TIME_VERDICTS
                            )
                        )
                    else:
                        on_time_total += len(delivered)
        else:
            skipped = sorted(model_names)

        measured = aggregated["aht_measured_count"]
        avg_seconds = round(weighted_seconds / measured, 2) if measured else 0.0
        tasker_count = len({tid for p in projects for tid in p.project_tasker.ids})
        qc_reviewers_count = len(
            {rid for p in projects for rid in p.project_qc_reviewer.ids}
        )
        reviews_completed = 0
        if "connected_table" in env["project.project"]._fields:
            qc_projects = projects.filtered(
                lambda p: employee.id in p.project_qc_reviewer.ids
            )
            for project in qc_projects:
                table = (project.connected_table or "").strip()
                if not table or table not in env:
                    continue
                model = env[table].sudo()
                mfields = model._fields
                if all(
                    f in mfields
                    for f in ("project_id", "qc_verdict", "completed_at")
                ):
                    reviews_completed += model.search_count(
                        [
                            ("project_id", "=", project.id),
                            ("qc_verdict", "in", list(DECIDED_VERDICTS)),
                            ("completed_at", ">=", window[0]),
                            ("completed_at", "<=", window[1]),
                        ]
                    )
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
                "tasker_count": tasker_count,
                "qc_reviewers_count": qc_reviewers_count,
                "reviews_completed": reviews_completed,
                "on_time_delivery": _pct(on_time_total, delivered_total),
                "delivered_count": delivered_total,
                "on_time_count": on_time_total,
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

    def _build_running_projects(self, employee, projects, window):
        env = request.env
        running = []
        win_start = window[0].date()
        win_end = window[1].date()
        for project in projects:
            if project.non_stemp_project_status != "production":
                continue
            history = project.member_history_ids.filtered(
                lambda h: h.employee_id.id == employee.id
                and (not h.start_date or h.start_date <= win_end)
                and (not h.end_date or h.end_date >= win_start)
            )
            if not history:
                continue
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
                    "work_type": _project_work_type(project),
                    "taskers_count": len(project.project_tasker),
                    "reviews_count": self._project_reviews_count(env, project, window),
                    "task_stats": self._build_project_task_stats(env, project),
                }
            )
        return running

    def _build_task_list(self, employee, projects, window):
        env = request.env
        if not employee.user_id:
            return []
        if "connected_table" not in env["project.project"]._fields:
            return []

        viewed_role_id = (
            employee.user_id.user_role.id if employee.user_id.user_role else False
        )
        privileged_ids = _resolve_role_ids(env, PRIVILEGED_ROLE_XMLIDS)
        pl_qc_ids = _resolve_role_ids(env, PL_ROLE_XMLIDS) | _resolve_role_ids(
            env, QC_ROLE_XMLIDS
        )
        viewed_is_privileged = viewed_role_id in privileged_ids
        viewed_is_pl_or_qc = viewed_role_id in pl_qc_ids

        tasks = []
        for project in projects:
            table = (project.connected_table or "").strip()
            if not table or table not in env:
                continue
            model = env[table].sudo()
            mfields = model._fields
            if "user_id" not in mfields or "project_id" not in mfields:
                continue
            domain = [("project_id", "=", project.id)]
            if viewed_is_privileged:
                pass
            elif viewed_is_pl_or_qc:
                tasker_user_ids = project.project_tasker.mapped("user_id").ids
                if not tasker_user_ids:
                    continue
                domain.append(("user_id", "in", tasker_user_ids))
            else:
                domain.append(("user_id", "=", employee.user_id.id))
            if "started_at" in mfields:
                domain += [
                    ("started_at", ">=", window[0]),
                    ("started_at", "<=", window[1]),
                ]
            order = "started_at desc" if "started_at" in mfields else "id desc"
            records = model.search(domain, order=order)
            if not records:
                continue
            pl_name = ", ".join(project.project_lead.mapped("name"))
            qc_name = ", ".join(project.project_qc_reviewer.mapped("name"))
            project_name = project.name or ""
            for rec in records:
                tasker_name = ""
                tasker_user = rec.user_id if "user_id" in mfields else False
                if tasker_user:
                    if tasker_user.employee_id:
                        tasker_name = tasker_user.employee_id.name or tasker_user.name or ""
                    else:
                        tasker_name = tasker_user.name or ""
                start_at = rec.started_at if "started_at" in mfields else False
                end_at = rec.completed_at if "completed_at" in mfields else False
                tasks.append(
                    {
                        "task_id": rec.id,
                        "task_name": (rec.name if "name" in mfields else "") or "",
                        "sequence": (rec.site_name if "site_name" in mfields else "") or "",
                        "project_name": project_name,
                        "start_time": start_at.isoformat() if start_at else None,
                        "end_time": end_at.isoformat() if end_at else None,
                        "duration_seconds": (
                            rec.duration_seconds if "duration_seconds" in mfields else 0.0
                        )
                        or 0.0,
                        "status": (rec.state if "state" in mfields else "") or "",
                        "tasker_name": tasker_name,
                        "qc_name": qc_name,
                        "pl_name": pl_name,
                    }
                )
        tasks.sort(key=lambda t: t["start_time"] or "", reverse=True)
        return tasks

    def _build_project_task_stats(self, env, project):
        stats = {
            "total_tasks": 0,
            "completed": 0,
            "in_progress": 0,
            "qc_approved": 0,
            "qc_rework": 0,
            "avg_score": 0.0,
            "avg_duration_seconds": 0.0,
        }
        if "connected_table" not in project._fields:
            return stats
        table = (project.connected_table or "").strip()
        if not table or table not in env:
            return stats
        model = env[table].sudo()
        mfields = model._fields
        if "project_id" not in mfields:
            return stats
        base = [("project_id", "=", project.id)]
        stats["total_tasks"] = model.search_count(base)
        if "state" in mfields:
            stats["completed"] = model.search_count(
                base + [("state", "in", list(DELIVERED_STATES))]
            )
            stats["in_progress"] = model.search_count(
                base
                + [
                    (
                        "state",
                        "not in",
                        list(DELIVERED_STATES)
                        + ["cancel", "cancelled", "error", "failed"],
                    )
                ]
            )
        if "qc_verdict" in mfields:
            stats["qc_approved"] = model.search_count(
                base + [("qc_verdict", "=", "shippable")]
            )
            stats["qc_rework"] = model.search_count(
                base + [("qc_verdict", "=", "fixes")]
            )
        if "score" in mfields:
            groups = model._read_group(base, [], ["score:avg"])
            if groups and groups[0] and groups[0][0] is not None:
                stats["avg_score"] = round(groups[0][0], 2)
        if "duration_seconds" in mfields:
            groups = model._read_group(base, [], ["duration_seconds:avg"])
            if groups and groups[0] and groups[0][0] is not None:
                stats["avg_duration_seconds"] = round(groups[0][0], 2)
        return stats

    def _project_reviews_count(self, env, project, window):
        if "connected_table" not in project._fields:
            return 0
        table = (project.connected_table or "").strip()
        if not table or table not in env:
            return 0
        model = env[table].sudo()
        mfields = model._fields
        if not all(
            f in mfields for f in ("project_id", "qc_verdict", "completed_at")
        ):
            return 0
        return model.search_count(
            [
                ("project_id", "=", project.id),
                ("qc_verdict", "in", list(DECIDED_VERDICTS)),
                ("completed_at", ">=", window[0]),
                ("completed_at", "<=", window[1]),
            ]
        )

    def _build_project_history(self, employee, window):
        if not self._caller_can_view(employee):
            return []
        scope_project_ids = self._caller_scope_projects().ids
        if not scope_project_ids:
            return []
        History = request.env["project.member.history"].sudo()
        win_start = window[0].date()
        win_end = window[1].date()
        domain = [
            ("employee_id", "=", employee.id),
            ("project_id", "in", scope_project_ids),
            ("start_date", "<=", win_end),
            "|",
            ("end_date", "=", False),
            ("end_date", ">=", win_start),
        ]
        records = History.search(domain)
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
                    "work_type": _project_work_type(record.project_id),
                    "delivery_status": _derive_delivery_status(record),
                }
            )
        return history
