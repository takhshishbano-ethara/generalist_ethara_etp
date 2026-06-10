from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _get_role_ids,
    _scope as _role_scope,
    _user_role_tag,
)

TPM_ROLE_XMLIDS = ("api_auth_gateway.role_tpm_technical",)

# Which dashboard view each role sees (drives role-specific blocks).
# CTO/PL share the "cto_pl" view; TPM has its own (coordination events);
# QL/QC and everyone else get the individual "ql" view.
KPI_KEYS_BY_VIEW = {
    "cto_pl": ("total_burned", "active_tasks", "approval_rate", "team_members"),
    "tpm": ("total_burned", "active_tasks", "approval_rate", "team_members"),
    "ql": ("approved_today", "qc_pass_rate", "total_tasks_done", "total_burned"),
}

# Which section blocks each view's page actually shows. Every section KEY is
# always present in the response; sections NOT in a view's set are returned
# blank ({}) for that role rather than computed — the key belongs to the
# schema, but the data only fills in for the roles whose page displays it.
SECTIONS_BY_VIEW = {
    "cto_pl": ("task_progress", "approved_per_week", "recent_activity"),
    "tpm": ("task_progress", "approved_per_week", "coordination_events"),
    "ql": ("tasks_done_chart", "burned_amount_chart", "my_activity"),
}


def _overview_view(env, role_tag):
    if role_tag in ("full", "pl"):
        role = env.user.user_role
        tpm_ids = _get_role_ids(env, TPM_ROLE_XMLIDS)
        if role and role.id in tpm_ids:
            return "tpm"
        return "cto_pl"
    return "ql"

BUDGET_PARAM = "crowley.budget_usd"
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26

IN_FLIGHT_STATES = ("queued", "submitting", "processing", "downloading")
FAILED_STATES = ("failed", "cancelled")
DONE_STATES = ("done",)

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
        "value": str(value),
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


def _require_crowley_user():
    if not request.env.user.has_group("crowley.group_crowley_user"):
        return return_Response(
            message="You are not allowed to access Crowley data.",
            status=403,
        )
    return None


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


def _budget_usd(env):
    raw = env["ir.config_parameter"].sudo().get_param(BUDGET_PARAM, "")
    try:
        return float(raw) if raw else 0.0
    except (ValueError, TypeError):
        return 0.0


def _today_bounds(env):
    today = fields.Datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    return start, start + timedelta(days=1)


def _approved_reviewed(Attempt, attempt_scope, window_start=None):
    extra = []
    if window_start is not None:
        extra = [("reviewed_at", ">=", window_start)]
    approved = Attempt.search_count(
        attempt_scope + [("review_state", "=", "approved")] + extra
    )
    rejected = Attempt.search_count(
        attempt_scope + [("review_state", "=", "rejected")] + extra
    )
    return approved, rejected


def _compute_kpi(env, gen_scope, attempt_scope):
    Generation = env["crowley.generation"].sudo()
    Attempt = env["crowley.attempt"].sudo()

    total_tasks = Generation.search_count(gen_scope)
    draft = Generation.search_count(gen_scope + [("state", "=", "draft")])
    in_flight = Generation.search_count(
        gen_scope + [("state", "in", list(IN_FLIGHT_STATES))]
    )
    active = draft + in_flight
    tasks_done = Generation.search_count(
        gen_scope + [("state", "in", list(DONE_STATES))]
    )

    cost_rows = Attempt.read_group(
        attempt_scope + [("cost_usd", ">", 0)],
        fields=["cost_usd:sum"],
        groupby=[],
    )
    spent = round((cost_rows[0].get("cost_usd") if cost_rows else 0.0) or 0.0, 2)
    budget = _budget_usd(env)
    if budget > 0:
        burned_sub = (
            f"of ${budget:,.2f} budget · ${max(budget - spent, 0.0):,.2f} remaining"
        )
        burned_pattern = "down" if spent > budget else ""
    else:
        burned_sub = "Lifetime spend · no budget configured"
        burned_pattern = ""

    approved = Attempt.search_count(
        attempt_scope + [("review_state", "=", "approved")]
    )
    rejected = Attempt.search_count(
        attempt_scope + [("review_state", "=", "rejected")]
    )
    reviewed = approved + rejected
    approval_rate = _pct(approved, reviewed)

    owner_rows = Generation.read_group(
        gen_scope, fields=["user_id"], groupby=["user_id"], lazy=False
    )
    owner_ids = [row["user_id"][0] for row in owner_rows if row.get("user_id")]
    members = env["res.users"].sudo().browse(owner_ids)
    manager_count = len(
        members.filtered(lambda u: u.has_group("crowley.group_crowley_manager"))
    )

    today_start, today_end = _today_bounds(env)
    yesterday_start = today_start - timedelta(days=1)
    window_start = today_start - timedelta(days=QC_PASS_RATE_WINDOW_DAYS - 1)

    approved_today = Attempt.search_count(
        attempt_scope
        + [
            ("review_state", "=", "approved"),
            ("reviewed_at", ">=", today_start),
            ("reviewed_at", "<", today_end),
        ]
    )
    rejected_today = Attempt.search_count(
        attempt_scope
        + [
            ("review_state", "=", "rejected"),
            ("reviewed_at", ">=", today_start),
            ("reviewed_at", "<", today_end),
        ]
    )
    approved_yesterday = Attempt.search_count(
        attempt_scope
        + [
            ("review_state", "=", "approved"),
            ("reviewed_at", ">=", yesterday_start),
            ("reviewed_at", "<", today_start),
        ]
    )
    today_pass_rate = _pct(approved_today, approved_today + rejected_today)
    today_delta = approved_today - approved_yesterday

    approved_30, rejected_30 = _approved_reviewed(
        Attempt, attempt_scope, window_start=window_start
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
            len(members),
            sub_string=(
                f"{manager_count} managers · {len(members) - manager_count} users"
            ),
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
        ["|", ("state", "=", "draft"), ("state", "in", list(IN_FLIGHT_STATES))],
    ),
    (
        "s2_qc_approved",
        "S2 QC'd · Approved",
        [("state", "=", "done"), ("review_state", "=", "approved")],
    ),
    (
        "s3_delivered",
        "S3 Delivered",
        [
            ("state", "=", "done"),
            ("review_state", "!=", "approved"),
            ("review_state", "!=", "rejected"),
        ],
    ),
    (
        "rejected_failed",
        "Rejected / Failed",
        [
            "|",
            ("state", "in", list(FAILED_STATES)),
            "&",
            ("state", "=", "done"),
            ("review_state", "=", "rejected"),
        ],
    ),
)


def _compute_task_progress(env, gen_scope):
    Generation = env["crowley.generation"].sudo()
    total = Generation.search_count(gen_scope)
    items = []
    for key, label, bucket_domain in STAGE_BUCKETS:
        count = Generation.search_count(gen_scope + bucket_domain)
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


def _compute_approved_per_week(env, attempt_scope, weeks):
    Attempt = env["crowley.attempt"].sudo()
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)

    window_start = current_week_start - timedelta(weeks=weeks)
    window_start_dt = datetime.combine(window_start, datetime.min.time())

    approvals = Attempt.search_read(
        attempt_scope
        + [
            ("review_state", "=", "approved"),
            ("reviewed_at", ">=", window_start_dt),
        ],
        fields=["reviewed_at"],
    )
    counts = {}
    for row in approvals:
        when = row.get("reviewed_at")
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
    rows = env["crowley.generation"].sudo().search_read(
        gen_scope
        + [
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", start_dt),
            ("completed_at", "<", end_dt),
        ]
        + (extra or []),
        fields=["completed_at"],
    )
    counts = {}
    for row in rows:
        when = row.get("completed_at")
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
        env, gen_scope, start, today, extra=[("review_state", "=", "approved")]
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
    Generation = env["crowley.generation"].sudo()
    gens = Generation.search(
        gen_scope + [("total_cost_usd", ">", 0)],
        order="completed_at desc, id desc",
        limit=BURNED_TASKS_LIMIT,
    )
    ordered = list(reversed(gens))
    items = []
    for index, gen in enumerate(ordered, start=1):
        items.append({
            "seq": index,
            "code": gen.name or "",
            "cost": round(gen.total_cost_usd or 0.0, 4),
            "tokens": gen.tokens_used or 0,
            "duration_seconds": round(gen.duration_seconds or 0.0),
            "model": gen.model_name or "",
            "completed_at": (
                gen.completed_at.isoformat() if gen.completed_at else ""
            ),
        })

    total = round(sum(item["cost"] for item in items), 4)
    return {
        "label": "Burned Amount",
        "sub_string": f"Last {BURNED_TASKS_LIMIT} tasks (USD)",
        "total": total,
        "average_per_task": round(total / len(items), 4) if items else 0.0,
        "count": str(len(items)),
        "items": items,
    }


def _compute_recent_activity(env, gen_scope):
    Generation = env["crowley.generation"].sudo()
    category_labels = dict(Generation._fields["category"].selection)
    gens = Generation.search(
        gen_scope, order="write_date desc, id desc", limit=RECENT_ACTIVITY_LIMIT
    )

    items = []
    for gen in gens:
        state = gen.state
        review_state = gen.review_state
        if state in DONE_STATES and review_state == "approved":
            action = "approved"
            when = gen.completed_at or gen.write_date
        elif state in DONE_STATES and review_state == "rejected":
            action = "rejected"
            when = gen.completed_at or gen.write_date
        elif state in DONE_STATES:
            action = "completed"
            when = gen.completed_at or gen.write_date
        elif state in FAILED_STATES:
            action = "failed"
            when = gen.write_date
        else:
            action = "updated"
            when = gen.write_date
        items.append({
            "actor_id": gen.user_id.id,
            "actor_name": gen.user_id.name or "",
            "action": action,
            "task_code": gen.name or "",
            "category": category_labels.get(gen.category, "") if gen.category else "",
            "timestamp": when.isoformat() if when else "",
            "time_ago": _time_ago(when),
        })

    return {
        "label": "Recent Activity",
        "count": str(len(items)),
        "items": items,
    }


def _compute_coordination_events(env, gen_scope):
    # TPM "Coordination Events" feed: owner reassignments recorded via the
    # tracked user_id field on crowley.generation (mail.tracking.value).
    label = "TPM Activity — Coordination Events"
    Generation = env["crowley.generation"].sudo()
    gens = Generation.search(gen_scope)
    if not gens:
        return {"label": label, "count": "0", "items": []}

    code_by_id = {gen.id: (gen.name or "") for gen in gens}
    user_field = env["ir.model.fields"]._get("crowley.generation", "user_id")
    messages = env["mail.message"].sudo().search(
        [
            ("model", "=", "crowley.generation"),
            ("res_id", "in", gens.ids),
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


class CrowleyDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/crowley_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_ext_dashboard_overview(self, **kwargs):
        guard = _require_crowley_user()
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

        role_tag, gen_domain, _projects = _role_scope(env)
        attempt_base = [
            (f"job_id.{field}", operator, value)
            for field, operator, value in gen_domain
        ]
        attempt_date = [
            (f"job_id.{field}", operator, value)
            for field, operator, value in date_domain
        ]
        gen_scope = gen_domain + date_domain
        attempt_scope = attempt_base + attempt_date
        view = _overview_view(env, role_tag)

        # KPI — role-specific cards (one card per item), in this view's order.
        kpi_by_key = {
            item["key"]: item
            for item in _compute_kpi(env, gen_scope, attempt_scope)["items"]
        }
        kpi_items = [
            kpi_by_key[key]
            for key in KPI_KEYS_BY_VIEW[view]
            if key in kpi_by_key
        ]

        # Single `overview` wrapper. Both Crowley extensions expose the SAME
        # 12-key schema so one frontend model fits both; a section is filled
        # with real data only when it belongs to this view's page
        # (SECTIONS_BY_VIEW) and is returned blank ({}) otherwise. `budget`,
        # `burn_rate` and `accepted_per_day` are sourcing-only sections — kept
        # here as always-blank keys for schema parity. KPI items are the
        # role-specific cards; `overview.role` tells the frontend the view.
        sections = SECTIONS_BY_VIEW[view]

        def _section(key, builder):
            return builder() if key in sections else {}

        overview = {
            "role": role_tag or "tasker",
            "kpi": {"count": str(len(kpi_items)), "items": kpi_items},
            "budget": {},
            "burn_rate": {},
            "accepted_per_day": {},
            "task_progress": _section(
                "task_progress", lambda: _compute_task_progress(env, gen_scope)
            ),
            "approved_per_week": _section(
                "approved_per_week",
                lambda: _compute_approved_per_week(env, attempt_base, weeks),
            ),
            "recent_activity": _section(
                "recent_activity",
                lambda: _compute_recent_activity(env, gen_scope),
            ),
            "coordination_events": _section(
                "coordination_events",
                lambda: _compute_coordination_events(env, gen_scope),
            ),
            "tasks_done_chart": _section(
                "tasks_done_chart",
                lambda: _compute_tasks_done_chart(env, gen_scope),
            ),
            "burned_amount_chart": _section(
                "burned_amount_chart",
                lambda: _compute_burned_amount_chart(env, gen_scope),
            ),
            "my_activity": _section(
                "my_activity", lambda: _compute_my_activity(env, gen_scope)
            ),
        }

        return return_Response(
            message="OK",
            status=200,
            data={"overview": overview},
        )
