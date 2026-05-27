from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    COMPLETED_STATES,
    _build_team_overview_aligned,
    _create_date_domain,
    _pct,
    _resolve_dashboard_filters,
    _scope,
    _user_role_tag,
)

SUBMISSION_WINDOW_DAYS = 30
TEAM_ROLE_KEYS = ("team_lead", "qc_reviewer", "tasker", "aire", "swe")
TEAM_ROLE_FIELDS = (
    ("team_lead", "project_lead"),
    ("qc_reviewer", "project_qc_reviewer"),
    ("tasker", "project_tasker"),
    ("aire", "project_aire"),
    ("swe", "project_swe"),
)

OVERVIEW_CACHE_TTL = 60
OVERVIEW_CACHE_MAX_ENTRIES = 256
_OVERVIEW_CACHE = {}


def _resolve_project(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw:
        return None, None
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None, return_Response(
            message=f"Invalid project_id '{raw}'. Expected an integer.",
            status=400,
        )
    project = env["project.project"].sudo().browse(project_id)
    if not project.exists():
        return None, return_Response(
            message=f"Project {project_id} was not found.",
            status=404,
        )
    return project, None


def _parse_status(env, params):
    raw = (params.get("status") or "").strip()
    if not raw:
        return [], None
    valid = dict(env["vegeta.job"]._fields["state"].selection)
    requested = [value.strip() for value in raw.split(",") if value.strip()]
    invalid = [value for value in requested if value not in valid]
    if invalid:
        return None, return_Response(
            message=f"Invalid status value(s): {', '.join(invalid)}.",
            status=400,
        )
    return requested, None


def _parse_role(params):
    raw = (params.get("role") or "").strip()
    if not raw:
        return [], None
    requested = [value.strip() for value in raw.split(",") if value.strip()]
    invalid = [value for value in requested if value not in TEAM_ROLE_KEYS]
    if invalid:
        return None, return_Response(
            message=(
                f"Invalid role value(s): {', '.join(invalid)}. "
                f"Allowed: {', '.join(TEAM_ROLE_KEYS)}."
            ),
            status=400,
        )
    return requested, None


def _submission_window(filters):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        window_start = filters["start"] or (
            window_end - timedelta(days=SUBMISSION_WINDOW_DAYS - 1)
        )
        return window_start, window_end
    return today - timedelta(days=SUBMISSION_WINDOW_DAYS - 1), today


def _submission_day_counts(env, scope, win_start, win_end):
    start_dt = datetime.combine(win_start, datetime.min.time())
    end_dt = datetime.combine(win_end, datetime.min.time()) + timedelta(days=1)
    rows = env["vegeta.job"].sudo().search_read(
        scope
        + [
            ("state", "=", "submitted"),
            ("completed_at", ">=", start_dt),
            ("completed_at", "<", end_dt),
        ],
        ["completed_at"],
    )
    counts = {}
    for row in rows:
        when = row.get("completed_at")
        if when:
            day = when.date()
            counts[day] = counts.get(day, 0) + 1
    return counts


def _build_total_task(env, task_domain, filters):
    Job = env["vegeta.job"].sudo()
    date_domain = _create_date_domain(filters["start"], filters["end"])
    completed = Job.search_count(
        task_domain + date_domain + [("state", "in", list(COMPLETED_STATES))]
    )
    today = fields.Datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    this_week = Job.search_count(
        task_domain + [("create_date", ">=", week_start_dt)]
    )
    return {
        "total_completed_task_count": completed,
        "this_week_added_task_count": this_week,
        "week_start": week_start.isoformat(),
    }


def _build_url_analytics(env, task_domain, filters):
    Job = env["vegeta.job"].sudo()
    base = task_domain + _create_date_domain(filters["start"], filters["end"])
    total = Job.search_count(base)
    urls_added = Job.search_count(base + [("url", "!=", False)])
    return {
        "total_urls_added_count": urls_added,
        "total_task_count": total,
        "url_added_percentage": _pct(urls_added, total),
    }


def _build_quality_analytics(env, task_domain, filters):
    Job = env["vegeta.job"].sudo()
    domain = (
        task_domain
        + _create_date_domain(filters["start"], filters["end"])
        + [("score", ">", 0)]
    )
    groups = Job._read_group(domain, [], ["__count", "score:sum"])
    scored, total_score = groups[0] if groups else (0, 0.0)
    scored = scored or 0
    total_score = total_score or 0.0
    avg_max = 100.0
    average_score = (total_score / scored) if scored else 0.0
    return {
        "tasks_scored": scored,
        "average_quality_score": round(average_score, 2),
        "average_quality_score_percentage": _pct(average_score, avg_max),
    }


def _build_team_analytics(env, projects, role_filter):
    breakdown = []
    member_ids = set()
    for role_key, field_name in TEAM_ROLE_FIELDS:
        if role_filter and role_key not in role_filter:
            continue
        employees = projects.mapped(field_name)
        breakdown.append({"role": role_key, "count": len(employees)})
        member_ids.update(employees.ids)
    return {
        "total_team_size": len(member_ids),
        "role_breakdown": breakdown,
    }


def _build_task_progress(env, task_domain, filters):
    Job = env["vegeta.job"].sudo()
    domain = task_domain + _create_date_domain(filters["start"], filters["end"])
    groups = Job._read_group(domain, ["state"], ["__count"])
    counts = {}
    for key, count in groups:
        if key:
            counts[key] = count or 0
    total = sum(counts.values())
    status_chart = [
        {
            "status_key": key,
            "status_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Job._fields["state"].selection
    ]
    return {
        "total_task_count": total,
        "status_chart": status_chart,
    }


def _build_submission_trend(env, base_domain, filters):
    window_start, window_end = _submission_window(filters)
    counts = _submission_day_counts(env, base_domain, window_start, window_end)
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
        "total_submitted": sum(counts.values()),
        "trend": trend,
    }


def _overview_cache_key(env, filters, project, statuses, role_filter):
    return (
        env.cr.dbname,
        env.user.id,
        filters["start"].isoformat() if filters["start"] else "",
        filters["end"].isoformat() if filters["end"] else "",
        filters["month_start"].isoformat() if filters["month_start"] else "",
        project.id if project else 0,
        tuple(sorted(statuses)),
        tuple(sorted(role_filter)),
    )


def _overview_cache_get(key):
    entry = _OVERVIEW_CACHE.get(key)
    if not entry:
        return None
    cached_at, payload = entry
    if (datetime.now() - cached_at).total_seconds() > OVERVIEW_CACHE_TTL:
        _OVERVIEW_CACHE.pop(key, None)
        return None
    return payload


def _overview_cache_set(key, payload):
    if len(_OVERVIEW_CACHE) >= OVERVIEW_CACHE_MAX_ENTRIES:
        _OVERVIEW_CACHE.clear()
    _OVERVIEW_CACHE[key] = (datetime.now(), payload)


class VegetaDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/vegeta_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def vegeta_ext_dashboard_overview(self, **kwargs):
        """Single dashboard overview endpoint for vegeta.job.

        project_id is optional: when given, Team Analytics describes that
        project and the task metrics are narrowed to its taskers. Task,
        URL, quality, progress and submission metrics always stay
        role-scoped (CTO/TPM all, PL/QC their taskers, tasker own).
        """
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Vegeta analytics.",
                status=403,
            )

        params = request.params or {}
        filters, error = _resolve_dashboard_filters(params)
        if error is not None:
            return error
        project, error = _resolve_project(env, params)
        if error is not None:
            return error
        statuses, error = _parse_status(env, params)
        if error is not None:
            return error
        role_filter, error = _parse_role(params)
        if error is not None:
            return error

        cache_key = _overview_cache_key(
            env, filters, project, statuses, role_filter
        )
        cached = _overview_cache_get(cache_key)
        if cached is not None:
            return return_Response(message="OK", status=200, data=cached)

        tag, role_domain, role_projects = _scope(env)
        if project:
            projects = project
            tasker_user_ids = project.project_tasker.mapped("user_id").ids
            base_domain = role_domain + [("user_id", "in", tasker_user_ids)]
        else:
            projects = role_projects
            base_domain = role_domain
        task_domain = base_domain + (
            [("state", "in", statuses)] if statuses else []
        )

        total_task = _build_total_task(env, task_domain, filters)
        url_analytics = _build_url_analytics(env, task_domain, filters)
        quality_analytics = _build_quality_analytics(env, task_domain, filters)
        team_analytics = _build_team_analytics(env, projects, role_filter)
        task_progress = _build_task_progress(env, task_domain, filters)
        submission_trend = _build_submission_trend(env, base_domain, filters)
        kpi = {
            "total_task_count": url_analytics.get("total_task_count", 0),
            "total_task_done": total_task.get("total_completed_task_count", 0),
            "this_week_added": total_task.get("this_week_added_task_count", 0),
            "total_url_tasks": url_analytics.get("total_urls_added_count", 0),
            "avg_quality_score_percentage": quality_analytics.get(
                "average_quality_score_percentage", 0
            ),
            "team_overview": _build_team_overview_aligned(env, projects),
        }
        submission_trend_aligned = {
            "period": "daily",
            "total_in_period": submission_trend.get("total_submitted", 0),
            "trend": submission_trend.get("trend", []),
            "window": submission_trend.get("window", {}),
        }
        data = {
            "overview_dashboard": {
                "kpi": kpi,
                "status_chart": task_progress,
                "submission_trend": submission_trend_aligned,
                "total_task_analytics": total_task,
                "url_analytics": url_analytics,
                "quality_analytics": quality_analytics,
                "team_analytics": team_analytics,
                "task_progress_graph": task_progress,
                "submission_trend_analytics": submission_trend,
            },
        }
        _overview_cache_set(cache_key, data)
        return return_Response(message="OK", status=200, data=data)
