from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _create_date_domain,
    _pct,
    _resolve_dashboard_filters,
    _scope,
    _user_role_tag,
)

# gohan.job lifecycle buckets (see gohan/models/gohan_job.py state field).
DRAFT_STATES = ("not_assigned", "draft")
IN_FLIGHT_STATES = ("extracting", "generating", "scoring")
DONE_STATES = ("done", "submitted")
FAILED_STATES = ("failed", "discarded", "cancelled")

# QC verdict groupings: a job "passes" QC when the reviewer marks it shippable
# or shippable-with-fixes; "reviewed" is any decided verdict.
APPROVED_VERDICTS = ("shippable", "fixes")
DECIDED_VERDICTS = ("shippable", "fixes", "not_shippable")

QC_PASS_RATE_WINDOW_DAYS = 30
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26

# Which KPI cards each view shows, in order. CTO/TPM and PL get the team-level
# "manager" cards; QC/tasker get the individual "individual" cards. Mirrors the
# crowley_extension dashboard_overview KPI_KEYS_BY_VIEW split so one frontend
# model fits both projects.
#
# Crowley's `total_burned` card is KEPT (never omitted) for schema parity, but
# gohan has no cost data (`llm_qc_cost_usd` is declared on gohan.job yet never
# populated by the pipeline), so it is emitted with an empty value — the Flutter
# KPI strip renders an empty value as "—". It self-heals to a real figure if the
# QC pipeline ever writes cost. `avg_score` is a gohan-specific addition (same
# KPI item shape). Every other card maps to a populated gohan.job field
# (state, qc_verdict, score, completed_at, owners).
KPI_KEYS_BY_VIEW = {
    "manager": (
        "total_burned",
        "active_tasks",
        "approval_rate",
        "team_members",
        "avg_score",
    ),
    "individual": (
        "total_tasks_done",
        "qc_pass_rate",
        "approved_today",
        "total_burned",
        "avg_score",
    ),
}

# Funnel buckets for the Task Progress card.
STAGE_BUCKETS = (
    ("draft", "Draft / Queued", [("state", "in", list(DRAFT_STATES))]),
    ("in_progress", "In Progress", [("state", "in", list(IN_FLIGHT_STATES))]),
    ("done", "Done / Submitted", [("state", "in", list(DONE_STATES))]),
    ("failed", "Failed / Discarded", [("state", "in", list(FAILED_STATES))]),
)


def _overview_view(role_tag):
    return "manager" if role_tag in ("full", "pl") else "individual"


def _kpi_item(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": str(value),
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _today_bounds(env):
    today = fields.Datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    return start, start + timedelta(days=1)


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


def _compute_kpi(env, scope):
    """Role-scoped KPI cards over gohan.job, keyed for KPI_KEYS_BY_VIEW.

    Every key is always computed; the controller filters the items down to the
    set this view shows (manager vs individual).
    """
    Job = env["gohan.job"].sudo()

    total_tasks = Job.search_count(scope)
    draft = Job.search_count(scope + [("state", "in", list(DRAFT_STATES))])
    in_flight = Job.search_count(scope + [("state", "in", list(IN_FLIGHT_STATES))])
    active = draft + in_flight
    tasks_done = Job.search_count(scope + [("state", "in", list(DONE_STATES))])

    score_rows = Job.formatted_read_group(
        scope + [("score", ">", 0)], [], ["__count", "score:avg"]
    )
    scored = (score_rows[0]["__count"] if score_rows else 0) or 0
    avg_score = (score_rows[0]["score:avg"] if score_rows else 0.0) or 0.0

    approved = Job.search_count(
        scope + [("qc_verdict", "in", list(APPROVED_VERDICTS))]
    )
    reviewed = Job.search_count(
        scope + [("qc_verdict", "in", list(DECIDED_VERDICTS))]
    )
    approval_rate = _pct(approved, reviewed)

    owner_rows = Job.formatted_read_group(scope, ["user_id"], ["__count"])
    owner_ids = {row["user_id"][0] for row in owner_rows if row.get("user_id")}
    members = len(owner_ids)

    today_start, today_end = _today_bounds(env)
    yesterday_start = today_start - timedelta(days=1)
    window_start = today_start - timedelta(days=QC_PASS_RATE_WINDOW_DAYS - 1)

    done_today = Job.search_count(
        scope
        + [
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", today_start),
            ("completed_at", "<", today_end),
        ]
    )
    done_yesterday = Job.search_count(
        scope
        + [
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", yesterday_start),
            ("completed_at", "<", today_start),
        ]
    )
    today_delta = done_today - done_yesterday

    approved_30 = Job.search_count(
        scope
        + [
            ("qc_verdict", "in", list(APPROVED_VERDICTS)),
            ("completed_at", ">=", window_start),
        ]
    )
    reviewed_30 = Job.search_count(
        scope
        + [
            ("qc_verdict", "in", list(DECIDED_VERDICTS)),
            ("completed_at", ">=", window_start),
        ]
    )
    qc_pass_rate = _pct(approved_30, reviewed_30)

    items = [
        # Cost is not tracked on gohan.job — keep crowley's key, empty value
        # (the KPI strip shows "—"); self-heals if llm_qc_cost_usd is populated.
        _kpi_item(
            "total_burned",
            "Total Burned",
            "",
            sub_string="",
        ),
        _kpi_item(
            "avg_score",
            "Avg Quality Score",
            round(avg_score, 1),
            sub_string=(
                f"across {scored} scored jobs" if scored else "No scored jobs yet"
            ),
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
            members,
            sub_string=f"{members} contributing",
        ),
        _kpi_item(
            "total_tasks_done",
            "Total Tasks Done",
            tasks_done,
            sub_string=f"{_pct(tasks_done, total_tasks)}% of {total_tasks} tasks",
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
            "approved_today",
            "Completed Today",
            done_today,
            sub_string=(
                f"{'+' if today_delta > 0 else ''}{today_delta} vs yesterday"
            ),
            pattern="up" if today_delta > 0 else ("down" if today_delta < 0 else ""),
            sign="+" if today_delta > 0 else ("-" if today_delta < 0 else ""),
        ),
    ]
    return {"count": str(len(items)), "items": items}


def _compute_task_progress(env, scope):
    Job = env["gohan.job"].sudo()
    total = Job.search_count(scope)
    items = []
    for key, label, bucket in STAGE_BUCKETS:
        count = Job.search_count(scope + bucket)
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


def _compute_approved_per_week(env, role_scope, weeks):
    """Weekly count of jobs that reached a done/submitted state, by
    completed_at. Uses the role scope (NOT the request date filter) so the
    trailing-N-weeks window is the only date constraint."""
    Job = env["gohan.job"].sudo()
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)
    window_start = current_week_start - timedelta(weeks=weeks)
    window_start_dt = datetime.combine(window_start, datetime.min.time())

    rows = Job.search_read(
        role_scope
        + [
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", window_start_dt),
        ],
        ["completed_at"],
    )
    counts = {}
    for row in rows:
        when = row.get("completed_at")
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
        "label": "Tasks Completed per Week",
        "sub_string": f"Trailing {weeks} weeks",
        "total": sum(item["value"] for item in items),
        "count": str(len(items)),
        "items": items,
    }


class GohanDashboardOverviewController(http.Controller):

    @http.route(
        "/api/v1/gohan_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def gohan_ext_dashboard_overview(self, **kwargs):
        """Role-scoped Overview tab for gohan.job.

        Returns the crowley_extension-style ``{"overview": {...}}`` envelope so
        the Flutter ``InternalOverviewTab`` renders it: a role-filtered KPI
        strip, a task-progress funnel and a tasks-completed-per-week trend.
        ``project_id`` is optional (the Overview tab is fetched with an empty
        ``project_id`` query param); when supplied the metrics narrow to that
        project's taskers, otherwise they stay role-scoped.
        """
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Gohan analytics.",
                status=403,
            )

        params = request.params or {}
        filters, error = _resolve_dashboard_filters(params)
        if error is not None:
            return error
        weeks, error = _resolve_trend_weeks(params)
        if error is not None:
            return error
        project, error = _resolve_project(env, params)
        if error is not None:
            return error

        _tag, role_domain, _projects = _scope(env)
        if project:
            tasker_user_ids = project.project_tasker.mapped("user_id").ids
            role_scope = role_domain + [("user_id", "in", tasker_user_ids)]
        else:
            role_scope = role_domain
        date_domain = _create_date_domain(filters["start"], filters["end"])
        scope = role_scope + date_domain

        view = _overview_view(role_tag)
        kpi_by_key = {
            item["key"]: item for item in _compute_kpi(env, scope)["items"]
        }
        kpi_items = [
            kpi_by_key[key]
            for key in KPI_KEYS_BY_VIEW[view]
            if key in kpi_by_key
        ]

        # Single `overview` wrapper holding the crowley 12-key schema. Only the
        # three sections the InternalOverviewTab consumes (kpi, task_progress,
        # approved_per_week) are populated; the rest are returned blank ({}) for
        # schema parity with crowley_extension / crowley_sourcing_extension.
        overview = {
            "role": role_tag or "tasker",
            "kpi": {"count": str(len(kpi_items)), "items": kpi_items},
            "budget": {},
            "burn_rate": {},
            "accepted_per_day": {},
            "task_progress": _compute_task_progress(env, scope),
            "approved_per_week": _compute_approved_per_week(
                env, role_scope, weeks
            ),
            "recent_activity": {},
            "coordination_events": {},
            "tasks_done_chart": {},
            "burned_amount_chart": {},
            "my_activity": {},
        }

        return return_Response(
            message="OK",
            status=200,
            data={"overview": overview},
        )
