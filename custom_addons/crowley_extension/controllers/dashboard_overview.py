from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

BUDGET_PARAM = "crowley.budget_usd"
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26

IN_FLIGHT_STATES = ("queued", "submitting", "processing", "downloading")
FAILED_STATES = ("failed", "cancelled")


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


def _require_crowley_user():
    if not request.env.user.has_group("crowley.group_crowley_user"):
        return return_Response(
            message="You are not allowed to access Crowley data.",
            status=403,
        )
    return None


def _generation_scope_domain():
    env = request.env
    user = env.user
    if user.has_group("crowley.group_crowley_manager"):
        return [("company_id", "in", user.company_ids.ids)]
    return [("user_id", "=", user.id)]


def _attempt_scope_domain():
    return [
        (f"job_id.{field}", operator, value)
        for field, operator, value in _generation_scope_domain()
    ]


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


def _compute_kpi(env, gen_scope, attempt_scope):
    Generation = env["crowley.generation"].sudo()
    Attempt = env["crowley.attempt"].sudo()

    total_tasks = Generation.search_count(gen_scope)
    draft = Generation.search_count(gen_scope + [("state", "=", "draft")])
    in_flight = Generation.search_count(
        gen_scope + [("state", "in", list(IN_FLIGHT_STATES))]
    )
    active = draft + in_flight

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
    ]
    return {"count": str(len(items)), "items": items}


STAGE_BUCKETS = (
    ("draft", "Draft", [("state", "=", "draft")]),
    ("generating", "Generating", [("state", "in", list(IN_FLIGHT_STATES))]),
    (
        "pending_review",
        "Done · Pending Review",
        [
            ("state", "=", "done"),
            ("review_state", "!=", "approved"),
            ("review_state", "!=", "rejected"),
        ],
    ),
    (
        "approved",
        "Approved",
        [("state", "=", "done"), ("review_state", "=", "approved")],
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

        gen_scope = _generation_scope_domain() + date_domain
        attempt_scope = _attempt_scope_domain() + [
            (f"job_id.{field}", operator, value)
            for field, operator, value in date_domain
        ]

        return return_Response(
            message="OK",
            status=200,
            data={
                "overview": {
                    "kpi": _compute_kpi(env, gen_scope, attempt_scope),
                    "task_progress": _compute_task_progress(env, gen_scope),
                    "approved_per_week": _compute_approved_per_week(
                        env, _attempt_scope_domain(), weeks
                    ),
                },
            },
        )
