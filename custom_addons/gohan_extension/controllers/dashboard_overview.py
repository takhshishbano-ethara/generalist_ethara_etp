from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    STAGE_BUCKETS,
    _build_team_overview_aligned,
    _classify_job_action,
    _create_date_domain,
    _initials,
    _pct,
    _resolve_dashboard_filters,
    _scope,
    _team_breakdown_sub,
    _time_ago,
    _user_role_tag,
    _week_start,
)

# gohan.job lifecycle buckets (see gohan/models/gohan_job.py state field).
DONE_STATES = ("done", "submitted")

# Submission-trend weekly window (the pen shows 7 trailing weeks).
DEFAULT_TREND_WEEKS = 7
MAX_TREND_WEEKS = 26
RECENT_ACTIVITY_LIMIT = 8


def _kpi_item(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": str(value),
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


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


def _build_overview_kpi(env, scope, role_scope, projects):
    """The pen's universal 4 KPI cards — identical for every role:
    Total Tasks Done · Total URLs Added · AVG Quality Score · Team Size.

    (Locked decision: the old manager/individual 5-KPI split is dropped; all
    roles get the same four.)
    """
    Job = env["gohan.job"].sudo()

    total = Job.search_count(scope)
    done = Job.search_count(scope + [("state", "in", list(DONE_STATES))])
    urls = Job.search_count(scope + [("url", "!=", False)])

    today = fields.Datetime.now().date()
    week_start_dt = datetime.combine(_week_start(today), datetime.min.time())
    this_week_added = Job.search_count(
        role_scope + [("create_date", ">=", week_start_dt)]
    )

    score_rows = Job.formatted_read_group(
        scope + [("score", ">", 0)], [], ["__count", "score:avg"]
    )
    scored = (score_rows[0]["__count"] if score_rows else 0) or 0
    avg_score = (score_rows[0]["score:avg"] if score_rows else 0.0) or 0.0

    team = _build_team_overview_aligned(env, projects)
    team_sub = _team_breakdown_sub(team["role_counts"])

    items = [
        _kpi_item(
            "total_tasks_done",
            "Total Tasks Done",
            done,
            sub_string=f"{this_week_added} added this week",
        ),
        _kpi_item(
            "total_urls_added",
            "Total URLs Added",
            urls,
            sub_string=f"{done} of {total} completed",
        ),
        _kpi_item(
            "avg_quality_score",
            "AVG Quality Score",
            f"{round(avg_score, 1)}%",
            sub_string=(
                f"across {scored} scored jobs" if scored else "No scored jobs yet"
            ),
        ),
        _kpi_item(
            "team_size",
            "Team Size",
            team["total_team_size"],
            sub_string=team_sub,
        ),
    ]
    return {"count": str(len(items)), "items": items}


def _build_task_progress(env, scope):
    """6-stage funnel matching the pen ProgressCard (Draft, Extracting,
    Generating PRD, Scoring, Done, Failed), each carrying a color_token."""
    Job = env["gohan.job"].sudo()
    total = Job.search_count(scope)
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


def _build_submission_trend(env, role_scope, weeks):
    """Weekly count of jobs that reached a done/submitted state, by
    completed_at — the pen Submission Trend bar chart. Uses the role scope (NOT
    the request date filter) so the trailing-N-weeks window is the only date
    constraint."""
    Job = env["gohan.job"].sudo()
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)
    earliest = current_week_start - timedelta(weeks=weeks - 1)
    window_start_dt = datetime.combine(earliest, datetime.min.time())

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
        wk = _week_start(when.date())
        counts[wk] = counts.get(wk, 0) + 1

    items = []
    total = 0
    for i in range(weeks):
        wk_start = earliest + timedelta(weeks=i)
        wk_end = wk_start + timedelta(days=6)
        count = counts.get(wk_start, 0)
        total += count
        items.append({
            "key": f"w{wk_start.isocalendar()[1]}",
            "label": f"{wk_start.strftime('%b')} {wk_start.day}",
            "value": count,
            "week_start": wk_start.isoformat(),
            "week_end": wk_end.isoformat(),
        })

    return {
        "title": "Submission Trend",
        "sub_title": "Tasks completed per week · project-wide",
        "type": "bar",
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _build_recent_activity(env, role_scope, limit=RECENT_ACTIVITY_LIMIT):
    """The pen ActivityCard: most recently-touched jobs as a feed. Each item is
    a structured row (actor + classified action + task code + category + age);
    the Flutter widget composes the display sentence and avatar colour."""
    Job = env["gohan.job"].sudo()
    jobs = Job.search(role_scope, order="write_date desc, id desc", limit=limit)
    items = []
    for job in jobs:
        actor = job.user_id
        when = job.write_date
        category = (job.category_id.name or "") if job.category_id else ""
        if not category:
            category = job.site_name or ""
        items.append({
            "actor_id": actor.id if actor else 0,
            "actor_name": actor.name if actor else "",
            "initials": _initials(actor.name if actor else ""),
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

        Returns the crowley_extension-style ``{"overview": {...}}`` envelope the
        Flutter ``InternalOverviewTab`` renders, now enriched for the updated
        pen: the universal-4 KPI strip, a 6-stage task-progress funnel, a weekly
        ``submission_trend`` bar chart and a ``recent_activity`` feed. The
        remaining keys stay blank ({}) for crowley schema parity. ``project_id``
        is optional; when supplied the metrics narrow to that project's taskers,
        otherwise they stay role-scoped.
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

        _tag, role_domain, scope_projects = _scope(env)
        if project:
            tasker_user_ids = project.project_tasker.mapped("user_id").ids
            role_scope = role_domain + [("user_id", "in", tasker_user_ids)]
            team_projects = project
        else:
            role_scope = role_domain
            team_projects = scope_projects
        date_domain = _create_date_domain(filters["start"], filters["end"])
        scope = role_scope + date_domain

        overview = {
            "role": role_tag,
            "kpi": _build_overview_kpi(env, scope, role_scope, team_projects),
            "budget": {},
            "burn_rate": {},
            "accepted_per_day": {},
            "task_progress": _build_task_progress(env, scope),
            "approved_per_week": {},
            "submission_trend": _build_submission_trend(env, role_scope, weeks),
            "recent_activity": _build_recent_activity(env, role_scope),
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
