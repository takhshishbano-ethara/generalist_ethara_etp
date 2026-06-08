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
    raw = params.get("project_id")
    if not raw:
        return None, None
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None, return_Response(
            message="Invalid project_id.",
            status=400,
            errors=["invalid_project_id"],
        )
    project = env["project.project"].sudo().browse(project_id)
    if not project.exists():
        return None, return_Response(
            message="Project not found.",
            status=404,
            errors=["project_not_found"],
        )
    return project, None


def _parse_status(env, params):
    raw = params.get("status")
    if not raw:
        return [], None
    selection = dict(env["kensei2.kensei2"]._fields["task_status"].selection or [])
    values = [v.strip() for v in raw.split(",") if v.strip()]
    invalid = [v for v in values if v not in selection]
    if invalid:
        return [], return_Response(
            message="Invalid status filter.",
            status=400,
            errors=["invalid_status", ",".join(invalid)],
        )
    return values, None


def _parse_role(params):
    raw = params.get("role")
    if not raw:
        return None, None
    if raw not in TEAM_ROLE_KEYS:
        return None, return_Response(
            message="Invalid role filter.",
            status=400,
            errors=["invalid_role"],
        )
    return raw, None


def _submission_window(filters):
    today = fields.Date.context_today(request.env.user)
    end = filters.get("end") or today
    start = filters.get("start") or (end - timedelta(days=SUBMISSION_WINDOW_DAYS - 1))
    return start, end


def _submission_day_counts(env, scope_domain, win_start, win_end):
    domain = list(scope_domain) + [
        ("task_status", "in", list(COMPLETED_STATES)),
        (
            "batch_completed_at",
            ">=",
            fields.Datetime.to_string(
                datetime.combine(win_start, datetime.min.time())
            ),
        ),
        (
            "batch_completed_at",
            "<",
            fields.Datetime.to_string(
                datetime.combine(win_end + timedelta(days=1), datetime.min.time())
            ),
        ),
    ]
    records = env["kensei2.kensei2"].sudo().search_read(
        domain, ["batch_completed_at"]
    )
    counts = {}
    cursor = win_start
    while cursor <= win_end:
        counts[cursor] = 0
        cursor += timedelta(days=1)
    for rec in records:
        ts = rec.get("batch_completed_at")
        if not ts:
            continue
        day = ts.date() if hasattr(ts, "date") else ts
        if day in counts:
            counts[day] += 1
    return counts


def _build_total_task(env, task_domain, filters):
    today = fields.Date.context_today(request.env.user)
    week_start = today - timedelta(days=today.weekday())
    total = env["kensei2.kensei2"].sudo().search_count(task_domain)
    done = env["kensei2.kensei2"].sudo().search_count(
        task_domain + [("task_status", "in", list(COMPLETED_STATES))]
    )
    week_added = env["kensei2.kensei2"].sudo().search_count(
        task_domain + [
            (
                "create_date",
                ">=",
                fields.Datetime.to_string(
                    datetime.combine(week_start, datetime.min.time())
                ),
            )
        ]
    )
    return {
        "total_completed_task_count": done,
        "total_task_count": total,
        "this_week_added_task_count": week_added,
        "week_start": week_start.isoformat(),
    }


def _build_team_analytics(env, projects, role_filter):
    breakdown = []
    seen = env["hr.employee"]
    for key, field_name in TEAM_ROLE_FIELDS:
        if role_filter and role_filter != key:
            continue
        employees = projects.mapped(field_name)
        breakdown.append({"role": key, "count": len(employees)})
        seen = seen | employees
    return {"total_team_size": len(seen), "role_breakdown": breakdown}


def _build_task_progress(env, task_domain, filters):
    selection = env["kensei2.kensei2"]._fields["task_status"].selection or []
    total = env["kensei2.kensei2"].sudo().search_count(task_domain)
    chart = []
    for value, label in selection:
        count = env["kensei2.kensei2"].sudo().search_count(
            task_domain + [("task_status", "=", value)]
        )
        chart.append(
            {
                "status_key": value,
                "status_name": label,
                "count": count,
                "percentage": _pct(count, total),
            }
        )
    return {"total_task_count": total, "status_chart": chart}


def _build_submission_trend(env, base_domain, filters):
    win_start, win_end = _submission_window(filters)
    counts = _submission_day_counts(env, base_domain, win_start, win_end)
    total = sum(counts.values())
    series = [
        {"date": day.isoformat(), "count": cnt}
        for day, cnt in sorted(counts.items())
    ]
    return {
        "period": "daily",
        "total_in_period": total,
        "trend": series,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
    }


def _cache_key(env, filters, project, status_values, role_filter):
    return (
        env.cr.dbname,
        env.user.id,
        project.id if project else 0,
        ",".join(sorted(status_values)),
        role_filter or "",
        filters.get("start").isoformat() if filters.get("start") else "",
        filters.get("end").isoformat() if filters.get("end") else "",
    )


def _cache_get(key):
    entry = _OVERVIEW_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at < datetime.utcnow():
        _OVERVIEW_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key, payload):
    if len(_OVERVIEW_CACHE) >= OVERVIEW_CACHE_MAX_ENTRIES:
        oldest = min(_OVERVIEW_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _OVERVIEW_CACHE.pop(oldest, None)
    _OVERVIEW_CACHE[key] = (
        datetime.utcnow() + timedelta(seconds=OVERVIEW_CACHE_TTL),
        payload,
    )


class KenseiDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_dashboard_overview(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei dashboard.",
                status=403,
                errors=["forbidden"],
            )
        filters, err = _resolve_dashboard_filters(request.params)
        if err:
            return err
        project, err = _resolve_project(env, request.params)
        if err:
            return err
        status_values, err = _parse_status(env, request.params)
        if err:
            return err
        role_filter, err = _parse_role(request.params)
        if err:
            return err
        cache_key = _cache_key(env, filters, project, status_values, role_filter)
        cached = _cache_get(cache_key)
        if cached is not None:
            return return_Response(message="Success", status=200, data=cached)
        _tag, scope_domain, projects = _scope(env)
        if project:
            projects = projects & project
        base_domain = list(scope_domain) + _create_date_domain(
            filters.get("start"), filters.get("end")
        )
        if project:
            project_employee_ids = (
                project.project_tasker.ids
                + project.project_qc_reviewer.ids
                + project.project_lead.ids
            )
            if project_employee_ids:
                base_domain.append(("employee_id", "in", project_employee_ids))
            else:
                base_domain.append(("id", "=", 0))
        task_domain = list(base_domain)
        if status_values:
            task_domain.append(("task_status", "in", status_values))
        kpi = {
            "total_task_count": env["kensei2.kensei2"].sudo().search_count(base_domain),
            "total_task_done": env["kensei2.kensei2"].sudo().search_count(
                base_domain + [("task_status", "in", list(COMPLETED_STATES))]
            ),
            "this_week_added": _build_total_task(env, base_domain, filters)[
                "this_week_added_task_count"
            ],
            "team_overview": _build_team_overview_aligned(env, projects),
        }
        payload = {
            "overview_dashboard": {
                "kpi": kpi,
                "status_chart": _build_task_progress(env, task_domain, filters),
                "submission_trend": _build_submission_trend(env, base_domain, filters),
                "total_task_analytics": _build_total_task(env, base_domain, filters),
                "team_analytics": _build_team_analytics(env, projects, role_filter),
                "task_progress_graph": _build_task_progress(env, task_domain, filters),
                "submission_trend_analytics": _build_submission_trend(
                    env, base_domain, filters
                ),
            }
        }
        _cache_set(cache_key, payload)
        return return_Response(message="Success", status=200, data=payload)
