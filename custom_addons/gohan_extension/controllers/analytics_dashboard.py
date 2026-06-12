import calendar
from datetime import datetime, timedelta
from urllib.parse import urlparse

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

COMPLETED_STATES = ("done", "submitted")
FAILED_STATES = ("failed", "discarded", "cancelled")
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


# ─────────────────────────────────────────────────────────────────────────
# Shared widget / badge layer (Gohan pen design).
#
# Every enum field renders as a ``{key, label, color_token}`` badge. The
# ``color_token`` is a SEMANTIC token the Flutter app maps to a colour — the
# same vocabulary leviathan_extension uses (``primary/success/info/warn/
# danger/muted``), NOT raw hex. Homed here because the Overview, Tasks and URLs
# controllers all import from this module.
# ─────────────────────────────────────────────────────────────────────────

# gohan.job.state -> (Status badge label, color_token). Labels match the pen
# Tasks "Status" column (granular per-stage, not grouped).
STATE_BADGE = {
    "not_assigned": ("Not Assigned", "muted"),
    "draft": ("Draft", "muted"),
    "extracting": ("Extracting", "info"),
    "generating": ("Generating PRD", "warn"),
    "scoring": ("Scoring", "primary"),
    "done": ("Done", "success"),
    "submitted": ("Submitted", "success"),
    "failed": ("Failed", "danger"),
    "discarded": ("Discarded", "danger"),
    "cancelled": ("Cancelled", "danger"),
}

# gohan.job.qc_verdict -> (pen label, color_token). Pen relabels
# fixes -> "Needs Review", not_shippable -> "Unshippable".
QC_VERDICT_BADGE = {
    "shippable": ("Shippable", "success"),
    "fixes": ("Needs Review", "warn"),
    "not_shippable": ("Unshippable", "danger"),
    "pending": ("Pending", "muted"),
}

# 6-stage Task Progress funnel (Overview ProgressCard):
# (key, label, member states, color_token). Mirrors the pen's six bars.
STAGE_BUCKETS = (
    ("draft", "Draft", ("not_assigned", "draft"), "muted"),
    ("extracting", "Extracting", ("extracting",), "info"),
    ("generating", "Generating PRD", ("generating",), "warn"),
    ("scoring", "Scoring", ("scoring",), "primary"),
    ("done", "Done", ("done", "submitted"), "success"),
    ("failed", "Failed", ("failed", "discarded", "cancelled"), "danger"),
)

CATEGORY_COLOR_TOKENS = ("muted", "primary", "info", "warn", "success", "danger")

# Team Size / Team Members breakdown labels. Decision: CTO-PL == project lead,
# so pl -> "CTO"; qr -> "QL" (the pen's role labels).
TEAM_ROLE_LABELS = (("tpm", "TPM"), ("pl", "CTO"), ("qr", "QL"), ("tasker", "Tasker"))


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _format_month_day(d):
    return f"{d.strftime('%b')} {d.day}" if d else ""


def _format_short_date(dt):
    return f"{dt.strftime('%b')} {dt.day}" if dt else ""


def _format_long_date(dt):
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}" if dt else ""


def _domain_from_url(raw):
    if not raw:
        return ""
    try:
        host = urlparse(raw).netloc or urlparse(raw).path
    except Exception:
        return raw
    return (host or "").lstrip("www.")


def _state_badge(state):
    label, color = STATE_BADGE.get(
        state or "", ((state or "").replace("_", " ").title() or "—", "muted")
    )
    return {"key": state or "", "label": label, "color_token": color}


def _qc_badge(qc_verdict):
    if not qc_verdict:
        return {"key": "", "label": "—", "color_token": "muted"}
    label, color = QC_VERDICT_BADGE.get(qc_verdict, (qc_verdict, "muted"))
    return {"key": qc_verdict, "label": label, "color_token": color}


def _score_band_token(score):
    """Pen score chip colour band: >=80 lime, 60-79 yellow, <60 red."""
    if not score:
        return "muted"
    if score >= 80:
        return "success"
    if score >= 60:
        return "warn"
    return "danger"


def _grade_band_token(grade):
    """Letter grade shares the score colour band (A/B good, C/D mid, else low)."""
    letter = (grade or "").strip().upper()[:1]
    if not letter:
        return "muted"
    if letter in ("A", "B"):
        return "success"
    if letter in ("C", "D"):
        return "warn"
    return "danger"


def _source_badge(via_batch):
    if via_batch:
        return {"key": "bulk", "label": "Bulk CSV", "color_token": "info"}
    return {"key": "single", "label": "Single", "color_token": "muted"}


def _category_color_token(category_id):
    if not category_id:
        return "muted"
    return CATEGORY_COLOR_TOKENS[category_id % len(CATEGORY_COLOR_TOKENS)]


def _category_badge(category):
    """``category`` is a gohan.category record (may be empty)."""
    cid = category.id or 0
    return {
        "key": str(cid or ""),
        "label": category.name or "",
        "color_token": _category_color_token(cid),
    }


def _time_ago(when):
    if not when:
        return ""
    seconds = (fields.Datetime.now() - when).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def _classify_job_action(state):
    return {
        "submitted": "submitted",
        "done": "completed",
        "failed": "failed",
        "discarded": "discarded",
        "cancelled": "discarded",
        "scoring": "qc_scoring",
        "generating": "generation_complete",
        "extracting": "scraping",
        "draft": "created",
        "not_assigned": "created",
    }.get(state, "updated")


def _team_breakdown_sub(role_counts):
    parts = [
        f"{role_counts.get(key, 0)} {label}"
        for key, label in TEAM_ROLE_LABELS
        if role_counts.get(key, 0)
    ]
    return " · ".join(parts) if parts else "No assigned roles"


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
    groups = Job._read_group(domain, [], ["__count", "score:sum"])
    scored, total_score = groups[0] if groups else (0, 0.0)
    scored = scored or 0
    total_score = total_score or 0.0
    avg_max = 100.0
    average_score = (total_score / scored) if scored else 0.0
    return {
        "tasks_scored": scored,
        "average_score": round(average_score, 2),
        "average_score_percentage": _pct(average_score, avg_max),
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


def _build_aht_overview_aligned(env, scope, filters):
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


def _build_url_overview_aligned(env, scope, filters):
    Job = env["gohan.job"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    return {
        "tasks_with_url": Job.search_count(base),
        "rejected_at_validation": Job.search_count(base + [("state", "=", "failed")]),
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
    Job = env["gohan.job"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Job._read_group(domain, ["state"], ["__count"])
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
        for key, label in Job._fields["state"].selection
    ]
    return {"total_task_count": total, "status_chart": chart}


# ─────────────────────────────────────────────────────────────────────────
# Crowley-style analytics payload.
#
# The Flutter InternalAnalyticsTab probes the response root for
# `spend_by_category` / `qc_pass_rate_by_ql` / `daily_burn_rate`, so this
# endpoint must return those keys at the top level (no wrapper). The shape
# mirrors crowley_extension's analytics_dashboard `_build_analytics`, adapted
# to the gohan.job domain: cost is `llm_qc_cost_usd` (not crowley.attempt
# cost_usd), category is the `category_id` Many2one (not a selection), and
# "pass" means a `qc_verdict` of shippable/fixes (not review_state approved).
# The "QL" grouping reuses each tasker's task_forge_qr_id (their QC reviewer).
# ─────────────────────────────────────────────────────────────────────────

APPROVED_VERDICTS = ("shippable", "fixes")
DECIDED_VERDICTS = ("shippable", "fixes", "not_shippable")

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


def _counts_by_user(Job, scope, extra=None):
    rows = Job.formatted_read_group(scope + (extra or []), ["user_id"], ["__count"])
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


def _build_kpi_v2(env, ctx):
    """The pen Analytics KPI strip: 6 tiles —
    Total Tasks · Avg Score · Avg Duration · Failed · Team Members · Open Blockers.

    Reshaped from the old crowley spend/token tiles. Open Blockers is
    empty-by-design (gohan.job has no blocker/retry source field; renders "—").
    """
    Job = env["gohan.job"].sudo()
    scope = ctx["scope"]
    rng = ctx["rng"]

    # Previous equal-length window immediately before the range, for deltas.
    length = (rng["end"] - rng["start"]).days + 1
    prev_end = rng["start"] - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    prev_domain = [("user_id", "in", ctx["user_ids"])] + [
        ("create_date", ">=", datetime.combine(prev_start, datetime.min.time())),
        (
            "create_date",
            "<",
            datetime.combine(prev_end, datetime.min.time()) + timedelta(days=1),
        ),
    ]

    total_tasks = Job.search_count(scope)
    prev_total = Job.search_count(prev_domain)

    score_rows = Job.formatted_read_group(
        scope + [("score", ">", 0)], [], ["__count", "score:avg"]
    )
    scored = (score_rows[0]["__count"] if score_rows else 0) or 0
    avg_score = (score_rows[0]["score:avg"] if score_rows else 0.0) or 0.0
    prev_score_rows = Job.formatted_read_group(
        prev_domain + [("score", ">", 0)], [], ["score:avg"]
    )
    prev_avg_score = (
        prev_score_rows[0]["score:avg"] if prev_score_rows else 0.0
    ) or 0.0

    dur_rows = Job.formatted_read_group(
        scope + [("duration_seconds", ">", 0)], [], ["duration_seconds:avg"]
    )
    avg_seconds = (dur_rows[0]["duration_seconds:avg"] if dur_rows else 0.0) or 0.0
    prev_dur_rows = Job.formatted_read_group(
        prev_domain + [("duration_seconds", ">", 0)], [], ["duration_seconds:avg"]
    )
    prev_avg_seconds = (
        prev_dur_rows[0]["duration_seconds:avg"] if prev_dur_rows else 0.0
    ) or 0.0

    failed = Job.search_count(scope + [("state", "in", list(FAILED_STATES))])
    prev_failed = Job.search_count(
        prev_domain + [("state", "in", list(FAILED_STATES))]
    )

    project = ctx["project"]
    pl_employees = project.mapped("project_lead")
    qr_employees = project.mapped("project_qc_reviewer")
    tasker_employees = project.mapped("project_tasker")
    aire_employees = project.mapped("project_aire")
    swe_employees = project.mapped("project_swe")
    tpm_employees = pl_employees.mapped("task_forge_tpm_id")
    team_employees = (
        pl_employees
        | qr_employees
        | tasker_employees
        | aire_employees
        | swe_employees
        | tpm_employees
    )
    team_total = len(team_employees)
    team_sub_string = _team_breakdown_sub(
        {
            "tpm": len(tpm_employees),
            "pl": len(pl_employees),
            "qr": len(qr_employees),
            "tasker": len(tasker_employees),
            "aire": len(aire_employees),
            "swe": len(swe_employees),
        }
    )

    total_item = _kpi(
        "total_tasks", "Total Tasks", str(total_tasks),
        sub_string="Pipeline tasks created",
    )
    total_item.update(_trend(total_tasks, prev_total, as_pct=True))

    score_item = _kpi(
        "avg_score", "Avg Score",
        f"{round(avg_score)}" if avg_score else "—",
        sub_string=(
            f"Across {scored} scored tasks" if scored else "No scored tasks yet"
        ),
    )
    score_item.update(_trend(round(avg_score), round(prev_avg_score)))

    duration_item = _kpi(
        "avg_duration", "Avg Duration",
        _fmt_duration(avg_seconds) if avg_seconds else "—",
        sub_string="Extraction → Scoring",
    )
    duration_item.update(
        _trend(
            round(avg_seconds), round(prev_avg_seconds),
            lower_is_better=True, unit="s",
        )
    )

    failed_item = _kpi(
        "failed", "Failed", str(failed),
        sub_string="Pipeline-stage failures",
    )
    failed_item.update(_trend(failed, prev_failed, lower_is_better=True))

    items = [
        total_item,
        score_item,
        duration_item,
        failed_item,
        _kpi(
            "team_members", "Team Members", str(team_total),
            sub_string=team_sub_string,
        ),
        # Open Blockers — empty by design (no blocker/retry source on gohan.job).
        _kpi("open_blockers", "Open Blockers", "", sub_string=""),
    ]
    return {"count": len(items), "items": items}


def _build_spend_by_category(env, ctx):
    Job = env["gohan.job"].sudo()
    rows = Job.formatted_read_group(
        ctx["scope"] + [("category_id", "!=", False)],
        ["category_id"],
        ["llm_qc_cost_usd:sum"],
    )
    amount_by_cat = {}
    label_by_cat = {}
    for r in rows:
        cat = r["category_id"]
        if not cat:
            continue
        amount_by_cat[cat[0]] = round(r["llm_qc_cost_usd:sum"] or 0.0, 4)
        label_by_cat[cat[0]] = cat[1]
    total = round(sum(amount_by_cat.values()), 4)

    # Only categories with real spend. gohan does not populate llm_qc_cost_usd
    # yet, so today this yields no items and the card hides (the Flutter
    # InternalAnalyticsEntity.hasSpendByCategory checks items.isNotEmpty). It
    # self-heals: categories appear once the QC pipeline writes cost.
    ordered = sorted(
        (cid for cid in amount_by_cat if amount_by_cat[cid] > 0),
        key=lambda cid: (-amount_by_cat[cid], label_by_cat.get(cid, "")),
    )

    items = []
    for idx, cid in enumerate(ordered):
        amount = amount_by_cat[cid]
        percentage = _pct1(amount, total)
        items.append({
            "key": cid,
            "label": label_by_cat.get(cid, ""),
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
    Job = env["gohan.job"].sudo()
    scope = ctx["scope"]
    ql_taskers = ctx["ql_taskers"]
    ql_name = ctx["ql_name"]

    totals = _counts_by_user(Job, scope)
    approved = _counts_by_user(
        Job, scope, [("qc_verdict", "in", list(APPROVED_VERDICTS))]
    )
    reviewed = _counts_by_user(
        Job, scope, [("qc_verdict", "in", list(DECIDED_VERDICTS))]
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
    Job = env["gohan.job"].sudo()
    rng = ctx["rng"]
    ql_of_user = ctx["ql_of_user"]
    ql_name = ctx["ql_name"]

    jobs = Job.search(ctx["scope"])

    per_day = {}
    per_ql_total = {}
    ql_ids = set()
    for job in jobs:
        day = job.create_date.date() if job.create_date else None
        if not day:
            continue
        ql_id = ql_of_user.get(job.user_id.id, UNASSIGNED_QL)
        ql_ids.add(ql_id)
        cost = job.llm_qc_cost_usd or 0.0
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

    # No spend in range (llm_qc_cost_usd is not populated by the gohan pipeline
    # yet) — blank the series so the card hides instead of drawing a flat-zero
    # chart (Flutter InternalAnalyticsEntity.hasDailyBurnRate checks
    # data.isNotEmpty). Self-heals once cost is written. The section key stays
    # present for schema parity.
    if grand_total <= 0:
        legend = []
        data = []

    return {
        "title": "Daily Burn Rate",
        "sub_title": "",
        "type": "stacked_bar",
        "headline": _money(grand_total),
        "headline_caption": _range_label(rng),
        "legend": legend,
        "data": data,
    }


LEADERBOARD_LOW_SCORE = 70


def _trend(current, previous, lower_is_better=False, unit="", as_pct=False):
    """Period-over-period trend fields for a KPI tile. Returns pattern/sign (the
    arrow vocabulary the Flutter KPI card already reads) plus a numeric ``delta``
    string and ``is_positive`` flag (bonus fields for the redesigned card)."""
    diff = round(current - previous)
    if diff == 0:
        return {"pattern": "", "sign": "", "delta": "", "is_positive": True}
    up = diff > 0
    is_positive = (not up) if lower_is_better else up
    if as_pct:
        pct = _diff_pct(current, previous)
        delta = f"{'+' if pct >= 0 else ''}{pct:g}%"
    else:
        delta = f"{'+' if up else '-'}{abs(diff)}{unit}"
    return {
        "pattern": "up" if up else "down",
        "sign": "+" if up else "-",
        "delta": delta,
        "is_positive": is_positive,
    }


def _failed_day_counts(env, scope, win_start, win_end):
    """Failed jobs per day. gohan.job has no failed_at, so the failure day is
    approximated by write_date (when the job last changed ≈ when it failed)."""
    start_dt = datetime.combine(win_start, datetime.min.time())
    end_dt = datetime.combine(win_end, datetime.min.time()) + timedelta(days=1)
    rows = env["gohan.job"].sudo().search_read(
        scope
        + [
            ("state", "in", list(FAILED_STATES)),
            ("write_date", ">=", start_dt),
            ("write_date", "<", end_dt),
        ],
        ["write_date"],
    )
    counts = {}
    for row in rows:
        when = row.get("write_date")
        if when:
            day = when.date()
            counts[day] = counts.get(day, 0) + 1
    return counts


def _build_completed_heatmap(env, ctx):
    """Calendar heatmap: tasks completed per day over the range (pen)."""
    rng = ctx["rng"]
    user_domain = [("user_id", "in", ctx["user_ids"])]
    counts = _completed_day_counts(env, user_domain, rng["start"], rng["end"])
    max_count = max(counts.values()) if counts else 0
    days = []
    total = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        count = counts.get(cursor, 0)
        total += count
        days.append({
            "date": cursor.isoformat(),
            "day": cursor.day,
            "weekday": cursor.weekday(),
            "weekday_label": WEEKDAY_LABELS[cursor.weekday()],
            "count": count,
            "intensity": _intensity(count, max_count),
        })
        cursor += timedelta(days=1)
    total_days = len(days)
    active_days = sum(1 for d in days if d["count"] > 0)
    return {
        "label": "Tasks Completed",
        "sub_string": f"Tasks completed per day · {_range_label(rng)}",
        "window": {"start": rng["start"].isoformat(), "end": rng["end"].isoformat()},
        "max_count": max_count,
        "total_completed": total,
        "days": days,
        "summary": {
            "total_tasks": total,
            "avg_per_day": round(total / total_days, 2) if total_days else 0.0,
            "active_days": active_days,
            "total_days": total_days,
        },
    }


def _build_qc_distribution(env, ctx):
    """QC verdict donut (Shippable / Needs Review / Unshippable) over scored
    tasks in range. Counts per verdict directly to avoid any selection-field
    group-key ambiguity."""
    Job = env["gohan.job"].sudo()
    counts = {
        key: Job.search_count(ctx["scope"] + [("qc_verdict", "=", key)])
        for key in DECIDED_VERDICTS
    }
    total_scored = sum(counts.values())
    verdicts = []
    for key in DECIDED_VERDICTS:
        label, token = QC_VERDICT_BADGE.get(key, (key, "muted"))
        count = counts.get(key, 0)
        verdicts.append({
            "key": key,
            "label": label,
            "count": count,
            "pct": _pct1(count, total_scored),
            "color_token": token,
        })
    return {
        "label": "QC Verdict Distribution",
        "sub_string": f"All scored tasks · {total_scored} total",
        "total_scored": total_scored,
        "verdicts": verdicts,
    }


def _build_leaderboard(env, ctx):
    """Per-tasker leaderboard ranked by task volume then avg score (pen). Low
    scorers carry needs_attention for the "NEEDS ATTENTION" divider."""
    Job = env["gohan.job"].sudo()
    domain = [("user_id", "in", ctx["user_ids"])] + _range_domain(ctx["rng"])
    count_rows = Job.formatted_read_group(domain, ["user_id"], ["__count"])
    score_rows = Job.formatted_read_group(
        domain + [("score", ">", 0)], ["user_id"], ["score:avg"]
    )
    task_count = {}
    for row in count_rows:
        user = row["user_id"]
        if user:
            task_count[user[0]] = row["__count"]
    avg_score = {}
    for row in score_rows:
        user = row["user_id"]
        if user:
            avg_score[user[0]] = row["score:avg"] or 0.0
    user_name = {
        emp.user_id.id: emp.user_id.name
        for emp in ctx["taskers"]
        if emp.user_id
    }

    items = []
    for uid, count in task_count.items():
        score = round(avg_score.get(uid, 0.0))
        name = user_name.get(uid) or ""
        items.append({
            "name": name,
            "initials": _initials(name),
            "task_count": count,
            "score": score,
            "avatar_color": _color(len(items)),
            "needs_attention": bool(score) and score < LEADERBOARD_LOW_SCORE,
        })
    items.sort(key=lambda i: (-i["task_count"], -i["score"], i["name"]))
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {
        "label": "Team Leaderboard",
        "sub_string": f"{_range_label(ctx['rng'])} performance ranking",
        "count": len(items),
        "items": items,
    }


def _build_completion_timeline(env, ctx):
    """Stacked Done/Failed daily bar chart over the range (pen)."""
    rng = ctx["rng"]
    user_domain = [("user_id", "in", ctx["user_ids"])]
    done_counts = _completed_day_counts(env, user_domain, rng["start"], rng["end"])
    failed_counts = _failed_day_counts(env, user_domain, rng["start"], rng["end"])
    items = []
    y_max = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        done = done_counts.get(cursor, 0)
        failed = failed_counts.get(cursor, 0)
        y_max = max(y_max, done + failed)
        items.append({
            "date": cursor.isoformat(),
            "done_count": done,
            "failed_count": failed,
        })
        cursor += timedelta(days=1)
    return {
        "label": "Task Completion Timeline",
        "sub_string": f"Daily task volume by outcome · {_range_label(rng)}",
        "y_max": y_max,
        "items": items,
        "legend": [
            {"key": "done", "label": "Done", "color_token": "success"},
            {"key": "failed", "label": "Failed", "color_token": "danger"},
        ],
    }


def _build_analytics(env, ctx):
    """The pen Analytics tab widget set: 6 KPI tiles, a calendar heatmap, a QC
    verdict donut, a per-tasker leaderboard and a Done/Failed timeline.

    Gohan is internal/cost-free, so the crowley spend/burn keys
    (``spend_by_category`` / ``qc_pass_rate_by_ql`` / ``daily_burn_rate``) — which
    the pen does NOT show — are emitted as blank ({}) stubs for schema parity.
    """
    rng = ctx["rng"]
    total_tasks = env["gohan.job"].sudo().search_count(ctx["scope"])
    return {
        "role": _user_role_tag(env) or "tasker",
        "has_data": total_tasks > 0,
        "range": rng["key"],
        "date_range": {
            "start": rng["start"].isoformat(),
            "end": rng["end"].isoformat(),
        },
        "kpi": _build_kpi_v2(env, ctx),
        "tasks_completed_heatmap": _build_completed_heatmap(env, ctx),
        "qc_verdict_distribution": _build_qc_distribution(env, ctx),
        "leaderboard": _build_leaderboard(env, ctx),
        "timeline": _build_completion_timeline(env, ctx),
        # Parity stubs — gohan has no cost data; the pen shows no spend/burn.
        "spend_by_category": {},
        "qc_pass_rate_by_ql": {},
        "daily_burn_rate": {},
        "tasks_submitted_per_day": {},
        "qc_verdict_mix": {},
        "qc_verdicts_per_day": {},
    }


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
        """Project-scoped analytics for gohan.job, in the crowley contract.

        Returns the top-level ``{kpi, spend_by_category, qc_pass_rate_by_ql,
        daily_burn_rate, ...}`` shape the Flutter ``InternalAnalyticsTab``
        detects. ``project_id`` is required (the Analytics tab appends it plus
        ``start_date`` / ``end_date`` to the endpoint).
        """
        env = request.env
        ctx, error = _resolve_context(env, request.params or {})
        if error is not None:
            return error
        return return_Response(
            message="OK", status=200, data=_build_analytics(env, ctx)
        )
