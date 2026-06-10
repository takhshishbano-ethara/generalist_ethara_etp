"""Shared helpers for the Aurora Extension controllers.

Aurora has no notion of taskers, QC
reviewers, quality scores or handling time, so those keys are still emitted —
filled with empty / zero values — to keep the schema identical to the other
extensions.
"""

import calendar
from datetime import datetime, timedelta

from odoo import fields
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response

# Fixed PR-range buckets used as filter options on the showcase dashboard.
PR_RANGES = ["2-5", "6-10", "11-20", "21-40", "41-100"]

# Instance lifecycle states grouped for KPI / quality-tier reporting.
IN_PROGRESS_STATES = ("pending", "building", "built", "running")

# An instance counts as "completed/done" once it has resolved.
COMPLETED_STATES = ("resolved",)

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEATMAP_INTENSITY_LEVELS = 4
TIMELINE_WINDOW_DAYS = 30
SUBMISSION_WINDOW_DAYS = 30


def require_aurora_user():
    """Return a 403 Response if the caller is not an Aurora user, else None."""
    if not request.env.user.has_group("aurora.group_aurora_user"):
        return return_Response(
            message="You are not allowed to access Aurora data.",
            status=403,
        )
    return None


def user_role_tag(env):
    """Coarse role tag for the caller, mirroring talos_extension's `role`.

    Returns "admin" / "user", or None if the caller has no Aurora access.
    """
    user = env.user
    if user.has_group("aurora.group_aurora_admin"):
        return "admin"
    if user.has_group("aurora.group_aurora_user"):
        return "user"
    return None


def coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pct(part, whole):
    if whole == 0:
        return 0.0
    return round((part / whole) * 100.0, 2)


# ── Crowley-style helpers (kpi cards, ranges, activity) ──

RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"


def kpi_item(key, label, value, sub_string="", pattern="", sign=""):
    """KPI card item — matches crowley_sourcing's `_kpi_item` shape exactly."""
    return {
        "key": key,
        "label": label,
        "value": str(value),
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def resolve_range(params):
    """Resolve a {key, start, end} date range from start_date/end_date or range."""
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


def range_domain(rng):
    start_dt = datetime.combine(rng["start"], datetime.min.time())
    end_dt = datetime.combine(rng["end"], datetime.min.time()) + timedelta(days=1)
    return [("create_date", ">=", start_dt), ("create_date", "<", end_dt)]


def range_label(rng):
    if rng["key"] == "custom":
        return f"{rng['start'].isoformat()} → {rng['end'].isoformat()}"
    return {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days"}.get(
        rng["key"], ""
    )


def initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def time_ago(when):
    if not when:
        return ""
    seconds = int((fields.Datetime.now() - when).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def diff_pct(current, previous):
    if not previous:
        return 100.0 if current else 0.0
    return round(((current - previous) / previous) * 100.0, 2)


# ── Date filters (mirrors gohan/vegeta _resolve_dashboard_filters) ──

def parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def create_date_domain(start_date, end_date):
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


def resolve_dashboard_filters(params):
    start = end = month_start = month_end = None

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        start, error = parse_date(raw_start, "start_date")
        if error is not None:
            return None, error

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        end, error = parse_date(raw_end, "end_date")
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


def period_windows(start, end):
    today = fields.Datetime.now().date()
    current_end = end or today
    current_start = start or (current_end - timedelta(days=6))
    length = (current_end - current_start).days + 1
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    return current_start, current_end, previous_start, previous_end


# ── Shared section builders, computed from aurora.evaluation.instance ──

def build_status_chart(env, base_domain, filters):
    """Status distribution over aurora.evaluation.instance.status."""
    Instance = env["aurora.evaluation.instance"].sudo()
    domain = base_domain + create_date_domain(filters["start"], filters["end"])
    groups = Instance._read_group(domain, ["status"], ["__count"])
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
            "percentage": pct(counts.get(key, 0), total),
        }
        for key, label in Instance._fields["status"].selection
    ]
    return {"total_task_count": total, "status_chart": chart}


def build_team_overview(env):
    """Aurora has no team model — emit the schema with zeroed counts."""
    return {
        "total_team_size": 0,
        "role_counts": {
            "tpm": 0,
            "pl": 0,
            "qr": 0,
            "tasker": 0,
            "aire": 0,
            "swe": 0,
        },
    }


def _completed_day_counts(env, base_domain, win_start, win_end):
    """Resolved instances per create_date day within the window."""
    start_dt = datetime.combine(win_start, datetime.min.time())
    end_dt = datetime.combine(win_end, datetime.min.time()) + timedelta(days=1)
    rows = env["aurora.evaluation.instance"].sudo().search_read(
        base_domain
        + [
            ("status", "in", list(COMPLETED_STATES)),
            ("create_date", ">=", start_dt),
            ("create_date", "<", end_dt),
        ],
        ["create_date"],
    )
    counts = {}
    for row in rows:
        when = row.get("create_date")
        if when:
            day = when.date()
            counts[day] = counts.get(day, 0) + 1
    return counts


def _intensity(count, max_count):
    if not count or not max_count:
        return 0
    level = (count * HEATMAP_INTENSITY_LEVELS + max_count - 1) // max_count
    return min(level, HEATMAP_INTENSITY_LEVELS)


def _window(filters, default_days, month_aligned=False):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        if month_aligned:
            window_start = filters["start"] or window_end.replace(day=1)
        else:
            window_start = filters["start"] or (
                window_end - timedelta(days=default_days - 1)
            )
        return window_start, window_end
    if month_aligned:
        return today.replace(day=1), today
    return today - timedelta(days=default_days - 1), today


def build_heatmap(env, base_domain, filters):
    window_start, window_end = _window(filters, TIMELINE_WINDOW_DAYS, month_aligned=True)
    counts = _completed_day_counts(env, base_domain, window_start, window_end)
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
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "max_count": max_count,
        "total_completed": sum(counts.values()),
        "days": days,
    }


def build_timeline(env, base_domain, filters):
    window_start, window_end = _window(filters, TIMELINE_WINDOW_DAYS)
    counts = _completed_day_counts(env, base_domain, window_start, window_end)
    trend = []
    cursor = window_start
    while cursor <= window_end:
        trend.append({"label": cursor.isoformat(), "count": counts.get(cursor, 0)})
        cursor += timedelta(days=1)
    return {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "total_completed": sum(counts.values()),
        "trend": trend,
    }
