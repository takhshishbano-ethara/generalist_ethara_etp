from datetime import date, datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

BUDGET_PARAM = "crowley_sourcing.budget_usd"
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26
IN_FLIGHT_STATES = ("processing", "exporting")
FAILED_STATES = ("error",)


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


def _require_sourcing_user():
    env = request.env
    user = env.user
    if user.has_group("video_editor_s3.group_video_editor_s3_user") or user.has_group(
        "video_editor_s3.group_video_editor_s3_manager"
    ):
        return None
    return return_Response(
        message="You are not allowed to access Crowley Sourcing dashboards.",
        status=403,
    )


def _generation_scope_domain():
    env = request.env
    user = env.user
    if user.has_group("video_editor_s3.group_video_editor_s3_manager"):
        return []
    return [("assigned_to", "=", user.id)]


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
    start_raw = (params.get("start_date") or "").strip()
    end_raw = (params.get("end_date") or "").strip()
    if start_raw:
        start_date, err = _parse_date(start_raw, "start_date")
        if err:
            return None, err
        domain.append(
            ("create_date", ">=", datetime.combine(start_date, datetime.min.time()))
        )
    if end_raw:
        end_date, err = _parse_date(end_raw, "end_date")
        if err:
            return None, err
        domain.append(
            ("create_date", "<=", datetime.combine(end_date, datetime.max.time()))
        )
    return domain, None


def _resolve_trend_weeks(params):
    raw = (params.get("weeks") or "").strip()
    if not raw:
        return DEFAULT_TREND_WEEKS, None
    try:
        weeks = int(raw)
    except (TypeError, ValueError):
        return None, return_Response(
            message=f"Invalid weeks '{raw}'. Expected integer between 1 and {MAX_TREND_WEEKS}.",
            status=400,
        )
    if weeks < 1 or weeks > MAX_TREND_WEEKS:
        return None, return_Response(
            message=f"weeks must be between 1 and {MAX_TREND_WEEKS}.",
            status=400,
        )
    return weeks, None


def _budget_usd(env):
    raw = env["ir.config_parameter"].sudo().get_param(BUDGET_PARAM, "0")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _compute_kpi(env, gen_scope):
    Project = env["video.editor.project"].sudo()

    total_tasks = Project.search_count(gen_scope)
    draft = Project.search_count(gen_scope + [("state", "=", "draft")])
    in_flight = Project.search_count(
        gen_scope + [("state", "in", list(IN_FLIGHT_STATES))]
    )
    active_tasks = draft + in_flight

    cost_rows = Project.read_group(
        gen_scope + [("llm_qc_cost_usd", ">", 0)],
        fields=["llm_qc_cost_usd:sum"],
        groupby=[],
    )
    spent = 0.0
    if cost_rows:
        spent = round(float(cost_rows[0].get("llm_qc_cost_usd") or 0.0), 2)

    budget = _budget_usd(env)
    budget_caption = ""
    if budget:
        budget_caption = f"of ${budget:,.2f} budget"

    approved = Project.search_count(
        gen_scope + [("review_status", "=", "approved")]
    )
    rejected = Project.search_count(
        gen_scope + [("review_status", "=", "rejected")]
    )
    reviewed = approved + rejected
    approval_rate = _pct(approved, reviewed)

    owner_rows = Project.read_group(
        gen_scope,
        fields=["assigned_to"],
        groupby=["assigned_to"],
        lazy=False,
    )
    owner_ids = []
    for row in owner_rows:
        owner = row.get("assigned_to")
        if owner and isinstance(owner, (list, tuple)):
            owner_ids.append(owner[0])
    members = env["res.users"].sudo().browse(owner_ids)
    manager_count = 0
    for u in members:
        if u.has_group("video_editor_s3.group_video_editor_s3_manager"):
            manager_count += 1
    member_count = len(members)

    items = [
        _kpi_item(
            "total_burned",
            "Total Burned",
            f"${spent:,.2f}",
            sub_string=budget_caption,
        ),
        _kpi_item(
            "active_tasks",
            "Active Tasks",
            active_tasks,
            sub_string=f"{draft} draft · {in_flight} in-flight",
        ),
        _kpi_item(
            "approval_rate",
            "Approval Rate",
            f"{approval_rate}%",
            sub_string=f"{approved} approved / {reviewed} reviewed",
        ),
        _kpi_item(
            "team_members",
            "Team Members",
            member_count,
            sub_string=f"{manager_count} managers"
            if manager_count
            else "",
        ),
    ]
    return {"count": len(items), "items": items}


STAGE_BUCKETS = [
    ("draft", "Draft", [("state", "=", "draft")]),
    (
        "generating",
        "Generating",
        [("state", "in", list(IN_FLIGHT_STATES))],
    ),
    (
        "pending_review",
        "Done · Pending Review",
        [
            "|",
            ("state", "=", "processed"),
            "&",
            ("state", "=", "exported"),
            ("review_status", "in", (False, "pending")),
        ],
    ),
    (
        "approved",
        "Approved",
        [("state", "=", "exported"), ("review_status", "=", "approved")],
    ),
    (
        "rejected_failed",
        "Rejected / Failed",
        [
            "|",
            ("state", "in", list(FAILED_STATES)),
            "&",
            ("state", "=", "exported"),
            ("review_status", "=", "rejected"),
        ],
    ),
]


def _compute_task_progress(env, gen_scope):
    Project = env["video.editor.project"].sudo()
    items = []
    total = 0
    for key, label, domain in STAGE_BUCKETS:
        count = Project.search_count(gen_scope + domain)
        total += count
        items.append({"key": key, "label": label, "count": count})
    return {
        "label": "Task Progress",
        "total": total,
        "count": str(total),
        "items": items,
    }


def _compute_approved_per_week(env, gen_scope, weeks):
    Project = env["video.editor.project"].sudo()
    today = fields.Date.context_today(env.user)
    current_week_start = _week_start(today)
    earliest_week_start = current_week_start - timedelta(weeks=weeks - 1)
    window_start_dt = datetime.combine(earliest_week_start, datetime.min.time())

    domain = gen_scope + [
        ("review_status", "=", "approved"),
        ("review_decided_at", ">=", window_start_dt),
    ]
    rows = Project.search_read(
        domain,
        fields=["review_decided_at"],
        order="review_decided_at asc",
    )

    bucket = {}
    for row in rows:
        decided = row.get("review_decided_at")
        if not decided:
            continue
        if isinstance(decided, datetime):
            d = decided.date()
        else:
            d = decided
        ws = _week_start(d)
        bucket[ws] = bucket.get(ws, 0) + 1

    series = []
    for i in range(weeks):
        ws = earliest_week_start + timedelta(weeks=i)
        we = ws + timedelta(days=6)
        count = bucket.get(ws, 0)
        series.append(
            {
                "week_start": ws.isoformat(),
                "week_end": we.isoformat(),
                "label": f"{ws.strftime('%b %d')}",
                "count": count,
            }
        )

    current = series[-1]["count"] if series else 0
    previous = series[-2]["count"] if len(series) >= 2 else 0
    delta = current - previous
    if delta > 0:
        pattern, sign = "up", "+"
    elif delta < 0:
        pattern, sign = "down", "-"
    else:
        pattern, sign = "", ""

    headline = current
    headline_caption = (
        f"{sign}{abs(delta)} vs previous week" if pattern else "no change vs previous week"
    )

    return {
        "title": "Approved per week",
        "sub_title": f"Trailing {weeks} weeks",
        "type": "bar_chart",
        "headline": headline,
        "headline_caption": headline_caption,
        "legend": [{"key": "approved", "label": "Approved", "color_token": "success"}],
        "data": series,
        "delta_vs_prev_week": delta,
        "pattern": pattern,
        "sign": sign,
    }


class CrowleySourcingDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/crowley_sourcing_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_sourcing_ext_dashboard_overview(self, **kwargs):
        guard = _require_sourcing_user()
        if guard is not None:
            return guard

        params = kwargs or {}
        date_domain, err = _date_filter_domain(params)
        if err:
            return err
        weeks, err = _resolve_trend_weeks(params)
        if err:
            return err

        env = request.env
        base_scope = _generation_scope_domain()
        gen_scope = base_scope + date_domain

        data = {
            "overview": {
                "kpi": _compute_kpi(env, gen_scope),
                "task_progress": _compute_task_progress(env, gen_scope),
                "approved_per_week": _compute_approved_per_week(
                    env, base_scope, weeks
                ),
            }
        }
        return return_Response(
            message="Dashboard overview fetched successfully.",
            status=200,
            data=data,
        )
