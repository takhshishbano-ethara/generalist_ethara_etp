from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    PL_ROLE_XMLIDS,
    TALOS_USER_ROLE_XMLIDS,
    _get_role_ids,
    _scope as _role_scope,
    _tokens_to_cost,
    _total_tokens,
    _user_has_role,
    _user_role_tag,
)

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

BUDGET_PARAM = "talos.budget_tokens"
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26

IN_FLIGHT_GOLDEN = ("generating",)
IN_FLIGHT_AUTO = ("queued", "processing")
FAILED_QC = ("failed",)
FAILED_GOLDEN = ("error",)
DONE_STATES = ("Submitted",)

QC_PASS_RATE_WINDOW_DAYS = 30
MY_ACTIVITY_WINDOW_DAYS = 30
TASKS_DONE_WINDOW_DAYS = 7
BURNED_TASKS_LIMIT = 30
RECENT_ACTIVITY_LIMIT = 8
COORDINATION_EVENTS_LIMIT = 8
HEATMAP_LEVELS = 4

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")




def _kpi_item(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _require_talos_user():
    if _user_has_role(request.env, TALOS_USER_ROLE_XMLIDS):
        return None
    return return_Response(
        message="You are not allowed to access Talos data.",
        status=403,
    )


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _date_filter_domain(params):
    domain = []
    start = end = None
    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )
    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )
    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )
    return domain, None


def _resolve_trend_weeks(params):
    raw = (params.get("weeks") or "").strip()
    if not raw:
        return DEFAULT_TREND_WEEKS, None
    if not raw.isdigit() or not (1 <= int(raw) <= MAX_TREND_WEEKS):
        return None, return_Response(
            message=f"Invalid weeks '{raw}'. Expected 1..{MAX_TREND_WEEKS}.",
            status=400,
        )
    return int(raw), None


def _budget_tokens(env):
    raw = env["ir.config_parameter"].sudo().get_param(BUDGET_PARAM, "")
    try:
        return float(raw) if raw else 0.0
    except (ValueError, TypeError):
        return 0.0


def _today_bounds(env):
    today = fields.Datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    return start, start + timedelta(days=1)


def _sum_tokens_for(env, gen_scope):
    records = env["talos.talos"].sudo().search(gen_scope)
    return sum(_total_tokens(r) for r in records)


def _compute_kpi(env, gen_scope, project=None):
    Talos = env["talos.talos"].sudo()

    total_tasks = Talos.search_count(gen_scope)
    draft = Talos.search_count(gen_scope + [("task_status", "=", "NotSubmitted")])
    in_flight = Talos.search_count(
        gen_scope
        + [
            "|",
            ("golden_status", "in", list(IN_FLIGHT_GOLDEN)),
            ("auto_process_status", "in", list(IN_FLIGHT_AUTO)),
        ]
    )
    active = draft + in_flight
    tasks_done = Talos.search_count(
        gen_scope + [("task_status", "in", list(DONE_STATES))]
    )

    spent = round(_tokens_to_cost(_sum_tokens_for(env, gen_scope)), 2)
    budget = round(_tokens_to_cost(_budget_tokens(env)), 2)
    if budget > 0:
        burned_sub = (
            f"of ${budget:,.2f} budget · ${max(budget - spent, 0.0):,.2f} remaining"
        )
        burned_pattern = "down" if spent > budget else ""
    else:
        burned_sub = "Lifetime spend · no budget configured"
        burned_pattern = ""

    approved = Talos.search_count(gen_scope + [("qc_status", "=", "passed")])
    rejected = Talos.search_count(gen_scope + [("qc_status", "=", "failed")])
    reviewed = approved + rejected
    approval_rate = _pct(approved, reviewed)

    if project:
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
        members_total = len(team_employees)
        breakdown_parts = [
            f"{c} {label}"
            for c, label in (
                (len(tpm_employees), "TPM"),
                (len(pl_employees), "PL"),
                (len(qr_employees), "QR"),
                (len(tasker_employees), "Tasker"),
                (len(aire_employees), "AIRE"),
                (len(swe_employees), "SWE"),
            )
            if c
        ]
        members_sub_string = (
            " · ".join(breakdown_parts) if breakdown_parts else "No assigned roles"
        )
    else:
        owner_rows = Talos.read_group(
            gen_scope, fields=["user_id"], groupby=["user_id"], lazy=False
        )
        owner_ids = [row["user_id"][0] for row in owner_rows if row.get("user_id")]
        members_total = len(owner_ids)
        members_sub_string = ""

    today_start, today_end = _today_bounds(env)
    yesterday_start = today_start - timedelta(days=1)
    window_start = today_start - timedelta(days=QC_PASS_RATE_WINDOW_DAYS - 1)

    approved_today = Talos.search_count(
        gen_scope
        + [
            ("qc_status", "=", "passed"),
            ("write_date", ">=", today_start),
            ("write_date", "<", today_end),
        ]
    )
    rejected_today = Talos.search_count(
        gen_scope
        + [
            ("qc_status", "=", "failed"),
            ("write_date", ">=", today_start),
            ("write_date", "<", today_end),
        ]
    )
    approved_yesterday = Talos.search_count(
        gen_scope
        + [
            ("qc_status", "=", "passed"),
            ("write_date", ">=", yesterday_start),
            ("write_date", "<", today_start),
        ]
    )
    today_pass_rate = _pct(approved_today, approved_today + rejected_today)
    today_delta = approved_today - approved_yesterday

    approved_30 = Talos.search_count(
        gen_scope
        + [
            ("qc_status", "=", "passed"),
            ("write_date", ">=", window_start),
        ]
    )
    rejected_30 = Talos.search_count(
        gen_scope
        + [
            ("qc_status", "=", "failed"),
            ("write_date", ">=", window_start),
        ]
    )
    reviewed_30 = approved_30 + rejected_30
    qc_pass_rate = _pct(approved_30, reviewed_30)

    items = [
        _kpi_item(
            "total_burned",
            "Total Burned",
            spent,
            sub_string=burned_sub,
            pattern=burned_pattern,
            sign="-" if burned_pattern == "down" else "",
        ),
        _kpi_item(
            "active_tasks",
            "Active Tasks",
            f"{active}/{total_tasks}",
            sub_string=f"{draft} unstarted · {in_flight} in progress",
        ),
        _kpi_item(
            "approval_rate",
            "Approval Rate",
            approval_rate,
            sub_string=f"{approved} approved of {reviewed} reviewed",
        ),
        _kpi_item(
            "team_members",
            "Team Members",
            members_total,
            sub_string=members_sub_string,
        ),
        _kpi_item(
            "approved_today",
            "Approved Today",
            approved_today,
            sub_string=(
                f"{today_pass_rate}% pass rate · "
                f"{'+' if today_delta > 0 else ''}{today_delta} vs yesterday"
            ),
            pattern="up" if today_delta > 0 else ("down" if today_delta < 0 else ""),
            sign="+" if today_delta > 0 else ("-" if today_delta < 0 else ""),
        ),
        _kpi_item(
            "qc_pass_rate",
            "QC Pass Rate",
            qc_pass_rate,
            sub_string=(
                f"{approved_30} of {reviewed_30} reviewed · "
                f"last {QC_PASS_RATE_WINDOW_DAYS}d"
                if reviewed_30
                else f"No reviews in last {QC_PASS_RATE_WINDOW_DAYS}d"
            ),
        ),
        _kpi_item(
            "total_tasks_done",
            "Total Tasks Done",
            tasks_done,
            sub_string=f"{_pct(tasks_done, total_tasks)}% of {total_tasks} tasks",
        ),
    ]
    return {"count": str(len(items)), "items": items}


STAGE_BUCKETS = (
    (
        "s1_draft",
        "S1 Draft",
        [("task_status", "=", "NotSubmitted")],
    ),
    (
        "s2_qc_approved",
        "S2 QC'd · Approved",
        [("qc_status", "=", "passed")],
    ),
    (
        "s3_delivered",
        "S3 Delivered",
        [
            ("task_status", "=", "Submitted"),
            ("qc_status", "=", "pending"),
        ],
    ),
    (
        "rejected_failed",
        "Rejected / Failed",
        [
            "|",
            ("qc_status", "=", "failed"),
            ("golden_status", "=", "error"),
        ],
    ),
)


def _compute_task_progress(env, gen_scope):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(gen_scope)
    items = []
    for key, label, bucket_domain in STAGE_BUCKETS:
        count = Talos.search_count(gen_scope + bucket_domain)
        items.append({
            "key": key,
            "label": label,
            "value": count,
            "percentage": _pct(count, total),
        })
    return {
        "label": "Task Progress (by Stage)",
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _compute_approved_per_week(env, gen_scope, weeks):
    Talos = env["talos.talos"].sudo()
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)

    window_start = current_week_start - timedelta(weeks=weeks)
    window_start_dt = datetime.combine(window_start, datetime.min.time())

    approvals = Talos.search_read(
        gen_scope
        + [
            ("qc_status", "=", "passed"),
            ("write_date", ">=", window_start_dt),
        ],
        fields=["write_date"],
    )
    counts = {}
    for row in approvals:
        when = row.get("write_date")
        if not when:
            continue
        day = _week_start(when.date())
        counts[day] = counts.get(day, 0) + 1

    items = []
    prev_count = counts.get(window_start, 0)
    for offset in range(weeks - 1, -1, -1):
        start = current_week_start - timedelta(weeks=offset)
        end = start + timedelta(days=6)
        count = counts.get(start, 0)
        delta = count - prev_count
        items.append({
            "key": f"w{start.isocalendar()[1]}",
            "label": f"W{start.isocalendar()[1]}",
            "value": count,
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "delta_vs_prev_week": delta,
            "pattern": "up" if delta > 0 else ("down" if delta < 0 else ""),
            "sign": "+" if delta > 0 else ("-" if delta < 0 else ""),
        })
        prev_count = count

    return {
        "label": "Tasks Approved per Week",
        "sub_string": f"Trailing {weeks} weeks",
        "total": sum(item["value"] for item in items),
        "count": str(len(items)),
        "items": items,
    }


def _intensity(count, max_count):
    if not count or not max_count:
        return 0
    return min((count * HEATMAP_LEVELS + max_count - 1) // max_count, HEATMAP_LEVELS)


def _time_ago(when):
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


def _done_per_day(env, gen_scope, start_date, end_date, extra=None):
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.min.time()) + timedelta(days=1)
    records = env["talos.talos"].sudo().search(
        gen_scope
        + [
            ("task_status", "in", list(DONE_STATES)),
            ("write_date", ">=", start_dt),
            ("write_date", "<", end_dt),
        ]
        + (extra or [])
    )
    counts = {}
    for rec in records:
        when = rec.write_date
        if not when:
            continue
        day = when.date()
        counts[day] = counts.get(day, 0) + 1
    return counts


def _compute_my_activity(env, gen_scope):
    today = fields.Datetime.now().date()
    start = today - timedelta(days=MY_ACTIVITY_WINDOW_DAYS - 1)
    counts = _done_per_day(env, gen_scope, start, today)
    max_count = max(counts.values()) if counts else 0

    days = []
    streak = longest_streak = active_days = 0
    cursor = start
    while cursor <= today:
        count = counts.get(cursor, 0)
        if count > 0:
            streak += 1
            active_days += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0
        days.append({
            "date": cursor.isoformat(),
            "weekday": cursor.weekday(),
            "weekday_label": WEEKDAY_LABELS[cursor.weekday()],
            "count": count,
            "intensity": _intensity(count, max_count),
        })
        cursor += timedelta(days=1)

    total = sum(counts.values())
    prior_start = start - timedelta(days=MY_ACTIVITY_WINDOW_DAYS)
    prior_total = sum(
        _done_per_day(
            env, gen_scope, prior_start, start - timedelta(days=1)
        ).values()
    )

    return {
        "label": "My Activity",
        "sub_string": (
            f"Last {MY_ACTIVITY_WINDOW_DAYS} days · Tasks completed per day"
        ),
        "window": {"start": start.isoformat(), "end": today.isoformat()},
        "max_count": max_count,
        "days": days,
        "summary": {
            "total_tasks": total,
            "total_tasks_delta": total - prior_total,
            "avg_per_day": round(total / MY_ACTIVITY_WINDOW_DAYS, 1),
            "longest_streak": longest_streak,
            "active_days": active_days,
            "total_days": MY_ACTIVITY_WINDOW_DAYS,
        },
    }


def _compute_tasks_done_chart(env, gen_scope):
    today = fields.Datetime.now().date()
    start = today - timedelta(days=TASKS_DONE_WINDOW_DAYS - 1)
    done_counts = _done_per_day(env, gen_scope, start, today)
    approved_counts = _done_per_day(
        env, gen_scope, start, today, extra=[("qc_status", "=", "passed")]
    )

    items = []
    cursor = start
    while cursor <= today:
        items.append({
            "date": cursor.isoformat(),
            "label": WEEKDAY_LABELS[cursor.weekday()],
            "value": done_counts.get(cursor, 0),
            "approved": approved_counts.get(cursor, 0),
        })
        cursor += timedelta(days=1)

    total = sum(item["value"] for item in items)
    return {
        "label": "Tasks Done",
        "sub_string": f"Last {TASKS_DONE_WINDOW_DAYS} days",
        "window": {"start": start.isoformat(), "end": today.isoformat()},
        "total": total,
        "average_per_day": round(total / TASKS_DONE_WINDOW_DAYS, 1),
        "count": str(len(items)),
        "items": items,
    }


def _compute_burned_amount_chart(env, gen_scope):
    Talos = env["talos.talos"].sudo()
    candidates = Talos.search(
        gen_scope,
        order="write_date desc, id desc",
        limit=BURNED_TASKS_LIMIT * 4,
    )
    with_tokens = [(r, _total_tokens(r)) for r in candidates]
    with_tokens = [pair for pair in with_tokens if pair[1] > 0][:BURNED_TASKS_LIMIT]
    ordered = list(reversed(with_tokens))

    items = []
    for index, (rec, tokens) in enumerate(ordered, start=1):
        items.append({
            "seq": index,
            "code": rec.task_id or "",
            "cost": round(_tokens_to_cost(tokens), 4),
            "tokens": tokens,
            "duration_seconds": 0,
            "model": "",
            "completed_at": (
                rec.write_date.isoformat() if rec.write_date else ""
            ),
        })

    total = round(sum(item["cost"] for item in items), 4)
    return {
        "label": "Burned Amount",
        "sub_string": f"Last {BURNED_TASKS_LIMIT} tasks (tokens)",
        "total": total,
        "average_per_task": round(total / len(items), 4) if items else 0.0,
        "count": str(len(items)),
        "items": items,
    }


def _compute_recent_activity(env, gen_scope):
    Talos = env["talos.talos"].sudo()
    category_labels = dict(Talos._fields["task_type"].selection)
    records = Talos.search(
        gen_scope, order="write_date desc, id desc", limit=RECENT_ACTIVITY_LIMIT
    )

    items = []
    for rec in records:
        if rec.qc_status == "passed":
            action = "approved"
        elif rec.qc_status == "failed":
            action = "rejected"
        elif rec.task_status == "Submitted":
            action = "completed"
        elif rec.golden_status == "error":
            action = "failed"
        else:
            action = "updated"
        when = rec.write_date
        items.append({
            "actor_id": rec.user_id.id,
            "actor_name": rec.user_id.name or "",
            "action": action,
            "task_code": rec.task_id or "",
            "category": category_labels.get(rec.task_type, "") if rec.task_type else "",
            "timestamp": when.isoformat() if when else "",
            "time_ago": _time_ago(when),
        })

    return {
        "label": "Recent Activity",
        "count": str(len(items)),
        "items": items,
    }


def _compute_coordination_events(env, gen_scope):
    label = "TPM Activity — Coordination Events"
    Talos = env["talos.talos"].sudo()
    records = Talos.search(gen_scope)
    if not records:
        return {"label": label, "count": "0", "items": []}

    code_by_id = {rec.id: (rec.task_id or "") for rec in records}
    user_field = env["ir.model.fields"]._get("talos.talos", "user_id")
    if not user_field:
        return {"label": label, "count": "0", "items": []}

    messages = env["mail.message"].sudo().search(
        [
            ("model", "=", "talos.talos"),
            ("res_id", "in", records.ids),
            ("tracking_value_ids.field_id", "=", user_field.id),
        ],
        order="date desc, id desc",
        limit=COORDINATION_EVENTS_LIMIT,
    )

    items = []
    for message in messages:
        tracking = message.tracking_value_ids.filtered(
            lambda t: t.field_id.id == user_field.id
        )[:1]
        if not tracking:
            continue
        items.append({
            "actor_id": message.author_id.id,
            "actor_name": message.author_id.name or "",
            "action": "reassigned",
            "task_code": code_by_id.get(message.res_id, ""),
            "from_user": tracking.old_value_char or "",
            "to_user": tracking.new_value_char or "",
            "timestamp": message.date.isoformat() if message.date else "",
            "time_ago": _time_ago(message.date),
        })

    return {"label": label, "count": str(len(items)), "items": items}


class TalosDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_dashboard_overview(self, **kwargs):
        guard = _require_talos_user()
        if guard is not None:
            return guard

        env = request.env
        params = request.params or {}

        date_domain, error = _date_filter_domain(params)
        if error is not None:
            return error
        weeks, error = _resolve_trend_weeks(params)
        if error is not None:
            return error
        project, error = _resolve_project(env, params)
        if error is not None:
            return error

        role_tag, gen_domain, _projects = _role_scope(env)
        gen_scope = gen_domain + date_domain

        return return_Response(
            message="OK",
            status=200,
            data={
                "overview": {
                    "role": role_tag or "tasker",
                    "kpi": _compute_kpi(env, gen_scope, project=project),
                    "task_progress": _compute_task_progress(env, gen_scope),
                    "approved_per_week": _compute_approved_per_week(
                        env, gen_domain, weeks
                    ),
                    "recent_activity": _compute_recent_activity(env, gen_scope),
                    "coordination_events": _compute_coordination_events(
                        env, gen_scope
                    ),
                    "my_activity": _compute_my_activity(env, gen_scope),
                    "tasks_done_chart": _compute_tasks_done_chart(env, gen_scope),
                    "burned_amount_chart": _compute_burned_amount_chart(
                        env, gen_scope
                    ),
                },
            },
        )
