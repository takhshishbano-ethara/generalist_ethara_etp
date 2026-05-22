import calendar
import logging
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .main import (
    _job_scope_domain,
    _require_leviathan_user,
)

_logger = logging.getLogger(__name__)

TREND_PERIODS = ("weekly", "monthly", "yearly")
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

COMPLETED_STATES = ("done", "submitted")
DEFAULT_TARGET_AHT_MINUTES = 45.0
HEATMAP_INTENSITY_LEVELS = 4
DASHBOARD_CACHE_TTL = 60
DASHBOARD_CACHE_MAX_ENTRIES = 256
_DASHBOARD_CACHE = {}


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _diff_pct(current, previous):
    if not previous:
        return 100.0 if current else 0.0
    return round(((current - previous) / previous) * 100.0, 2)


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _date_filter_domain(params):
    domain = []
    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )
    if raw_end:
        try:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )
    return domain, None


def _submission_events(env, job_domain):
    """leviathan.job has no submitted_at field; the submission moment is
    recovered from the mail.tracking.value row for the tracked state field."""
    Job = env["leviathan.job"].sudo()
    job_ids = Job.search(job_domain).ids
    if not job_ids:
        return {}

    state_field = env["ir.model.fields"].sudo()._get("leviathan.job", "state")
    if not state_field:
        return {}

    state_selection = dict(env["leviathan.job"]._fields["state"].selection)
    submitted_label = state_selection.get("submitted", "Submitted")

    trackings = env["mail.tracking.value"].sudo().search([
        ("field_id", "=", state_field.id),
        ("new_value_char", "=", submitted_label),
        ("mail_message_id.res_id", "in", job_ids),
    ])

    events = {}
    for tracking in trackings:
        message = tracking.mail_message_id
        when = message.date
        res_id = message.res_id
        if not when or not res_id:
            continue
        if res_id not in events or when > events[res_id]:
            events[res_id] = when
    return events


def _team_overview(env, project_id=None):
    Project = env["project.project"].sudo()
    projects = Project.browse(project_id) if project_id else Project.search([])

    pl_employees = projects.mapped("project_lead")
    qr_employees = projects.mapped("project_qc_reviewer")
    tasker_employees = projects.mapped("project_tasker")
    aire_employees = projects.mapped("project_aire")
    swe_employees = projects.mapped("project_swe")
    tpm_employees = pl_employees.mapped("task_forge_tpm_id")

    members = (
        pl_employees
        | qr_employees
        | tasker_employees
        | aire_employees
        | swe_employees
        | tpm_employees
    )

    return {
        "total_team_size": len(members),
        "role_counts": {
            "tpm": len(tpm_employees),
            "pl": len(pl_employees),
            "qr": len(qr_employees),
            "tasker": len(tasker_employees),
            "aire": len(aire_employees),
            "swe": len(swe_employees),
        },
    }


def _resolve_project_id(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit():
        return None, return_Response(
            message=f"Invalid project_id '{raw}'. Expected an integer.",
            status=400,
        )
    project_id = int(raw)
    if not env["project.project"].sudo().browse(project_id).exists():
        return None, return_Response(
            message=f"Project '{project_id}' not found.",
            status=404,
        )
    return project_id, None


def _resolve_trend_period(params):
    period = (params.get("period") or "weekly").strip().lower()
    if period not in TREND_PERIODS:
        return None, return_Response(
            message=(
                f"Invalid period '{period}'. Allowed: "
                f"{', '.join(TREND_PERIODS)}."
            ),
            status=400,
        )
    return period, None


def _compute_kpi(env, scope, project_id=None):
    Job = env["leviathan.job"].sudo()
    today = fields.Datetime.now().date()
    this_week_start = datetime.combine(_week_start(today), datetime.min.time())

    total_task_count = Job.search_count(scope)
    total_task_done = Job.search_count(scope + [("state", "=", "done")])
    this_week_added = Job.search_count(
        scope + [("create_date", ">=", this_week_start)]
    )
    total_url_tasks = Job.search_count(scope + [("url", "!=", False)])

    scored_rows = Job.read_group(
        scope + [("score", ">", 0)], fields=["score:avg"], groupby=[]
    )
    avg_score = (scored_rows[0].get("score") if scored_rows else 0.0) or 0.0

    return {
        "total_task_count": total_task_count,
        "total_task_done": total_task_done,
        "this_week_added": this_week_added,
        "total_url_tasks": total_url_tasks,
        "avg_quality_score_percentage": round(avg_score, 2),
        "team_overview": _team_overview(env, project_id),
    }


def _compute_status_chart(env, scope):
    Job = env["leviathan.job"].sudo()
    total_task_count = Job.search_count(scope)
    status_chart = []
    for key, label in Job._fields["state"].selection:
        count = Job.search_count(scope + [("state", "=", key)])
        status_chart.append({
            "status_key": key,
            "status_name": label,
            "count": count,
            "percentage": _pct(count, total_task_count),
        })
    return {
        "total_task_count": total_task_count,
        "status_chart": status_chart,
    }


def _compute_submission_trend(env, scope, period):
    events = list(_submission_events(env, scope).values())
    today = fields.Datetime.now().date()

    if period == "weekly":
        start_date = _week_start(today)
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = start_dt + timedelta(days=7)
        buckets = [0] * 7
        for when in events:
            if start_dt <= when < end_dt:
                buckets[(when.date() - start_date).days] += 1
        trend = [
            {"label": WEEKDAY_LABELS[i], "count": buckets[i]}
            for i in range(7)
        ]
    elif period == "monthly":
        year, month = today.year, today.month
        days_in_month = calendar.monthrange(year, month)[1]
        num_weeks = (days_in_month + 6) // 7
        start_dt = datetime(year, month, 1)
        if month == 12:
            end_dt = datetime(year + 1, 1, 1)
        else:
            end_dt = datetime(year, month + 1, 1)
        buckets = [0] * num_weeks
        for when in events:
            if start_dt <= when < end_dt:
                idx = min((when.day - 1) // 7, num_weeks - 1)
                buckets[idx] += 1
        trend = [
            {"label": f"Week {i + 1}", "count": buckets[i]}
            for i in range(num_weeks)
        ]
    else:
        year = today.year
        start_dt = datetime(year, 1, 1)
        end_dt = datetime(year + 1, 1, 1)
        buckets = [0] * 12
        for when in events:
            if start_dt <= when < end_dt:
                buckets[when.month - 1] += 1
        trend = [
            {"label": MONTH_LABELS[i], "count": buckets[i]}
            for i in range(12)
        ]

    return {
        "period": period,
        "total_in_period": sum(item["count"] for item in trend),
        "trend": trend,
    }


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _create_date_domain(start_date, end_date):
    domain = []
    if start_date:
        domain.append(
            ("create_date", ">=", datetime.combine(start_date, datetime.min.time()))
        )
    if end_date:
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end_date, datetime.min.time()) + timedelta(days=1),
            )
        )
    return domain


def _resolve_dashboard_filters(params):
    start = end = month_start = month_end = None

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error

    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )

    raw_month = (params.get("month") or "").strip()
    if raw_month:
        try:
            parsed = datetime.strptime(raw_month, "%Y-%m").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid month '{raw_month}'. Expected YYYY-MM.",
                status=400,
            )
        month_start = parsed.replace(day=1)
        month_end = parsed.replace(
            day=calendar.monthrange(parsed.year, parsed.month)[1]
        )

    target_aht = DEFAULT_TARGET_AHT_MINUTES
    raw_target = (params.get("target_aht_minutes") or "").strip()
    if raw_target:
        try:
            target_aht = float(raw_target)
        except ValueError:
            return None, return_Response(
                message=(
                    f"Invalid target_aht_minutes '{raw_target}'. "
                    "Expected a number."
                ),
                status=400,
            )
        if target_aht <= 0:
            return None, return_Response(
                message="target_aht_minutes must be greater than zero.",
                status=400,
            )

    return {
        "start": start,
        "end": end,
        "month_start": month_start,
        "month_end": month_end,
        "target_aht": target_aht,
    }, None


def _period_windows(start, end):
    today = fields.Datetime.now().date()
    current_end = end or today
    current_start = start or (current_end - timedelta(days=6))
    length = (current_end - current_start).days + 1
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    return current_start, current_end, previous_start, previous_end


def _completed_day_counts(env, scope, win_start, win_end):
    start_dt = datetime.combine(win_start, datetime.min.time())
    end_dt = datetime.combine(win_end, datetime.min.time()) + timedelta(days=1)
    jobs = env["leviathan.job"].sudo().search(
        scope
        + [
            ("state", "in", list(COMPLETED_STATES)),
            ("completed_at", ">=", start_dt),
            ("completed_at", "<", end_dt),
        ]
    )
    counts = {}
    for job in jobs:
        when = job.completed_at
        if not when:
            continue
        day = when.date()
        counts[day] = counts.get(day, 0) + 1
    return counts


def _intensity(count, max_count):
    if not count or not max_count:
        return 0
    level = (count * HEATMAP_INTENSITY_LEVELS + max_count - 1) // max_count
    return min(level, HEATMAP_INTENSITY_LEVELS)


def _heatmap_window(filters):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        window_start = filters["start"] or window_end.replace(day=1)
        return window_start, window_end
    return today.replace(day=1), today


def _timeline_window(filters):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        window_start = filters["start"] or (window_end - timedelta(days=29))
        return window_start, window_end
    return today - timedelta(days=29), today


def _build_task_overview(env, scope, filters):
    Job = env["leviathan.job"].sudo()
    total = Job.search_count(
        scope + _create_date_domain(filters["start"], filters["end"])
    )
    current_start, current_end, previous_start, previous_end = _period_windows(
        filters["start"], filters["end"]
    )
    current = Job.search_count(
        scope + _create_date_domain(current_start, current_end)
    )
    previous = Job.search_count(
        scope + _create_date_domain(previous_start, previous_end)
    )
    return {
        "total_task_count": total,
        "current_period_count": current,
        "previous_period_count": previous,
        "difference_percentage": _diff_pct(current, previous),
        "current_period": {
            "start": current_start.isoformat(),
            "end": current_end.isoformat(),
        },
        "previous_period": {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        },
    }


def _build_aht_overview(env, scope, filters):
    Job = env["leviathan.job"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("duration_seconds", ">", 0)]
    )
    measured = Job.search_count(domain)
    rows = Job.read_group(domain, fields=["duration_seconds:avg"], groupby=[])
    avg_seconds = (rows[0].get("duration_seconds") if rows else 0.0) or 0.0
    avg_minutes = round(avg_seconds / 60.0, 2)
    target = round(filters["target_aht"], 2)
    if not measured:
        indicator = "no_data"
    elif avg_minutes <= target:
        indicator = "on_target"
    else:
        indicator = "above_target"
    return {
        "average_handling_time_minutes": avg_minutes,
        "target_aht_minutes": target,
        "difference_minutes": round(avg_minutes - target, 2),
        "performance_indicator": indicator,
        "tasks_measured": measured,
    }


def _build_url_overview(env, scope, filters):
    Job = env["leviathan.job"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    return {
        "tasks_with_url": Job.search_count(base),
        "rejected_at_validation": Job.search_count(
            base + [("state", "=", "discarded")]
        ),
    }


def _build_status_chart(env, scope, filters):
    Job = env["leviathan.job"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    grouped = Job.read_group(
        domain, fields=["state"], groupby=["state"], lazy=False
    )
    counts = {row["state"]: row["__count"] for row in grouped}
    total = sum(counts.values())
    chart = [
        {
            "status_key": key,
            "status_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Job._fields["state"].selection
    ]
    return {"total_task_count": total, "status_chart": chart}


def _build_heatmap(env, scope, filters):
    window_start, window_end = _heatmap_window(filters)
    counts = _completed_day_counts(env, scope, window_start, window_end)
    max_count = max(counts.values()) if counts else 0
    days = []
    cursor = window_start
    while cursor <= window_end:
        count = counts.get(cursor, 0)
        days.append({
            "date": cursor.isoformat(),
            "weekday": cursor.weekday(),
            "count": count,
            "intensity": _intensity(count, max_count),
        })
        cursor += timedelta(days=1)
    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "max_count": max_count,
        "total_completed": sum(counts.values()),
        "days": days,
    }


def _build_timeline(env, scope, filters):
    window_start, window_end = _timeline_window(filters)
    counts = _completed_day_counts(env, scope, window_start, window_end)
    trend = []
    cursor = window_start
    while cursor <= window_end:
        trend.append({
            "label": cursor.isoformat(),
            "count": counts.get(cursor, 0),
        })
        cursor += timedelta(days=1)
    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "total_completed": sum(counts.values()),
        "trend": trend,
    }


def _build_qc_leaderboard(env, filters):
    Employee = env["hr.employee"].sudo()
    Job = env["leviathan.job"].sudo()

    today = fields.Datetime.now().date()
    window_end = filters["end"] or today
    window_start = filters["start"] or (window_end - timedelta(days=29))
    date_domain = _create_date_domain(window_start, window_end)

    taskers_by_qc = {}
    for employee in Employee.search([("task_forge_active", "=", True)]):
        qc_id = employee.task_forge_qr_id.id
        if qc_id:
            taskers_by_qc.setdefault(qc_id, []).append(employee)

    user_ids = list({
        employee.user_id.id
        for taskers in taskers_by_qc.values()
        for employee in taskers
        if employee.user_id
    })

    total_by_user = {}
    completed_by_user = {}
    if user_ids:
        base_domain = [("user_id", "in", user_ids)] + date_domain
        for row in Job.read_group(
            base_domain, fields=["user_id"], groupby=["user_id"], lazy=False
        ):
            if row["user_id"]:
                total_by_user[row["user_id"][0]] = row["__count"]
        for row in Job.read_group(
            base_domain + [("state", "in", list(COMPLETED_STATES))],
            fields=["user_id"],
            groupby=["user_id"],
            lazy=False,
        ):
            if row["user_id"]:
                completed_by_user[row["user_id"][0]] = row["__count"]

    leaderboard = []
    for qc in Employee.browse(list(taskers_by_qc.keys())):
        taskers = taskers_by_qc.get(qc.id, [])
        tasks_total = 0
        tasks_completed = 0
        for employee in taskers:
            user_id = employee.user_id.id
            if not user_id:
                continue
            tasks_total += total_by_user.get(user_id, 0)
            tasks_completed += completed_by_user.get(user_id, 0)
        leaderboard.append({
            "qc_id": qc.id,
            "qc_name": qc.name or "",
            "total_taskers": len(taskers),
            "tasks_completed": tasks_completed,
            "tasks_total": tasks_total,
            "completion_percentage": _pct(tasks_completed, tasks_total),
        })

    leaderboard.sort(
        key=lambda row: (
            -row["tasks_completed"],
            -row["completion_percentage"],
            row["qc_name"],
        )
    )
    for rank, row in enumerate(leaderboard, start=1):
        row["rank"] = rank

    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "leaderboard": leaderboard,
    }


def _dashboard_cache_key(env, filters, project_id):
    return (
        env.cr.dbname,
        env.user.id,
        filters["start"].isoformat() if filters["start"] else "",
        filters["end"].isoformat() if filters["end"] else "",
        filters["month_start"].isoformat() if filters["month_start"] else "",
        round(filters["target_aht"], 2),
        project_id or 0,
    )


def _dashboard_cache_get(key):
    entry = _DASHBOARD_CACHE.get(key)
    if not entry:
        return None
    cached_at, payload = entry
    if (datetime.now() - cached_at).total_seconds() > DASHBOARD_CACHE_TTL:
        _DASHBOARD_CACHE.pop(key, None)
        return None
    return payload


def _dashboard_cache_set(key, payload):
    if len(_DASHBOARD_CACHE) >= DASHBOARD_CACHE_MAX_ENTRIES:
        _DASHBOARD_CACHE.clear()
    _DASHBOARD_CACHE[key] = (datetime.now(), payload)


class LeviathanAnalyticsController(http.Controller):

    @http.route(
        "/api/v1/leviathan_ext/analytics/kpi",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_analytics_kpi(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}
        date_domain, error = _date_filter_domain(params)
        if error is not None:
            return error

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error
        scope = _job_scope_domain() + date_domain

        return return_Response(
            message="OK",
            status=200,
            data={"kpi": _compute_kpi(env, scope, project_id)},
        )

    @http.route(
        "/api/v1/leviathan_ext/analytics/status_chart",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_analytics_status_chart(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        date_domain, error = _date_filter_domain(request.params or {})
        if error is not None:
            return error
        scope = _job_scope_domain() + date_domain

        return return_Response(
            message="OK",
            status=200,
            data=_compute_status_chart(env, scope),
        )

    @http.route(
        "/api/v1/leviathan_ext/analytics/submission_trend",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_analytics_submission_trend(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}
        period, error = _resolve_trend_period(params)
        if error is not None:
            return error

        date_domain, error = _date_filter_domain(params)
        if error is not None:
            return error
        scope = _job_scope_domain() + date_domain

        return return_Response(
            message="OK",
            status=200,
            data=_compute_submission_trend(env, scope, period),
        )

    @http.route(
        "/api/v1/leviathan_ext/analytics/dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_analytics_dashboard(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}
        filters, error = _resolve_dashboard_filters(params)
        if error is not None:
            return error

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error

        cache_key = _dashboard_cache_key(env, filters, project_id)
        cached = _dashboard_cache_get(cache_key)
        if cached is not None:
            return return_Response(message="OK", status=200, data=cached)

        scope = _job_scope_domain()
        data = {
            "dashboard": {
                # "filters": {
                #     "start_date": (
                #         filters["start"].isoformat() if filters["start"] else None
                #     ),
                #     "end_date": (
                #         filters["end"].isoformat() if filters["end"] else None
                #     ),
                #     "month": params.get("month") or None,
                #     "target_aht_minutes": round(filters["target_aht"], 2),
                # },
                "task_overview": _build_task_overview(env, scope, filters),
                "aht_overview": _build_aht_overview(env, scope, filters),
                "url_overview": _build_url_overview(env, scope, filters),
                "team_overview": _team_overview(env, project_id),
                "status_chart": _build_status_chart(env, scope, filters),
                "completion_heatmap": _build_heatmap(env, scope, filters),
                "qc_leaderboard": _build_qc_leaderboard(env, filters),
                "completion_timeline": _build_timeline(env, scope, filters)
            },
        }
        _dashboard_cache_set(cache_key, data)
        return return_Response(message="OK", status=200, data=data)

    @http.route(
        "/api/v1/leviathan_ext/analytics/overview_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def leviathan_ext_analytics_overview_dashboard(self, **kwargs):
        guard = _require_leviathan_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}
        period, error = _resolve_trend_period(params)
        if error is not None:
            return error

        date_domain, error = _date_filter_domain(params)
        if error is not None:
            return error

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error
        scope = _job_scope_domain() + date_domain

        return return_Response(
            message="OK",
            status=200,
            data={
                "overview_dashboard": {
                    "kpi": _compute_kpi(env, scope, project_id),
                    "status_chart": _compute_status_chart(env, scope),
                    "submission_trend": _compute_submission_trend(
                        env, scope, period
                    ),
                },
            },
        )
