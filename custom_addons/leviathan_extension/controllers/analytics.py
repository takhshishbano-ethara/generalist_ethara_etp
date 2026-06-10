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
    LEVIATHAN_FULL_ACCESS_ROLE_XMLIDS,
    LEVIATHAN_PL_ROLE_XMLIDS,
    LEVIATHAN_QR_ROLE_XMLIDS,
    LEVIATHAN_TASKER_ROLE_XMLIDS,
    _get_role_ids,
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
FAILED_STATES = ("failed", "discarded")
HEATMAP_INTENSITY_LEVELS = 4

RANGE_KEYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"

STAGE_BUCKETS = (
    ("created", "Created", ("draft", "not_assigned"), "muted"),
    ("scraping", "Scraping", ("extracting",), "info"),
    ("generated", "Generated", ("extracted", "generating", "generated"), "warn"),
    ("in_review", "In Review", ("scoring", "scored", "qc_running"), "primary"),
    ("submitted", "Submitted", ("done", "submitted"), "success"),
    ("flagged", "Flagged", ("failed", "discarded", "cancelled"), "danger"),
)

COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")


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


def _color(idx):
    return COLOR_TOKENS[idx % len(COLOR_TOKENS)]


def _time_ago(when):
    if not when:
        return ""
    now = fields.Datetime.now()
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    return f"{int(days)}d ago"


def _format_month_day(d):
    # strftime("%-d") is Linux-only; build the day manually for cross-platform output.
    return f"{d.strftime('%b')} {d.day}"


def _kpi_item(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": str(value),
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _kpi(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _user_role_tag(env):
    user_role = env.user.user_role
    if not user_role:
        return None
    rid = user_role.id
    for tag, xmlids in (
        ("full", LEVIATHAN_FULL_ACCESS_ROLE_XMLIDS),
        ("pl", LEVIATHAN_PL_ROLE_XMLIDS),
        ("qr", LEVIATHAN_QR_ROLE_XMLIDS),
        ("tasker", LEVIATHAN_TASKER_ROLE_XMLIDS),
    ):
        if rid in _get_role_ids(env, xmlids):
            return tag
    return None


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


def _resolve_range(params):
    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    today = fields.Datetime.now().date()
    if raw_start or raw_end:
        start_date = end_date = None
        if raw_start:
            try:
                start_date = datetime.strptime(raw_start, "%Y-%m-%d").date()
            except ValueError:
                return None, return_Response(
                    message=(
                        f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD."
                    ),
                    status=400,
                )
        if raw_end:
            try:
                end_date = datetime.strptime(raw_end, "%Y-%m-%d").date()
            except ValueError:
                return None, return_Response(
                    message=(
                        f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD."
                    ),
                    status=400,
                )
        end_date = end_date or today
        start_date = start_date or (end_date - timedelta(days=29))
        if start_date > end_date:
            return None, return_Response(
                message=(
                    "Invalid date range: start_date must be on or before end_date."
                ),
                status=400,
            )
        return {"key": "custom", "start": start_date, "end": end_date}, None

    raw_range = (params.get("range") or DEFAULT_RANGE).strip().lower()
    if raw_range not in RANGE_KEYS:
        return None, return_Response(
            message=(
                f"Invalid range '{raw_range}'. Allowed: "
                f"{', '.join(sorted(RANGE_KEYS))}, or supply start_date/end_date."
            ),
            status=400,
        )
    days = RANGE_KEYS[raw_range]
    return {
        "key": raw_range,
        "start": today - timedelta(days=days - 1),
        "end": today,
    }, None


def _range_label(rng):
    if rng["key"] == "custom":
        return f"{rng['start'].isoformat()} → {rng['end'].isoformat()}"
    return f"Last {RANGE_KEYS[rng['key']]} days"


def _resolve_weeks(params, default=8, maximum=26):
    raw = (params.get("weeks") or "").strip()
    if not raw:
        return default, None
    if not raw.isdigit():
        return None, return_Response(
            message=f"Invalid weeks '{raw}'. Expected a positive integer.",
            status=400,
        )
    weeks = int(raw)
    if weeks < 1 or weeks > maximum:
        return None, return_Response(
            message=f"weeks must be between 1 and {maximum}.",
            status=400,
        )
    return weeks, None


def _submission_events(env, job_domain):
    # leviathan.job has no submitted_at field; recover the moment from the
    # mail.tracking.value row written when `state` flipped to 'submitted'.
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


def _team_breakdown_sub(role_counts):
    parts = []
    for key, label in (
        ("tpm", "TPM"),
        ("pl", "PL"),
        ("qr", "QC"),
        ("tasker", "Tasker"),
    ):
        n = role_counts.get(key, 0)
        if n:
            parts.append(f"{n} {label}")
    return " · ".join(parts) if parts else "No assigned roles"


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


def _build_overview_kpi(env, scope, project_id):
    Job = env["leviathan.job"].sudo()
    today = fields.Datetime.now().date()
    this_week_start = datetime.combine(_week_start(today), datetime.min.time())

    total = Job.search_count(scope)
    submitted_count = Job.search_count(
        scope + [("state", "in", list(COMPLETED_STATES))]
    )
    failed_count = Job.search_count(
        scope + [("state", "in", list(FAILED_STATES))]
    )
    this_week_added = Job.search_count(
        scope + [("create_date", ">=", this_week_start)]
    )

    submitted_pct = _pct(submitted_count, total)
    team = _team_overview(env, project_id)
    team_sub = _team_breakdown_sub(team["role_counts"])

    items = [
        _kpi_item(
            "total_tasks",
            "Total Tasks",
            total,
            sub_string=f"{this_week_added} added this week",
        ),
        _kpi_item(
            "submitted",
            "Submitted",
            f"{int(submitted_pct)}%",
            sub_string=f"{submitted_count} of {total} submitted",
            pattern="up" if submitted_pct >= 50 else "down",
            sign="+" if submitted_pct >= 50 else "-",
        ),
        _kpi_item(
            "failed",
            "Failed",
            failed_count,
            sub_string="Scrape + generation failures",
            pattern="badge",
        ),
        _kpi_item(
            "team_size",
            "Team Size",
            team["total_team_size"],
            sub_string=team_sub,
        ),
    ]
    return {"count": str(len(items)), "items": items}



def _build_overview_task_progress(env, scope):
    Job = env["leviathan.job"].sudo()
    total = Job.search_count(scope) or 0
    items = []
    for key, label, states, color_token in STAGE_BUCKETS:
        count = Job.search_count(scope + [("state", "in", list(states))])
        items.append({
            "key": key,
            "label": label,
            "value": count,
            "percentage": _pct(count, total),
            "color_token": color_token,
        })
    return {
        "label": "Task Progress",
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _build_overview_submission_trend(env, scope, weeks=8):
    events = _submission_events(env, scope)
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)
    earliest_week_start = current_week_start - timedelta(days=(weeks - 1) * 7)
    window_start_dt = datetime.combine(earliest_week_start, datetime.min.time())
    window_end_dt = datetime.combine(
        current_week_start + timedelta(days=7), datetime.min.time()
    )

    buckets = [0] * weeks
    for when in events.values():
        if not (window_start_dt <= when < window_end_dt):
            continue
        idx = (when.date() - earliest_week_start).days // 7
        if 0 <= idx < weeks:
            buckets[idx] += 1

    items = []
    total = 0
    for i in range(weeks):
        week_start = earliest_week_start + timedelta(days=i * 7)
        week_end = week_start + timedelta(days=6)
        count = buckets[i]
        total += count
        items.append({
            "key": f"w{week_start.isocalendar()[1]}",
            "label": _format_month_day(week_start),
            "value": count,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
        })

    return {
        "title": "Submission Trend",
        "sub_title": "Tasks submitted per week · project-wide",
        "type": "bar",
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _classify_job_action(state):
    if state == "submitted":
        return "submitted"
    if state == "done":
        return "completed"
    if state == "failed":
        return "failed"
    if state == "discarded":
        return "discarded"
    if state == "qc_running":
        return "qc_review"
    if state in ("scoring", "scored"):
        return "qc_scoring"
    if state in ("generating", "generated", "extracted"):
        return "generation_complete"
    if state == "extracting":
        return "scraping"
    if state in ("draft", "not_assigned"):
        return "created"
    return "updated"


def _build_overview_recent_activity(env, scope, limit=8):
    Job = env["leviathan.job"].sudo()
    jobs = Job.search(scope, order="write_date desc, id desc", limit=limit)
    items = []
    for job in jobs:
        actor = job.user_id
        when = job.write_date
        category = ""
        if job.category_id:
            category = job.category_id.name or ""
        if not category:
            category = job.site_name or ""
        items.append({
            "actor_id": actor.id if actor else 0,
            "actor_name": actor.name if actor else "",
            "action": _classify_job_action(job.state),
            "task_code": job.name or "",
            "category": category,
            "timestamp": when.isoformat() if when else "",
            "time_ago": _time_ago(when),
        })
    return {
        "label": "Recent Activity",
        "count": str(len(items)),
        "items": items,
    }


def _build_analytics_kpi(env, scope, rng, project_id):
    Job = env["leviathan.job"].sudo()
    range_start_dt = datetime.combine(rng["start"], datetime.min.time())
    range_end_dt = datetime.combine(rng["end"], datetime.min.time()) + timedelta(days=1)
    range_domain = [
        ("create_date", ">=", range_start_dt),
        ("create_date", "<", range_end_dt),
    ]
    in_range = scope + range_domain

    total_tasks = Job.search_count(in_range)

    aht_domain = (
        in_range
        + [
            ("state", "in", list(COMPLETED_STATES)),
            ("duration_seconds", ">", 0),
        ]
    )
    aht_rows = Job.read_group(
        aht_domain, fields=["duration_seconds:avg"], groupby=[]
    )
    aht_seconds = (aht_rows[0].get("duration_seconds") if aht_rows else 0.0) or 0.0
    aht_minutes = round(aht_seconds / 60.0, 1) if aht_seconds else 0.0

    urls_added = Job.search_count(in_range + [("url", "!=", False)])
    rejected = Job.search_count(in_range + [("state", "=", "discarded")])

    flagged = Job.search_count(in_range + [("state", "in", list(FAILED_STATES))])
    failed_only = Job.search_count(in_range + [("state", "=", "failed")])
    qc_flagged = Job.search_count(
        in_range + [("qc_verdict", "=", "not_shippable")]
    )

    team = _team_overview(env, project_id)
    team_sub = _team_breakdown_sub(team["role_counts"])

    open_blockers = Job.search_count(
        scope
        + [
            ("watchdog_retry_count", ">", 0),
            (
                "state",
                "in",
                ["extracting", "generating", "scoring", "qc_running"],
            ),
        ]
    )

    aht_pattern = ""
    aht_sign = ""
    aht_value = "—"
    if aht_minutes:
        aht_value = f"{int(round(aht_minutes))}m"
        if aht_minutes < 45:
            aht_pattern, aht_sign = "down", "-"
        else:
            aht_pattern, aht_sign = "up", "+"

    items = [
        _kpi(
            "total_tasks",
            "Total Tasks",
            total_tasks,
            sub_string="Scraping tasks created",
        ),
        _kpi(
            "avg_handling_time",
            "Avg Handling Time",
            aht_value,
            sub_string="Target < 45m",
            pattern=aht_pattern,
            sign=aht_sign,
        ),
        _kpi(
            "total_urls_added",
            "Total URLs Added",
            urls_added,
            sub_string=f"{rejected} rejected at validation",
        ),
        _kpi(
            "flagged_urls",
            "Flagged URLs",
            flagged,
            sub_string=f"{failed_only} failed · {qc_flagged} QC-flagged",
            pattern="up" if flagged else "",
            sign="+" if flagged else "",
        ),
        _kpi(
            "team_members",
            "Team Members",
            team["total_team_size"],
            sub_string=team_sub,
        ),
        _kpi(
            "open_blockers",
            "Open Blockers",
            open_blockers,
            sub_string="Pipeline retries in progress",
            pattern="up" if open_blockers else "",
            sign="+" if open_blockers else "",
        ),
    ]
    return {"count": len(items), "items": items}


def _build_analytics_heatmap(env, scope, rng):
    counts = _completed_day_counts(env, scope, rng["start"], rng["end"])
    max_count = max(counts.values()) if counts else 0
    days = []
    total = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        count = counts.get(cursor, 0)
        total += count
        days.append({
            "date": cursor.isoformat(),
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
        "window": {
            "start": rng["start"].isoformat(),
            "end": rng["end"].isoformat(),
        },
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

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error

        rng, error = _resolve_range(params)
        if error is not None:
            return error

        scope = _job_scope_domain()
        analytics = {
            "role": _user_role_tag(env) or "tasker",
            "kpi": _build_analytics_kpi(env, scope, rng, project_id),
            "spend_by_category": {},
            "qc_pass_rate_by_ql": {},
            "daily_burn_rate": {},
            "tasks_submitted_per_day": {},
            "qc_verdict_mix": {},
            "qc_verdicts_per_day": {},
            "tasks_completed_heatmap": _build_analytics_heatmap(env, scope, rng),
        }
        return return_Response(message="OK", status=200, data=analytics)

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

        project_id, error = _resolve_project_id(env, params)
        if error is not None:
            return error

        weeks, error = _resolve_weeks(params, default=8)
        if error is not None:
            return error

        scope = _job_scope_domain()
        overview = {
            "role": _user_role_tag(env) or "tasker",
            "kpi": _build_overview_kpi(env, scope, project_id),
            "budget": {},
            "burn_rate": {},
            "accepted_per_day": {},
            "task_progress": _build_overview_task_progress(env, scope),
            "approved_per_week": {},
            "recent_activity": _build_overview_recent_activity(env, scope),
            "coordination_events": {},
            "tasks_done_chart": {},
            "burned_amount_chart": {},
            "my_activity": {},
            "submission_trend": _build_overview_submission_trend(
                env, scope, weeks=weeks
            ),
        }
        return return_Response(
            message="OK", status=200, data={"overview": overview}
        )
