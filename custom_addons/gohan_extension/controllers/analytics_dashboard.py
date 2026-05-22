import calendar
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

COMPLETED_STATES = ("done", "submitted")
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEATMAP_INTENSITY_LEVELS = 4
LEADERBOARD_WINDOW_DAYS = 30
TIMELINE_WINDOW_DAYS = 30

CACHE_TTL = 60
CACHE_MAX_ENTRIES = 256
_CACHE = {}

FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)

PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)

QR_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

TASKER_ROLE_XMLIDS = (
    "api_auth_gateway.role_tasker_technical",
    "api_auth_gateway.role_tasker_stem",
    "api_auth_gateway.role_tasker_non_stem",
)


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _diff_pct(current, previous):
    if not previous:
        return 100.0 if current else 0.0
    return round(((current - previous) / previous) * 100.0, 2)


def _fmt_duration(seconds):
    total = int(round(seconds or 0))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _user_role_tag(env):
    role = env.user.user_role
    if not role:
        return None
    role_id = role.id
    if role_id in _get_role_ids(env, FULL_ACCESS_ROLE_XMLIDS):
        return "full"
    if role_id in _get_role_ids(env, PL_ROLE_XMLIDS):
        return "pl"
    if role_id in _get_role_ids(env, QR_ROLE_XMLIDS):
        return "qr"
    if role_id in _get_role_ids(env, TASKER_ROLE_XMLIDS):
        return "tasker"
    return None


def _scope(env):
    """Resolve the calling user's role-scoped view of gohan.job.

    Team membership is read from project.project records: a PL owns the
    projects listing them in project_lead, a QC the projects listing them in
    project_qc_reviewer, and the team is each project's project_tasker set.
    CTO/TPM see every job; PL/QC see their projects' taskers' jobs; a tasker
    sees only their own. Returns (tag, job_domain, projects); projects is the
    project.project recordset feeding the team and QC-leaderboard sections.
    """
    tag = _user_role_tag(env)
    user = env.user
    Project = env["project.project"].sudo()
    Employee = env["hr.employee"].sudo()
    if tag == "full":
        return tag, [], Project.search([])
    employee = Employee.search([("user_id", "=", user.id)], limit=1)
    if tag in ("pl", "qr"):
        field = "project_lead" if tag == "pl" else "project_qc_reviewer"
        projects = Project.search([(field, "in", employee.ids)])
        taskers = projects.mapped("project_tasker")
        user_ids = (taskers.mapped("user_id") | user).ids
        return tag, [("user_id", "in", user_ids)], projects
    projects = Project.search([("project_tasker", "in", employee.ids)])
    return "tasker", [("user_id", "=", user.id)], projects


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

    return {
        "start": start,
        "end": end,
        "month_start": month_start,
        "month_end": month_end,
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
    rows = env["gohan.job"].sudo().search_read(
        scope
        + [
            ("state", "in", list(COMPLETED_STATES)),
            ("completed_at", ">=", start_dt),
            ("completed_at", "<", end_dt),
        ],
        ["completed_at"],
    )
    counts = {}
    for row in rows:
        when = row.get("completed_at")
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
        window_start = filters["start"] or (
            window_end - timedelta(days=TIMELINE_WINDOW_DAYS - 1)
        )
        return window_start, window_end
    return today - timedelta(days=TIMELINE_WINDOW_DAYS - 1), today


def _build_total_task(env, scope, filters):
    Job = env["gohan.job"].sudo()
    total = Job.search_count(
        scope + _create_date_domain(filters["start"], filters["end"])
    )
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters["start"], filters["end"]
    )
    current = Job.search_count(scope + _create_date_domain(cur_start, cur_end))
    previous = Job.search_count(scope + _create_date_domain(prev_start, prev_end))
    if current > previous:
        trend = "increase"
    elif current < previous:
        trend = "decrease"
    else:
        trend = "no_change"
    return {
        "total_task_count": total,
        "current_period_count": current,
        "previous_period_count": previous,
        "difference_percentage": _diff_pct(current, previous),
        "trend": trend,
        "current_period": {
            "start": cur_start.isoformat(),
            "end": cur_end.isoformat(),
        },
        "previous_period": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        },
    }


def _build_avg_score(env, scope, filters):
    Job = env["gohan.job"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("score", ">", 0)]
    )
    groups = Job._read_group(
        domain, [], ["__count", "score:sum", "score_max:avg"]
    )
    scored, total_score, avg_max = groups[0] if groups else (0, 0.0, 100.0)
    scored = scored or 0
    total_score = total_score or 0.0
    avg_max = avg_max or 100.0
    average_score = (total_score / scored) if scored else 0.0
    return {
        "tasks_scored": scored,
        "average_score": round(average_score, 2),
        "average_score_percentage": _pct(average_score, avg_max or 100.0),
        "total_score": round(total_score, 2),
    }


def _build_avg_duration(env, scope, filters):
    Job = env["gohan.job"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("duration_seconds", ">", 0)]
    )
    groups = Job._read_group(domain, [], ["__count", "duration_seconds:avg"])
    measured, avg_seconds = groups[0] if groups else (0, 0.0)
    measured = measured or 0
    avg_seconds = avg_seconds or 0.0
    return {
        "tasks_measured": measured,
        "average_duration_seconds": round(avg_seconds, 2),
        "average_duration_minutes": round(avg_seconds / 60.0, 2),
        "average_duration_display": _fmt_duration(avg_seconds),
    }


def _build_failed_task(env, scope, filters):
    Job = env["gohan.job"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    total = Job.search_count(base)
    failed = Job.search_count(base + [("state", "=", "failed")])
    return {
        "failed_task_count": failed,
        "total_task_count": total,
        "failure_percentage": _pct(failed, total),
    }


def _build_team_members(env, projects):
    role_fields = (
        ("team_lead", "project_lead"),
        ("qc_reviewer", "project_qc_reviewer"),
        ("tasker", "project_tasker"),
        ("aire", "project_aire"),
        ("swe", "project_swe"),
    )
    breakdown = []
    member_ids = set()
    for role_key, field_name in role_fields:
        employees = projects.mapped(field_name)
        breakdown.append({"role": role_key, "count": len(employees)})
        member_ids.update(employees.ids)
    return {
        "total_team_members": len(member_ids),
        "role_breakdown": breakdown,
    }


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
            "weekday_label": WEEKDAY_LABELS[cursor.weekday()],
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


def _build_qc_verdict(env, scope, filters):
    Job = env["gohan.job"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Job._read_group(domain, ["qc_verdict"], ["__count"])
    counts = {}
    no_verdict = 0
    for key, count in groups:
        count = count or 0
        if key:
            counts[key] = count
        else:
            no_verdict += count
    total = sum(counts.values()) + no_verdict
    distribution = [
        {
            "verdict_key": key,
            "verdict_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Job._fields["qc_verdict"].selection
    ]
    return {
        "total_task_count": total,
        "no_verdict_count": no_verdict,
        "distribution": distribution,
    }


def _build_qc_leaderboard(env, projects, filters):
    Job = env["gohan.job"].sudo()
    today = fields.Datetime.now().date()
    window_end = filters["end"] or today
    window_start = filters["start"] or (
        window_end - timedelta(days=LEADERBOARD_WINDOW_DAYS - 1)
    )
    date_domain = _create_date_domain(window_start, window_end)

    taskers_by_qc = {}
    qc_names = {}
    for project in projects:
        tasker_ids = set(project.project_tasker.ids)
        for qc in project.project_qc_reviewer:
            qc_names[qc.id] = qc.name or ""
            taskers_by_qc.setdefault(qc.id, set()).update(tasker_ids)

    all_tasker_ids = set()
    for tasker_ids in taskers_by_qc.values():
        all_tasker_ids.update(tasker_ids)

    emp_to_user = {
        emp.id: emp.user_id.id
        for emp in env["hr.employee"].sudo().browse(list(all_tasker_ids))
        if emp.user_id
    }
    user_ids = list(set(emp_to_user.values()))

    total_by_user = {}
    completed_by_user = {}
    if user_ids:
        base_domain = [("user_id", "in", user_ids)] + date_domain
        for user, count in Job._read_group(
            base_domain, ["user_id"], ["__count"]
        ):
            if user:
                total_by_user[user.id] = count
        for user, count in Job._read_group(
            base_domain + [("state", "in", list(COMPLETED_STATES))],
            ["user_id"],
            ["__count"],
        ):
            if user:
                completed_by_user[user.id] = count

    leaderboard = []
    for qc_id, tasker_ids in taskers_by_qc.items():
        tasks_total = 0
        tasks_completed = 0
        for emp_id in tasker_ids:
            uid = emp_to_user.get(emp_id)
            if not uid:
                continue
            tasks_total += total_by_user.get(uid, 0)
            tasks_completed += completed_by_user.get(uid, 0)
        leaderboard.append({
            "qc_id": qc_id,
            "qc_name": qc_names.get(qc_id, ""),
            "total_taskers": len(tasker_ids),
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


def _cache_key(env, filters):
    return (
        env.cr.dbname,
        env.user.id,
        filters["start"].isoformat() if filters["start"] else "",
        filters["end"].isoformat() if filters["end"] else "",
        filters["month_start"].isoformat() if filters["month_start"] else "",
    )


def _cache_get(key):
    entry = _CACHE.get(key)
    if not entry:
        return None
    cached_at, payload = entry
    if (datetime.now() - cached_at).total_seconds() > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key, payload):
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        _CACHE.clear()
    _CACHE[key] = (datetime.now(), payload)


class GohanAnalyticsDashboardController(http.Controller):

    @http.route(
        "/api/v1/gohan_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_analytics_dashboard(self, **kwargs):
        """Single role-scoped analytics dashboard endpoint for gohan.job."""
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Gohan analytics.",
                status=403,
            )

        params = request.params or {}
        filters, error = _resolve_dashboard_filters(params)
        if error is not None:
            return error

        cache_key = _cache_key(env, filters)
        cached = _cache_get(cache_key)
        if cached is not None:
            return return_Response(message="OK", status=200, data=cached)

        tag, scope, projects = _scope(env)
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
                #     "data_scope": tag,
                # },
                "total_task_analytics": _build_total_task(env, scope, filters),
                "average_score_analytics": _build_avg_score(env, scope, filters),
                "average_duration_analytics": _build_avg_duration(
                    env, scope, filters
                ),
                "failed_task_analytics": _build_failed_task(env, scope, filters),
                "team_member_analytics": _build_team_members(env, projects),
                "completion_heatmap": _build_heatmap(env, scope, filters),
                "qc_verdict_distribution": _build_qc_verdict(env, scope, filters),
                "qc_team_leaderboard": _build_qc_leaderboard(
                    env, projects, filters
                ),
                "completion_timeline": _build_timeline(env, scope, filters),
            },
        }
        _cache_set(cache_key, data)
        return return_Response(message="OK", status=200, data=data)
