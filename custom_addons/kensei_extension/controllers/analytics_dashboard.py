import calendar
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)


COMPLETED_STATES = ("Submitted",)
APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS

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
    return round((part / whole) * 100, 2)


def _diff_pct(current, previous):
    if not previous:
        return 100.0 if current else 0.0
    return round(((current - previous) / previous) * 100, 2)


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            ids.append(record.id)
    return ids


def _user_role_tag(env):
    role = env.user.user_role
    if not role:
        return None
    if role.id in _get_role_ids(env, FULL_ACCESS_ROLE_XMLIDS):
        return "full"
    if role.id in _get_role_ids(env, PL_ROLE_XMLIDS):
        return "pl"
    if role.id in _get_role_ids(env, QR_ROLE_XMLIDS):
        return "qr"
    if role.id in _get_role_ids(env, TASKER_ROLE_XMLIDS):
        return "tasker"
    return None


def _scope(env):
    tag = _user_role_tag(env)
    if tag == "full":
        projects = env["project.project"].sudo().search([])
        return tag, [], projects
    user = env.user
    employee = env["hr.employee"].sudo().search([("user_id", "=", user.id)], limit=1)
    if tag in ("pl", "qr"):
        field = "project_lead" if tag == "pl" else "project_qc_reviewer"
        projects = env["project.project"].sudo().search([(field, "in", employee.ids)])
        tasker_user_ids = projects.mapped("project_tasker").mapped("user_id").ids
        user_ids = list(set(tasker_user_ids) | {user.id})
        return tag, [("user_id", "in", user_ids)], projects
    if tag == "tasker":
        projects = env["project.project"].sudo().search(
            [("project_tasker", "in", employee.ids)]
        )
        return tag, [("user_id", "=", user.id)], projects
    return None, [("id", "=", 0)], env["project.project"]


def _parse_date(raw, label):
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except (TypeError, ValueError):
        return None, return_Response(
            message="Invalid %s. Expected YYYY-MM-DD." % label,
            status=400,
            errors=["invalid_%s" % label],
        )


def _create_date_domain(start_date, end_date):
    domain = []
    if start_date:
        domain.append(
            (
                "create_date",
                ">=",
                fields.Datetime.to_string(
                    datetime.combine(start_date, datetime.min.time())
                ),
            )
        )
    if end_date:
        next_day = end_date + timedelta(days=1)
        domain.append(
            (
                "create_date",
                "<",
                fields.Datetime.to_string(
                    datetime.combine(next_day, datetime.min.time())
                ),
            )
        )
    return domain


def _resolve_dashboard_filters(params):
    start, err = _parse_date(params.get("start_date"), "start_date")
    if err:
        return None, err
    end, err = _parse_date(params.get("end_date"), "end_date")
    if err:
        return None, err
    month_raw = params.get("month")
    month_start = None
    month_end = None
    if month_raw:
        try:
            month_start = datetime.strptime(month_raw, "%Y-%m").date().replace(day=1)
            last_day = calendar.monthrange(month_start.year, month_start.month)[1]
            month_end = month_start.replace(day=last_day)
        except (TypeError, ValueError):
            return None, return_Response(
                message="Invalid month. Expected YYYY-MM.",
                status=400,
                errors=["invalid_month"],
            )
    return {
        "start": start,
        "end": end,
        "month_start": month_start,
        "month_end": month_end,
    }, None


def _period_windows(start, end):
    today = fields.Date.context_today(request.env.user)
    cur_end = end or today
    cur_start = start or (cur_end - timedelta(days=6))
    span = (cur_end - cur_start).days + 1
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return cur_start, cur_end, prev_start, prev_end


def _completed_day_counts(env, scope_domain, win_start, win_end):
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
    for rec in records:
        ts = rec.get("batch_completed_at")
        if not ts:
            continue
        day = ts.date() if hasattr(ts, "date") else ts
        counts[day] = counts.get(day, 0) + 1
    return counts


def _intensity(count, max_count):
    if not max_count or not count:
        return 0
    ratio = count / max_count
    bucket = int(ratio * HEATMAP_INTENSITY_LEVELS) + (
        1 if count and ratio > 0 and ratio <= 1.0 else 0
    )
    if bucket > HEATMAP_INTENSITY_LEVELS:
        bucket = HEATMAP_INTENSITY_LEVELS
    if bucket < 1 and count:
        bucket = 1
    return bucket


def _heatmap_window(filters):
    if filters.get("month_start") and filters.get("month_end"):
        return filters["month_start"], filters["month_end"]
    today = fields.Date.context_today(request.env.user)
    first = today.replace(day=1)
    last = first.replace(
        day=calendar.monthrange(first.year, first.month)[1]
    )
    return first, last


def _timeline_window(filters):
    if filters.get("start") and filters.get("end"):
        return filters["start"], filters["end"]
    today = fields.Date.context_today(request.env.user)
    return today - timedelta(days=TIMELINE_WINDOW_DAYS - 1), today


def _build_total_task(env, scope_domain, filters):
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters.get("start"), filters.get("end")
    )
    cur_domain = list(scope_domain) + _create_date_domain(cur_start, cur_end)
    prev_domain = list(scope_domain) + _create_date_domain(prev_start, prev_end)
    current = env["kensei2.kensei2"].sudo().search_count(cur_domain)
    previous = env["kensei2.kensei2"].sudo().search_count(prev_domain)
    return {
        "total_task_count": current,
        "current_period_count": current,
        "previous_period_count": previous,
        "difference_percentage": _diff_pct(current, previous),
        "current_period": {
            "start": cur_start.isoformat(),
            "end": cur_end.isoformat(),
        },
        "previous_period": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        },
    }


def _build_not_submitted_task(env, scope_domain, filters):
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters.get("start"), filters.get("end")
    )
    base = list(scope_domain) + [("task_status", "=", "NotSubmitted")]
    cur_domain = base + _create_date_domain(cur_start, cur_end)
    prev_domain = base + _create_date_domain(prev_start, prev_end)
    current = env["kensei2.kensei2"].sudo().search_count(cur_domain)
    previous = env["kensei2.kensei2"].sudo().search_count(prev_domain)
    return {
        "current_period_count": current,
        "previous_period_count": previous,
        "diff_percentage": _diff_pct(current, previous),
    }


def _build_team_members(env, projects):
    members = (
        projects.mapped("project_lead")
        | projects.mapped("project_qc_reviewer")
        | projects.mapped("project_tasker")
        | projects.mapped("project_aire")
        | projects.mapped("project_swe")
    )
    return {
        "total_team_size": len(members),
        "members": [
            {
                "id": emp.id,
                "name": emp.name or "",
                "email": emp.work_email or "",
            }
            for emp in members
        ],
    }


def _build_heatmap(env, scope_domain, filters):
    win_start, win_end = _heatmap_window(filters)
    counts = _completed_day_counts(env, scope_domain, win_start, win_end)
    max_count = max(counts.values()) if counts else 0
    total_completed = sum(counts.values())
    days = []
    cursor = win_start
    while cursor <= win_end:
        c = counts.get(cursor, 0)
        days.append(
            {
                "date": cursor.isoformat(),
                "weekday": cursor.weekday(),
                "weekday_label": WEEKDAY_LABELS[cursor.weekday()],
                "count": c,
                "intensity": _intensity(c, max_count),
            }
        )
        cursor += timedelta(days=1)
    return {
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "max_count": max_count,
        "total_completed": total_completed,
        "days": days,
    }


def _build_qc_verdict(env, scope_domain, filters):
    selection = env["kensei2.kensei2"]._fields["qc_status"].selection or []
    domain = list(scope_domain) + _create_date_domain(
        filters.get("start"), filters.get("end")
    )
    total = env["kensei2.kensei2"].sudo().search_count(domain)
    breakdown = []
    for value, label in selection:
        scoped = domain + [("qc_status", "=", value)]
        count = env["kensei2.kensei2"].sudo().search_count(scoped)
        breakdown.append(
            {
                "verdict_key": value,
                "verdict_label": label,
                "count": count,
                "percentage": _pct(count, total),
            }
        )
    return {"total": total, "distribution": breakdown}


def _build_qc_leaderboard(env, projects, filters):
    today = fields.Date.context_today(request.env.user)
    win_start = filters.get("start") or today - timedelta(days=LEADERBOARD_WINDOW_DAYS - 1)
    win_end = filters.get("end") or today
    rows = []
    for project in projects:
        qcs = project.project_qc_reviewer
        taskers = project.project_tasker
        tasker_user_ids = taskers.mapped("user_id").ids
        if not tasker_user_ids:
            continue
        domain = [
            ("user_id", "in", tasker_user_ids),
            ("create_date", ">=", fields.Datetime.to_string(
                datetime.combine(win_start, datetime.min.time())
            )),
            ("create_date", "<", fields.Datetime.to_string(
                datetime.combine(win_end + timedelta(days=1), datetime.min.time())
            )),
        ]
        total = env["kensei2.kensei2"].sudo().search_count(domain)
        done = env["kensei2.kensei2"].sudo().search_count(
            domain + [("task_status", "in", list(COMPLETED_STATES))]
        )
        approved = env["kensei2.kensei2"].sudo().search_count(
            domain + [("qc_status", "in", list(APPROVED_VERDICTS))]
        )
        decided = env["kensei2.kensei2"].sudo().search_count(
            domain + [("qc_status", "in", list(DECIDED_VERDICTS))]
        )
        for qc in qcs:
            rows.append(
                {
                    "qc_id": qc.id,
                    "qc_name": qc.name or "",
                    "project_id": project.id,
                    "project_name": project.name or "",
                    "total_taskers": len(tasker_user_ids),
                    "tasks_total": total,
                    "tasks_completed": done,
                    "completion_percentage": _pct(done, total),
                    "approval_percentage": _pct(approved, decided),
                }
            )
    rows.sort(key=lambda r: r["completion_percentage"], reverse=True)
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
    return {"window": {"start": win_start.isoformat(), "end": win_end.isoformat()}, "leaderboard": rows}


def _build_timeline(env, scope_domain, filters):
    win_start, win_end = _timeline_window(filters)
    domain_base = list(scope_domain) + [
        (
            "create_date",
            ">=",
            fields.Datetime.to_string(
                datetime.combine(win_start, datetime.min.time())
            ),
        ),
        (
            "create_date",
            "<",
            fields.Datetime.to_string(
                datetime.combine(win_end + timedelta(days=1), datetime.min.time())
            ),
        ),
    ]
    records = env["kensei2.kensei2"].sudo().search_read(
        domain_base, ["create_date", "qc_status", "task_status"]
    )
    by_day = {}
    cursor = win_start
    while cursor <= win_end:
        by_day[cursor] = {"approved": 0, "rework": 0, "pending": 0}
        cursor += timedelta(days=1)
    for rec in records:
        ts = rec.get("create_date")
        if not ts:
            continue
        day = ts.date() if hasattr(ts, "date") else ts
        if day not in by_day:
            continue
        verdict = rec.get("qc_status")
        if verdict in APPROVED_VERDICTS:
            by_day[day]["approved"] += 1
        elif verdict in REWORK_VERDICTS:
            by_day[day]["rework"] += 1
        else:
            by_day[day]["pending"] += 1
    total_completed = sum(
        stats["approved"] + stats["rework"] for stats in by_day.values()
    )
    return {
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "total_completed": total_completed,
        "trend": [
            {
                "label": day.isoformat(),
                "count": stats["approved"] + stats["rework"] + stats["pending"],
            }
            for day, stats in sorted(by_day.items())
        ],
    }


def _build_team_overview_aligned(env, projects):
    pl_employees = projects.mapped("project_lead")
    qr_employees = projects.mapped("project_qc_reviewer")
    tasker_employees = projects.mapped("project_tasker")
    aire_employees = projects.mapped("project_aire")
    swe_employees = projects.mapped("project_swe")
    tpm_employees = pl_employees.mapped("task_forge_tpm_id") if pl_employees and "task_forge_tpm_id" in pl_employees._fields else env["hr.employee"]
    unique = (
        pl_employees | qr_employees | tasker_employees | aire_employees | swe_employees | tpm_employees
    )
    return {
        "total_team_size": len(unique),
        "role_counts": {
            "tpm": len(tpm_employees),
            "pl": len(pl_employees),
            "qr": len(qr_employees),
            "tasker": len(tasker_employees),
            "aire": len(aire_employees),
            "swe": len(swe_employees),
        },
    }


def _build_status_chart_aligned(env, scope_domain, filters):
    selection = env["kensei2.kensei2"]._fields["task_status"].selection or []
    domain = list(scope_domain) + _create_date_domain(
        filters.get("start"), filters.get("end")
    )
    total = env["kensei2.kensei2"].sudo().search_count(domain)
    chart = []
    for value, label in selection:
        count = env["kensei2.kensei2"].sudo().search_count(
            domain + [("task_status", "=", value)]
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


def _cache_key(env, filters):
    return (
        env.cr.dbname,
        env.user.id,
        filters.get("start").isoformat() if filters.get("start") else "",
        filters.get("end").isoformat() if filters.get("end") else "",
        filters.get("month_start").isoformat() if filters.get("month_start") else "",
        filters.get("month_end").isoformat() if filters.get("month_end") else "",
    )


def _cache_get(key):
    entry = _CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at < datetime.utcnow():
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key, payload):
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _CACHE.pop(oldest, None)
    _CACHE[key] = (datetime.utcnow() + timedelta(seconds=CACHE_TTL), payload)


class KenseiAnalyticsDashboardController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def kensei_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Kensei analytics.",
                status=403,
                errors=["forbidden"],
            )
        filters, err = _resolve_dashboard_filters(request.params)
        if err:
            return err
        cache_key = _cache_key(env, filters)
        cached = _cache_get(cache_key)
        if cached is not None:
            return return_Response(message="Success", status=200, data=cached)
        _tag, scope_domain, projects = _scope(env)
        dashboard = {
            "team_overview": _build_team_overview_aligned(env, projects),
            "status_chart": _build_status_chart_aligned(env, scope_domain, filters),
            "completion_heatmap": _build_heatmap(env, scope_domain, filters),
            "qc_verdict_distribution": _build_qc_verdict(env, scope_domain, filters),
            "qc_leaderboard": _build_qc_leaderboard(env, projects, filters),
            "completion_timeline": _build_timeline(env, scope_domain, filters),
            "task_overview": _build_total_task(env, scope_domain, filters),
            "not_submitted_task_analytics": _build_not_submitted_task(
                env, scope_domain, filters
            ),
            "team_member_analytics": _build_team_members(env, projects),
        }
        payload = {"dashboard": dashboard}
        _cache_set(cache_key, payload)
        return return_Response(message="Success", status=200, data=payload)
