import calendar
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

COMPLETED_STATES = ("done",)
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
    rows = env["crowley.generation"].sudo().search_read(
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
    Gen = env["crowley.generation"].sudo()
    total = Gen.search_count(
        scope + _create_date_domain(filters["start"], filters["end"])
    )
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters["start"], filters["end"]
    )
    current = Gen.search_count(scope + _create_date_domain(cur_start, cur_end))
    previous = Gen.search_count(scope + _create_date_domain(prev_start, prev_end))
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


def _build_avg_cost(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("total_cost_usd", ">", 0)]
    )
    groups = Gen._read_group(domain, [], ["__count", "total_cost_usd:sum"])
    counted, total_cost = groups[0] if groups else (0, 0.0)
    counted = counted or 0
    total_cost = total_cost or 0.0
    average_cost = (total_cost / counted) if counted else 0.0
    return {
        "tasks_with_cost": counted,
        "average_cost": round(average_cost, 6),
        "total_cost": round(total_cost, 6),
    }


def _build_avg_duration(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("duration_seconds", ">", 0)]
    )
    groups = Gen._read_group(domain, [], ["__count", "duration_seconds:avg"])
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
    Gen = env["crowley.generation"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    total = Gen.search_count(base)
    failed = Gen.search_count(base + [("state", "=", "failed")])
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


def _build_review_state(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Gen._read_group(domain, ["review_state"], ["__count"])
    counts = {}
    no_state = 0
    for key, count in groups:
        count = count or 0
        if key:
            counts[key] = count
        else:
            no_state += count
    total = sum(counts.values()) + no_state
    distribution = [
        {
            "verdict_key": key,
            "verdict_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Gen._fields["review_state"].selection
    ]
    return {
        "total_task_count": total,
        "no_verdict_count": no_state,
        "distribution": distribution,
    }


def _build_qc_leaderboard(env, projects, filters):
    Gen = env["crowley.generation"].sudo()
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
        for user, count in Gen._read_group(
            base_domain, ["user_id"], ["__count"]
        ):
            if user:
                total_by_user[user.id] = count
        for user, count in Gen._read_group(
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


def _build_aht_overview_aligned(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("duration_seconds", ">", 0)]
    )
    groups = Gen._read_group(domain, [], ["__count", "duration_seconds:avg"])
    measured, avg_seconds = groups[0] if groups else (0, 0.0)
    measured = measured or 0
    avg_seconds = avg_seconds or 0.0
    avg_minutes = round(avg_seconds / 60.0, 2)
    target = round(filters.get("target_aht", 0) or 0, 2)
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


def _build_video_overview_aligned(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    return {
        "tasks_with_video": Gen.search_count(
            base + [("state", "=", "done"), ("video_s3_url", "!=", False)]
        ),
        "rejected_at_validation": Gen.search_count(base + [("state", "=", "failed")]),
    }


def _build_team_overview_aligned(env, projects):
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


def _build_status_chart_aligned(env, scope, filters):
    Gen = env["crowley.generation"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Gen._read_group(domain, ["state"], ["__count"])
    counts = {}
    for key, count in groups:
        if key:
            counts[key] = count or 0
    total = sum(counts.values())
    chart = [
        {
            "status_key": key,
            "status_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Gen._fields["state"].selection
    ]
    return {"total_task_count": total, "status_chart": chart}


DONE_STATE = "done"
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")
PASS_RATE_GOOD = 90.0
UNASSIGNED_QL = 0
UNASSIGNED_QL_LABEL = "Unassigned"


def _money(value):
    return f"${value or 0.0:,.2f}"


def _pct1(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 1)


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _color(index):
    return COLOR_TOKENS[index % len(COLOR_TOKENS)]


def _resolve_range(params):
    today = fields.Datetime.now().date()

    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    if raw_start or raw_end:
        try:
            start = (
                datetime.strptime(raw_start, "%Y-%m-%d").date()
                if raw_start
                else today - timedelta(days=29)
            )
            end = (
                datetime.strptime(raw_end, "%Y-%m-%d").date() if raw_end else today
            )
        except ValueError:
            return None, return_Response(
                message="Invalid start_date/end_date. Expected YYYY-MM-DD.",
                status=400,
            )
        if start > end:
            return None, return_Response(
                message="start_date must be on or before end_date.",
                status=400,
            )
        return {"key": "custom", "start": start, "end": end}, None

    range_key = (params.get("range") or DEFAULT_RANGE).strip().lower()
    if range_key not in RANGE_DAYS:
        return None, return_Response(
            message=f"Invalid range '{range_key}'. Allowed: {', '.join(RANGE_DAYS)}.",
            status=400,
        )
    days = RANGE_DAYS[range_key]
    return {
        "key": range_key,
        "start": today - timedelta(days=days - 1),
        "end": today,
    }, None


def _range_domain(rng):
    start_dt = datetime.combine(rng["start"], datetime.min.time())
    end_dt = datetime.combine(rng["end"], datetime.min.time()) + timedelta(days=1)
    return [("create_date", ">=", start_dt), ("create_date", "<", end_dt)]


def _range_label(rng):
    if rng["key"] == "custom":
        return f"{rng['start'].isoformat()} → {rng['end'].isoformat()}"
    return {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days"}[
        rng["key"]
    ]


def _visible_taskers(env, project):
    Employee = env["hr.employee"].sudo()
    taskers = project.project_tasker.filtered(lambda e: e.task_forge_active)

    if env.user.has_group("base.group_system"):
        return taskers, "admin"

    caller = Employee.search([("user_id", "=", env.user.id)], limit=1)
    if not caller:
        return Employee.browse(), None

    role = caller._get_task_forge_role()
    if role in ("admin", "tpm"):
        return taskers, role
    visible_ids = set(caller._get_team_employee_ids())
    return taskers.filtered(lambda e: e.id in visible_ids), role


def _build_ql_maps(taskers):
    user_ids = []
    ql_of_user = {}
    ql_name = {UNASSIGNED_QL: UNASSIGNED_QL_LABEL}
    ql_taskers = {}
    for emp in taskers:
        uid = emp.user_id.id
        if not uid:
            continue
        ql = emp.task_forge_qr_id
        ql_id = ql.id or UNASSIGNED_QL
        ql_name[ql_id] = ql.name or UNASSIGNED_QL_LABEL
        ql_of_user[uid] = ql_id
        ql_taskers.setdefault(ql_id, set()).add(uid)
        user_ids.append(uid)
    return {
        "user_ids": list(dict.fromkeys(user_ids)),
        "ql_of_user": ql_of_user,
        "ql_name": ql_name,
        "ql_taskers": ql_taskers,
    }


def _resolve_context(env, params):
    raw = (params.get("project_id") or "").strip()
    if not raw or not raw.isdigit():
        return None, return_Response(
            message="project_id is required and must be an integer.",
            status=400,
        )
    project = env["project.project"].sudo().browse(int(raw)).exists()
    if not project:
        return None, return_Response(
            message=f"Project {raw} not found.", status=404
        )

    taskers, role = _visible_taskers(env, project)
    if role is None:
        return None, return_Response(
            message="You are not allowed to access this project's analytics.",
            status=403,
        )

    rng, error = _resolve_range(params)
    if error is not None:
        return None, error

    ctx = {"project": project, "role": role, "rng": rng, "taskers": taskers}
    ctx.update(_build_ql_maps(taskers))
    ctx["scope"] = [("user_id", "in", ctx["user_ids"])] + _range_domain(rng)
    return ctx, None


def _counts_by_user(Gen, scope, extra=None):
    rows = Gen.formatted_read_group(scope + (extra or []), ["user_id"], ["__count"])
    out = {}
    for r in rows:
        user = r["user_id"]
        if user:
            out[user[0]] = r["__count"]
    return out


def _kpi(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _attempt_scope(scope):
    out = []
    for leaf in scope:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
            field, op, value = leaf
            out.append((f"job_id.{field}", op, value))
        else:
            out.append(leaf)
    return out


def _build_kpi_v2(env, ctx):
    Gen = env["crowley.generation"].sudo()
    Attempt = env["crowley.attempt"].sudo()
    scope = ctx["scope"]
    attempt_scope = _attempt_scope(scope)

    gens_count = Gen.search_count(scope)

    spend_rows = Gen.formatted_read_group(scope, [], ["total_cost_usd:sum"])
    total_spend = (spend_rows[0]["total_cost_usd:sum"] if spend_rows else 0.0) or 0.0
    avg_per_task = (total_spend / gens_count) if gens_count else 0.0

    approved = Gen.search_count(scope + [("review_state", "=", "approved")])
    reviewed = approved + Gen.search_count(scope + [("review_state", "=", "rejected")])
    pass_rate = _pct1(approved, reviewed)

    token_rows = Attempt.formatted_read_group(attempt_scope, [], ["tokens_used:avg"])
    avg_tokens = round((token_rows[0]["tokens_used:avg"] if token_rows else 0.0) or 0.0)

    dur_rows = Attempt.formatted_read_group(
        attempt_scope + [("state", "=", DONE_STATE)], [], ["duration_seconds:avg"]
    )
    avg_wall = (dur_rows[0]["duration_seconds:avg"] if dur_rows else 0.0) or 0.0

    items = [
        _kpi(
            "total_spend",
            "Total Spend",
            _money(total_spend),
            sub_string=f"Avg {_money(avg_per_task)}/task · {gens_count} generations",
        ),
        _kpi(
            "qc_pass_rate",
            "QC Pass Rate",
            f"{pass_rate}%",
            sub_string=(
                f"{approved} of {reviewed} reviewed" if reviewed else "No reviews yet"
            ),
            pattern="badge" if reviewed else "",
            sign="+" if pass_rate >= PASS_RATE_GOOD else "-" if reviewed else "",
        ),
        _kpi(
            "avg_tokens_per_task",
            "Avg Tokens / Task",
            avg_tokens,
            sub_string="per generation",
        ),
        _kpi(
            "avg_wall_time",
            "Avg Wall Time",
            f"{round(avg_wall)}s",
            sub_string=f"{round(avg_wall / 60.0, 1)} min per generation",
        ),
    ]
    return {"count": len(items), "items": items}


def _build_spend_by_category(env, ctx):
    Gen = env["crowley.generation"].sudo()
    rows = Gen.formatted_read_group(
        ctx["scope"] + [("category", "!=", False)],
        ["category"],
        ["total_cost_usd:sum"],
    )
    amount_by_cat = {r["category"]: round(r["total_cost_usd:sum"] or 0.0, 4) for r in rows}
    total = round(sum(amount_by_cat.values()), 4)

    selection = list(Gen._fields["category"].selection)
    ordered = sorted(selection, key=lambda kv: (-amount_by_cat.get(kv[0], 0.0), kv[1]))

    items = []
    for idx, (key, label) in enumerate(ordered):
        amount = amount_by_cat.get(key, 0.0)
        percentage = _pct1(amount, total)
        items.append({
            "key": key,
            "label": label,
            "value": f"{_money(amount)} ({percentage:.0f}%)",
            "amount": amount,
            "percentage": percentage,
            "color_token": _color(idx),
        })

    return {
        "title": "Spend by Category",
        "sub_title": _range_label(ctx["rng"]),
        "type": "horizontal_bar",
        "total": total,
        "items": items,
    }


def _build_pass_rate_by_ql(env, ctx):
    Gen = env["crowley.generation"].sudo()
    scope = ctx["scope"]
    ql_taskers = ctx["ql_taskers"]
    ql_name = ctx["ql_name"]

    totals = _counts_by_user(Gen, scope)
    approved = _counts_by_user(Gen, scope, [("review_state", "=", "approved")])
    reviewed = _counts_by_user(
        Gen, scope, [("review_state", "in", ["approved", "rejected"])]
    )

    items = []
    for ql_id, uids in ql_taskers.items():
        tasks = sum(totals.get(u, 0) for u in uids)
        appr = sum(approved.get(u, 0) for u in uids)
        rev = sum(reviewed.get(u, 0) for u in uids)
        rate = _pct1(appr, rev)
        name = ql_name.get(ql_id, UNASSIGNED_QL_LABEL)
        items.append({
            "key": ql_id,
            "label": name,
            "initials": _initials(name),
            "value": f"{rate}% · {tasks} tasks",
            "pass_rate": rate,
            "tasks": tasks,
            "reviewed": rev,
            "taskers": len(uids),
            "color_token": "success" if rate >= PASS_RATE_GOOD else "warn",
        })

    items.sort(key=lambda i: (-i["pass_rate"], -i["tasks"], i["label"]))
    return {
        "title": "QC Pass Rate by QL",
        "sub_title": "",
        "type": "horizontal_bar",
        "items": items,
    }


def _build_daily_burn_rate(env, ctx):
    Gen = env["crowley.generation"].sudo()
    rng = ctx["rng"]
    ql_of_user = ctx["ql_of_user"]
    ql_name = ctx["ql_name"]

    gens = Gen.search(ctx["scope"])

    per_day = {}
    per_ql_total = {}
    ql_ids = set()
    for gen in gens:
        day = gen.create_date.date() if gen.create_date else None
        if not day:
            continue
        ql_id = ql_of_user.get(gen.user_id.id, UNASSIGNED_QL)
        ql_ids.add(ql_id)
        cost = gen.total_cost_usd or 0.0
        per_day.setdefault(day, {})
        per_day[day][ql_id] = per_day[day].get(ql_id, 0.0) + cost
        per_ql_total[ql_id] = per_ql_total.get(ql_id, 0.0) + cost

    color_by_ql = {ql_id: _color(idx) for idx, ql_id in enumerate(sorted(ql_ids))}

    data = []
    cursor = rng["start"]
    grand_total = 0.0
    while cursor <= rng["end"]:
        day_map = per_day.get(cursor, {})
        day_total = round(sum(day_map.values()), 4)
        grand_total += day_total
        data.append({
            "date": cursor.isoformat(),
            "total": day_total,
            "segments": [
                {"key": ql_id, "value": round(day_map.get(ql_id, 0.0), 4)}
                for ql_id in sorted(ql_ids)
                if day_map.get(ql_id)
            ],
        })
        cursor += timedelta(days=1)

    legend = [
        {
            "key": ql_id,
            "label": ql_name.get(ql_id, UNASSIGNED_QL_LABEL),
            "value": _money(per_ql_total.get(ql_id, 0.0)),
            "amount": round(per_ql_total.get(ql_id, 0.0), 4),
            "color_token": color_by_ql[ql_id],
        }
        for ql_id in sorted(ql_ids, key=lambda q: -per_ql_total.get(q, 0.0))
    ]

    return {
        "title": "Daily Burn Rate",
        "sub_title": "",
        "type": "stacked_bar",
        "headline": _money(grand_total),
        "headline_caption": _range_label(rng),
        "legend": legend,
        "data": data,
    }


def _build_analytics(env, ctx):
    # Schema parity with crowley_sourcing_extension analytics: both expose the
    # same top-level keys so one frontend model fits both. `tasks_submitted_per_day`,
    # `qc_verdict_mix` and `qc_verdicts_per_day` are sourcing-only sections —
    # emitted here as always-blank ({}) keys.
    return {
        "role": _user_role_tag(env) or "tasker",
        "kpi": _build_kpi_v2(env, ctx),
        "spend_by_category": _build_spend_by_category(env, ctx),
        "qc_pass_rate_by_ql": _build_pass_rate_by_ql(env, ctx),
        "daily_burn_rate": _build_daily_burn_rate(env, ctx),
        "tasks_submitted_per_day": {},
        "qc_verdict_mix": {},
        "qc_verdicts_per_day": {},
    }


class CrowleyAnalyticsDashboardController(http.Controller):

    @http.route(
        "/api/v1/crowley_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        ctx, error = _resolve_context(env, request.params or {})
        if error is not None:
            return error
        return return_Response(
            message="OK", status=200, data=_build_analytics(env, ctx)
        )
