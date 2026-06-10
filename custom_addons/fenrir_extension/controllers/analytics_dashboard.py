import calendar
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

# Fenrir task status mapping (mirrors crowley_sourcing's state buckets).
DONE_STATES = ("completed", "approved")
IN_FLIGHT_STATES = ("pending_review",)
FAILED_STATES = ("rejected", "cancelled")
COMPLETED_STATES = DONE_STATES
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEATMAP_INTENSITY_LEVELS = 4
LEADERBOARD_WINDOW_DAYS = 30
TIMELINE_WINDOW_DAYS = 30

CACHE_TTL = 60
CACHE_MAX_ENTRIES = 256
_CACHE = {}

MANAGER_GROUP = "fenrir.group_fenrir_manager"
TASKER_GROUP = "fenrir.group_fenrir_tasker"


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


def _user_role_tag(env):
    """Return 'manager' or 'tasker' (or None if user has neither group)."""
    user = env.user
    if user.has_group(MANAGER_GROUP):
        return "manager"
    if user.has_group(TASKER_GROUP):
        return "tasker"
    return None


def _scope(env):
    """Return (role_tag, domain, tasks_recordset) for the calling user.

    Manager  → full visibility across every fenrir.task.
    Tasker   → only tasks where lead_user_id matches the caller.
    """
    tag = _user_role_tag(env)
    user = env.user
    Task = env["fenrir.task"].sudo()
    if tag == "manager":
        return tag, [], Task.search([])
    domain = [("lead_user_id", "=", user.id)]
    return "tasker", domain, Task.search(domain)


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
    rows = env["fenrir.task"].sudo().search_read(
        scope
        + [
            ("status", "in", list(COMPLETED_STATES)),
            ("write_date", ">=", start_dt),
            ("write_date", "<", end_dt),
        ],
        ["write_date"],
    )
    counts = {}
    for row in rows:
        when = row.get("write_date")
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


def _task_cost(task):
    """Sum of accepted seller offers' price_paid_usd for a task."""
    return sum(
        (o.price_paid_usd or o.final_payment_amount or 0.0)
        for o in task.seller_offer_ids.filtered(lambda o: o.accepted == "yes")
    )


def _scope_spend(env, scope, extra=None):
    """Aggregate Fenrir task cost (accepted seller offers) over a scope."""
    Task = env["fenrir.task"].sudo()
    tasks = Task.search(scope + (extra or []))
    return round(sum(_task_cost(t) for t in tasks), 2), len(tasks)


def _build_total_task(env, scope, filters):
    Task = env["fenrir.task"].sudo()
    total = Task.search_count(
        scope + _create_date_domain(filters["start"], filters["end"])
    )
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters["start"], filters["end"]
    )
    current = Task.search_count(scope + _create_date_domain(cur_start, cur_end))
    previous = Task.search_count(scope + _create_date_domain(prev_start, prev_end))
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
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    total_cost, counted = _scope_spend(env, domain)
    average_cost = (total_cost / counted) if counted else 0.0
    return {
        "tasks_with_cost": counted,
        "average_cost": round(average_cost, 6),
        "total_cost": round(total_cost, 6),
    }


def _build_avg_duration(env, scope, filters):
    Task = env["fenrir.task"].sudo()
    domain = (
        scope
        + _create_date_domain(filters["start"], filters["end"])
        + [("estimated_completion_time_hours", ">", 0)]
    )
    groups = Task._read_group(
        domain, [], ["__count", "estimated_completion_time_hours:avg"]
    )
    measured, avg_hours = groups[0] if groups else (0, 0.0)
    measured = measured or 0
    avg_hours = avg_hours or 0.0
    avg_seconds = avg_hours * 3600.0
    return {
        "tasks_measured": measured,
        "average_duration_seconds": round(avg_seconds, 2),
        "average_duration_minutes": round(avg_seconds / 60.0, 2),
        "average_duration_display": _fmt_duration(avg_seconds),
    }


def _build_failed_task(env, scope, filters):
    Task = env["fenrir.task"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    total = Task.search_count(base)
    failed = Task.search_count(base + [("status", "in", list(FAILED_STATES))])
    return {
        "failed_task_count": failed,
        "total_task_count": total,
        "failure_percentage": _pct(failed, total),
    }


def _build_team_members(env, tasks):
    leads = tasks.mapped("lead_user_id")
    reviewers = tasks.mapped("reviewer_id")
    members = leads | reviewers
    return {
        "total_team_members": len(members),
        "role_breakdown": [
            {"role": "lead", "count": len(leads)},
            {"role": "reviewer", "count": len(reviewers)},
        ],
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
    Task = env["fenrir.task"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Task._read_group(domain, ["status"], ["__count"])
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
        for key, label in Task._fields["status"].selection
    ]
    return {
        "total_task_count": total,
        "no_verdict_count": no_state,
        "distribution": distribution,
    }


def _build_qc_leaderboard(env, tasks, filters):
    """Reviewer leaderboard — ranks reviewers by tasks_completed in window."""
    Task = env["fenrir.task"].sudo()
    today = fields.Datetime.now().date()
    window_end = filters["end"] or today
    window_start = filters["start"] or (
        window_end - timedelta(days=LEADERBOARD_WINDOW_DAYS - 1)
    )
    date_domain = _create_date_domain(window_start, window_end)

    reviewers_by_id = {}
    for task in tasks:
        for reviewer in task.reviewer_id:
            reviewers_by_id[reviewer.id] = reviewer.name or ""

    leaderboard = []
    for reviewer_id, reviewer_name in reviewers_by_id.items():
        base = date_domain + [("reviewer_id", "=", reviewer_id)]
        total = Task.search_count(base)
        completed = Task.search_count(base + [("status", "in", list(DONE_STATES))])
        leaderboard.append({
            "qc_id": reviewer_id,
            "qc_name": reviewer_name,
            "total_taskers": 0,
            "tasks_completed": completed,
            "tasks_total": total,
            "completion_percentage": _pct(completed, total),
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


DONE_STATE = "completed"
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")
PASS_RATE_GOOD = 90.0
UNASSIGNED_CATEGORY = 0
UNASSIGNED_CATEGORY_LABEL = "Uncategorized"


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


def _build_category_maps(tasks):
    """Map cat_id → label and cat_id → set(lead_user_ids) over the tasks set.

    Crowley grouped breakdowns by 'QL' (a person) because each tasker has one
    QL. In fenrir a single lead_user can own tasks across many categories, so
    the breakdown axis is the task's category — not the user's category — and
    aggregation MUST be per-task, not via a user→category lookup.
    """
    cat_name = {UNASSIGNED_CATEGORY: UNASSIGNED_CATEGORY_LABEL}
    cat_users = {}
    for task in tasks:
        cat = task.category_id
        cat_id = cat.id or UNASSIGNED_CATEGORY
        cat_name[cat_id] = cat.name or UNASSIGNED_CATEGORY_LABEL
        uid = task.lead_user_id.id
        if uid:
            cat_users.setdefault(cat_id, set()).add(uid)
    return {
        "ql_name": cat_name,
        "ql_taskers": cat_users,
    }


def _counts_by_user(Task, scope, extra=None):
    rows = Task.formatted_read_group(
        scope + (extra or []), ["lead_user_id"], ["__count"]
    )
    out = {}
    for r in rows:
        user = r["lead_user_id"]
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


def _budget_usd(env):
    raw = env["ir.config_parameter"].sudo().get_param("fenrir.budget_usd", "0")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _longest_streak(env, ctx):
    rng = ctx["rng"]
    counts = _completed_day_counts(
        env, ctx.get("role_domain", []), rng["start"], rng["end"]
    )
    best = streak = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        if counts.get(cursor, 0) > 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
        cursor += timedelta(days=1)
    return best


def _build_analytics_kpi(env, ctx, view):
    Task = env["fenrir.task"].sudo()
    scope = ctx["scope"]
    total = Task.search_count(scope)
    approved = Task.search_count(scope + [("status", "in", list(DONE_STATES))])
    rejected = Task.search_count(scope + [("status", "in", list(FAILED_STATES))])
    reviewed = approved + rejected
    force_passed = Task.search_count(
        scope + [("status", "=", "approved"), ("reviewer_id", "=", False)]
    )
    spent, _ = _scope_spend(env, scope)
    budget = _budget_usd(env)
    pass_rate = _pct1(approved, reviewed)
    force_rate = _pct1(force_passed, reviewed)
    re_verify_rate = _pct1(rejected, reviewed)
    budget_used = _pct1(spent, budget)
    pass_kpi = _kpi(
        "qc_pass_rate", "QC Pass Rate", f"{pass_rate}%",
        sub_string=f"{approved} of {reviewed} reviewed",
    )
    force_kpi = _kpi(
        "force_pass_rate", "Force-Pass Rate", f"{force_rate}%",
        sub_string=f"{force_passed} force-passes",
    )
    if view == "manager":
        items = [
            pass_kpi, force_kpi,
            _kpi("budget_used", "Budget Used", f"{budget_used}%",
                 sub_string=f"{_money(spent)} of {_money(budget)}"),
            _kpi("tasks_submitted", "Tasks Submitted", str(total),
                 sub_string=_range_label(ctx["rng"])),
        ]
    else:
        items = [
            pass_kpi, force_kpi,
            _kpi("re_verify_rate", "Re-Verify Rate", f"{re_verify_rate}%",
                 sub_string=f"{rejected} re-verifies"),
            _kpi("longest_streak", "Longest Streak",
                 f"{_longest_streak(env, ctx)} days",
                 sub_string="consecutive active days"),
        ]
    return {"count": len(items), "items": items}


def _build_spend_by_category(env, ctx):
    Task = env["fenrir.task"].sudo()
    Category = env["fenrir.category"].sudo()
    tasks = Task.search(ctx["scope"])
    amount_by_cat = {}
    for t in tasks:
        cat_id = t.category_id.id or 0
        amount_by_cat[cat_id] = amount_by_cat.get(cat_id, 0.0) + _task_cost(t)
    total = round(sum(amount_by_cat.values()), 4)

    cat_records = {c.id: c for c in Category.search([])}
    cat_records[0] = None
    ordered = sorted(amount_by_cat.items(), key=lambda kv: -kv[1])

    items = []
    for idx, (cat_id, amount) in enumerate(ordered):
        label = (cat_records.get(cat_id).name if cat_records.get(cat_id)
                 else UNASSIGNED_CATEGORY_LABEL)
        percentage = _pct1(amount, total)
        items.append({
            "key": cat_id,
            "label": label,
            "value": f"{_money(amount)} ({percentage:.0f}%)",
            "amount": round(amount, 4),
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
    """Pass rate broken out by task.category_id (not by lead_user)."""
    Task = env["fenrir.task"].sudo()
    scope = ctx["scope"]
    cat_name = ctx["ql_name"]
    cat_users = ctx["ql_taskers"]

    def _by_cat(extra=None):
        rows = Task.formatted_read_group(
            scope + (extra or []), ["category_id"], ["__count"]
        )
        out = {}
        for r in rows:
            cat = r["category_id"]
            cat_id = cat[0] if cat else UNASSIGNED_CATEGORY
            out[cat_id] = r["__count"]
        return out

    totals = _by_cat()
    approved = _by_cat([("status", "in", list(DONE_STATES))])
    reviewed = _by_cat([("status", "in", list(DONE_STATES) + list(FAILED_STATES))])

    items = []
    for cat_id in totals:
        tasks = totals.get(cat_id, 0)
        appr = approved.get(cat_id, 0)
        rev = reviewed.get(cat_id, 0)
        rate = _pct1(appr, rev)
        name = cat_name.get(cat_id, UNASSIGNED_CATEGORY_LABEL)
        items.append({
            "key": cat_id,
            "label": name,
            "initials": _initials(name),
            "value": f"{rate}% · {tasks} tasks",
            "pass_rate": rate,
            "tasks": tasks,
            "reviewed": rev,
            "taskers": len(cat_users.get(cat_id, set())),
            "color_token": "success" if rate >= PASS_RATE_GOOD else "warn",
        })

    items.sort(key=lambda i: (-i["pass_rate"], -i["tasks"], i["label"]))
    return {
        "title": "QC Pass Rate by Category",
        "sub_title": "",
        "type": "horizontal_bar",
        "items": items,
    }


def _build_daily_burn_rate(env, ctx):
    """Daily stacked-bar series, bucketed by each task's own category."""
    Task = env["fenrir.task"].sudo()
    rng = ctx["rng"]
    cat_name = ctx["ql_name"]

    tasks = Task.search(ctx["scope"])

    per_day = {}
    per_cat_total = {}
    cat_ids = set()
    for task in tasks:
        day = task.create_date.date() if task.create_date else None
        if not day:
            continue
        cat_id = task.category_id.id or UNASSIGNED_CATEGORY
        if cat_id not in cat_name:
            cat_name[cat_id] = task.category_id.name or UNASSIGNED_CATEGORY_LABEL
        cat_ids.add(cat_id)
        cost = _task_cost(task)
        per_day.setdefault(day, {})
        per_day[day][cat_id] = per_day[day].get(cat_id, 0.0) + cost
        per_cat_total[cat_id] = per_cat_total.get(cat_id, 0.0) + cost

    color_by_cat = {cat_id: _color(idx) for idx, cat_id in enumerate(sorted(cat_ids))}

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
                {"key": cat_id, "value": round(day_map.get(cat_id, 0.0), 4)}
                for cat_id in sorted(cat_ids)
                if day_map.get(cat_id)
            ],
        })
        cursor += timedelta(days=1)

    legend = [
        {
            "key": cat_id,
            "label": cat_name.get(cat_id, UNASSIGNED_CATEGORY_LABEL),
            "value": _money(per_cat_total.get(cat_id, 0.0)),
            "amount": round(per_cat_total.get(cat_id, 0.0), 4),
            "color_token": color_by_cat[cat_id],
        }
        for cat_id in sorted(cat_ids, key=lambda q: -per_cat_total.get(q, 0.0))
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


def _per_day_series(env, ctx, extra=None):
    Task = env["fenrir.task"].sudo()
    rng = ctx["rng"]
    per_day = {}
    for task in Task.search(ctx["scope"] + (extra or [])):
        if task.create_date:
            day = task.create_date.date()
            per_day[day] = per_day.get(day, 0) + 1
    data = []
    total = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        count = per_day.get(cursor, 0)
        total += count
        data.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%b %d"),
            "value": count,
        })
        cursor += timedelta(days=1)
    return data, total


def _build_tasks_submitted_per_day(env, ctx):
    data, total = _per_day_series(env, ctx)
    return {
        "title": "Tasks Submitted per Day",
        "sub_title": _range_label(ctx["rng"]),
        "type": "line",
        "total": total,
        "data": data,
    }


def _build_qc_verdict_mix(env, ctx):
    Task = env["fenrir.task"].sudo()
    counts = {"pass": 0, "fail": 0, "force_pass": 0}
    for task in Task.search(ctx["scope"]):
        if task.status == "approved" and not task.reviewer_id:
            counts["force_pass"] += 1
        elif task.status in DONE_STATES:
            counts["pass"] += 1
        elif task.status in FAILED_STATES:
            counts["fail"] += 1
    total = sum(counts.values())
    labels = {"pass": "Pass", "fail": "Fail", "force_pass": "Force-Pass"}
    colors = {"pass": "success", "fail": "danger", "force_pass": "warn"}
    items = []
    for key in ("pass", "fail", "force_pass"):
        amount = counts[key]
        percentage = _pct1(amount, total)
        items.append({
            "key": key,
            "label": labels[key],
            "value": f"{amount} ({percentage:.0f}%)",
            "amount": amount,
            "percentage": percentage,
            "color_token": colors[key],
        })
    return {
        "title": "QC Verdict Mix",
        "sub_title": _range_label(ctx["rng"]),
        "type": "stacked_bar",
        "total": total,
        "items": items,
    }


def _build_qc_verdicts_per_day(env, ctx):
    data, total = _per_day_series(
        env, ctx, [("status", "in", list(DONE_STATES) + list(FAILED_STATES))]
    )
    return {
        "title": "QC Verdicts per Day",
        "sub_title": _range_label(ctx["rng"]),
        "type": "line",
        "total": total,
        "data": data,
    }


# Which analytics view each fenrir role sees.
ANALYTICS_VIEW_BY_TAG = {"manager": "manager", "tasker": "ql"}

# Which analytics blocks each view's page actually shows (mirrors the UI).
# Every section KEY is always present in the response; sections NOT in a view's
# set are returned blank ({}) for that role rather than computed.
SECTIONS_BY_VIEW = {
    "manager": (
        "spend_by_category",
        "qc_pass_rate_by_ql",
        "daily_burn_rate",
        "tasks_submitted_per_day",
    ),
    "ql": (
        "qc_pass_rate_by_ql",
        "qc_verdict_mix",
        "qc_verdicts_per_day",
    ),
}


def _resolve_role_context(env, params):
    tag = _user_role_tag(env)
    if tag is None:
        return None, return_Response(
            message="You are not allowed to access Fenrir analytics.",
            status=403,
        )
    rng, error = _resolve_range(params)
    if error is not None:
        return None, error
    _t, role_domain, tasks = _scope(env)
    ctx = {"tag": tag, "rng": rng, "tasks": tasks, "role_domain": role_domain}
    ctx.update(_build_category_maps(tasks))
    ctx["scope"] = role_domain + _range_domain(rng)
    return ctx, None


def _build_analytics(env, ctx):
    view = ANALYTICS_VIEW_BY_TAG.get(ctx["tag"], "ql")
    sections = SECTIONS_BY_VIEW[view]

    def _section(key, builder):
        return builder() if key in sections else {}

    return {
        "role": ctx["tag"] or "tasker",
        "kpi": _build_analytics_kpi(env, ctx, view),
        "spend_by_category": _section(
            "spend_by_category", lambda: _build_spend_by_category(env, ctx)
        ),
        "qc_pass_rate_by_ql": _section(
            "qc_pass_rate_by_ql", lambda: _build_pass_rate_by_ql(env, ctx)
        ),
        "daily_burn_rate": _section(
            "daily_burn_rate", lambda: _build_daily_burn_rate(env, ctx)
        ),
        "tasks_submitted_per_day": _section(
            "tasks_submitted_per_day",
            lambda: _build_tasks_submitted_per_day(env, ctx),
        ),
        "qc_verdict_mix": _section(
            "qc_verdict_mix", lambda: _build_qc_verdict_mix(env, ctx)
        ),
        "qc_verdicts_per_day": _section(
            "qc_verdicts_per_day", lambda: _build_qc_verdicts_per_day(env, ctx)
        ),
    }


class FenrirAnalyticsDashboardController(http.Controller):

    @http.route(
        "/api/v1/fenrir_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def fenrir_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        ctx, error = _resolve_role_context(env, request.params or {})
        if error is not None:
            return error
        return return_Response(
            message="OK",
            status=200,
            data=_build_analytics(env, ctx),
        )
