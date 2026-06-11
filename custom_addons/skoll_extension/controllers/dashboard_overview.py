from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    DONE_STATE,
    PASS_RATE_GOOD,
    WEEKDAY_LABELS,
    _money,
    _parse_date,
    _pct1,
    _scope as _role_scope,
    _user_role_tag,
)

_logger = logging.getLogger(__name__)

IN_FLIGHT_STATES = ()
FAILED_STATES = ("failed",)
DONE_STATES = ("passed",)
APPROVED_STATES = ("passed",)
REWORK_STATES = ("failed",)
DRAFT_STATES = ("pending",)
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26
MY_ACTIVITY_WINDOW_DAYS = 30
HEATMAP_LEVELS = 4
TASKS_DONE_WINDOW_DAYS = 7
BURNED_TASKS_LIMIT = 30
RECENT_ACTIVITY_LIMIT = 8
COORDINATION_EVENTS_LIMIT = 8

STAGE_BUCKETS = (
    ("s1_draft", "S1 Draft"),
    ("s2_qc_approved", "S2 QC Approved"),
    ("s3_delivered", "S3 Delivered"),
    ("rejected_failed", "Rejected / Failed"),
)


def _require_skoll_user(env):
    if _user_role_tag(env) is None:
        return False
    return True


def _kpi_item(key, label, value, sub_string="", pattern="neutral", sign=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _pct(part, whole):
    return _pct1(part, whole)


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _date_filter_domain(params):
    dom = []
    start = _parse_date(params.get("start_date"))
    end = _parse_date(params.get("end_date"))
    if start:
        dom.append(("create_date", ">=", datetime.combine(start, time.min)))
    if end:
        dom.append(("create_date", "<=", datetime.combine(end, time.max)))
    return dom


def _resolve_trend_weeks(params):
    raw = params.get("weeks") or DEFAULT_TREND_WEEKS
    try:
        weeks = int(raw)
    except (TypeError, ValueError):
        weeks = DEFAULT_TREND_WEEKS
    return max(1, min(weeks, MAX_TREND_WEEKS))


def _budget_usd(env):
    try:
        raw = env["ir.config_parameter"].sudo().get_param("skoll.budget_usd")
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


def _today_bounds(env):
    today = fields.Date.context_today(env.user)
    return (
        datetime.combine(today, time.min),
        datetime.combine(today, time.max),
        today,
    )


def _approved_reviewed(env, task_scope):
    Task = env["skoll.skoll"].sudo()
    approved = Task.search_count(task_scope + [("qc_status", "in", list(APPROVED_STATES))])
    rework = Task.search_count(task_scope + [("qc_status", "in", list(REWORK_STATES))])
    return approved, approved + rework


def _intensity(count, max_count):
    if not max_count or count <= 0:
        return 0
    ratio = count / max_count
    level = int(round(ratio * (HEATMAP_LEVELS - 1)))
    return max(0, min(level, HEATMAP_LEVELS - 1))


def _time_ago(when):
    if not when:
        return ""
    now = fields.Datetime.now()
    if isinstance(when, str):
        try:
            when = fields.Datetime.from_string(when)
        except Exception:
            return ""
    delta = now - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _done_per_day(env, task_scope, day_from, day_to):
    Task = env["skoll.skoll"].sudo()
    dom = task_scope + [
        ("qc_status", "=", DONE_STATE),
        ("write_date", ">=", datetime.combine(day_from, time.min)),
        ("write_date", "<=", datetime.combine(day_to, time.max)),
    ]
    rows = Task.read_group(dom, ["write_date:day", "id:count"], ["write_date:day"])
    out = defaultdict(int)
    for row in rows:
        raw_day = row.get("write_date:day") or row.get("write_date")
        count = int(row.get("write_date_count") or row.get("__count") or 0)
        if not raw_day:
            continue
        if isinstance(raw_day, str):
            try:
                parsed = datetime.strptime(raw_day, "%d %b %Y").date()
            except ValueError:
                try:
                    parsed = datetime.strptime(raw_day, "%Y-%m-%d").date()
                except ValueError:
                    continue
        else:
            parsed = raw_day if hasattr(raw_day, "isoformat") else None
            if parsed is None:
                continue
            if hasattr(parsed, "date"):
                parsed = parsed.date()
        out[parsed.isoformat()] += count
    return out


def _team_members_from_projects(projects):
    leads = projects.mapped("project_lead")
    qrs = projects.mapped("project_qc_reviewer")
    taskers = projects.mapped("project_tasker")
    aires = projects.mapped("project_aire")
    swes = projects.mapped("project_swe")
    total = len(leads | qrs | taskers | aires | swes)
    sub_string = (
        f"{len(leads)} leads · {len(qrs)} QR · {len(taskers)} taskers · "
        f"{len(aires)} AIRE · {len(swes)} SWE"
    )
    return total, sub_string


def _scope_projects_for_team(env):
    role_tag = _user_role_tag(env)
    Project = env["project.project"].sudo()
    if role_tag in ("full", "ql", "manager"):
        return Project.search([])
    employee_ids = env.user.employee_ids.ids
    if not employee_ids:
        return Project.browse()
    return Project.search([
        "|", "|", "|", "|",
        ("project_lead", "in", employee_ids),
        ("project_qc_reviewer", "in", employee_ids),
        ("project_tasker", "in", employee_ids),
        ("project_aire", "in", employee_ids),
        ("project_swe", "in", employee_ids),
    ])


def _compute_kpi(env, task_scope, gen_scope):
    Task = env["skoll.skoll"].sudo()
    Generation = env["skoll.generation"].sudo()

    cost_rows = Generation.read_group(gen_scope, ["total_cost:sum"], [])
    total_burned = float((cost_rows[0]["total_cost"] if cost_rows else 0.0) or 0.0)
    budget = _budget_usd(env)
    if budget > 0:
        sub_burn = f"{_pct(total_burned, budget)}% of {_money(budget)} budget"
    else:
        sub_burn = "No budget configured"

    total_tasks = Task.search_count(task_scope)
    draft = Task.search_count(task_scope + [("qc_status", "in", list(DRAFT_STATES))])
    in_flight = Task.search_count(task_scope + [("qc_status", "in", list(IN_FLIGHT_STATES))])
    active = draft + in_flight

    approved, reviewed = _approved_reviewed(env, task_scope)
    approval_rate = _pct(approved, reviewed) if reviewed else 0.0

    team_total, team_sub_string = _team_members_from_projects(
        _scope_projects_for_team(env)
    )

    today_from, today_to, today_date = _today_bounds(env)
    yesterday_date = today_date - timedelta(days=1)
    yesterday_from = datetime.combine(yesterday_date, time.min)
    yesterday_to = datetime.combine(yesterday_date, time.max)

    approved_today = Task.search_count(
        task_scope
        + [
            ("qc_status", "in", list(APPROVED_STATES)),
            ("write_date", ">=", today_from),
            ("write_date", "<=", today_to),
        ]
    )
    approved_yesterday = Task.search_count(
        task_scope
        + [
            ("qc_status", "in", list(APPROVED_STATES)),
            ("write_date", ">=", yesterday_from),
            ("write_date", "<=", yesterday_to),
        ]
    )
    delta_today = approved_today - approved_yesterday
    if delta_today > 0:
        pattern_today, sign_today = "up", "+"
    elif delta_today < 0:
        pattern_today, sign_today = "down", "-"
    else:
        pattern_today, sign_today = "neutral", ""

    thirty_days_ago = today_date - timedelta(days=30)
    qc_window_scope = task_scope + [
        ("write_date", ">=", datetime.combine(thirty_days_ago, time.min)),
        ("write_date", "<=", today_to),
    ]
    qc_approved_30d, qc_reviewed_30d = _approved_reviewed(env, qc_window_scope)
    qc_pass_rate = _pct(qc_approved_30d, qc_reviewed_30d) if qc_reviewed_30d else 0.0

    total_done = Task.search_count(task_scope + [("qc_status", "=", DONE_STATE)])

    items = [
        _kpi_item(
            "total_burned",
            "Total Burned",
            _money(total_burned),
            sub_burn,
        ),
        _kpi_item(
            "active_tasks",
            "Active Tasks",
            f"{active}/{total_tasks}",
            f"{draft} unstarted · {in_flight} in progress",
        ),
        _kpi_item(
            "approval_rate",
            "Approval Rate",
            f"{approval_rate}%",
            f"{approved}/{reviewed} approved",
            pattern="up" if approval_rate >= PASS_RATE_GOOD else "down" if reviewed else "neutral",
            sign="+" if approval_rate >= PASS_RATE_GOOD else "-" if reviewed else "",
        ),
        _kpi_item(
            "team_members",
            "Team Members",
            f"{team_total}",
            team_sub_string,
        ),
        _kpi_item(
            "approved_today",
            "Approved Today",
            f"{approved_today}",
            f"vs {approved_yesterday} yesterday",
            pattern=pattern_today,
            sign=sign_today,
        ),
        _kpi_item(
            "qc_pass_rate",
            "QC Pass Rate (30d)",
            f"{qc_pass_rate}%",
            f"{qc_approved_30d}/{qc_reviewed_30d} reviewed",
        ),
        _kpi_item(
            "total_tasks_done",
            "Tasks Done",
            f"{total_done}",
            "Lifetime",
        ),
    ]
    return {"count": str(len(items)), "items": items}


def _compute_task_progress(env, task_scope):
    Task = env["skoll.skoll"].sudo()
    bucket_counts = {
        "s1_draft": Task.search_count(
            task_scope + [("qc_status", "in", list(DRAFT_STATES) + list(IN_FLIGHT_STATES))]
        ),
        "s2_qc_approved": Task.search_count(
            task_scope + [("qc_status", "in", list(APPROVED_STATES))]
        ),
        "s3_delivered": Task.search_count(
            task_scope + [("qc_status", "in", list(APPROVED_STATES))]
        ),
        "rejected_failed": Task.search_count(
            task_scope + [("qc_status", "in", list(FAILED_STATES))]
        ),
    }
    total = sum(bucket_counts.values()) or 1
    items = []
    for key, label in STAGE_BUCKETS:
        value = bucket_counts.get(key, 0)
        items.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "percentage": _pct(value, total),
            }
        )
    return {
        "label": "Task Progress (by Stage)",
        "total": sum(bucket_counts.values()),
        "count": str(len(items)),
        "items": items,
    }


def _compute_approved_per_week(env, task_scope, weeks):
    Task = env["skoll.skoll"].sudo()
    today = fields.Date.context_today(env.user)
    this_week_start = _week_start(today)
    items = []
    counts = []
    for i in range(weeks):
        week_start = this_week_start - timedelta(weeks=(weeks - 1 - i))
        week_end = week_start + timedelta(days=6)
        count = Task.search_count(
            task_scope
            + [
                ("qc_status", "in", list(APPROVED_STATES)),
                ("write_date", ">=", datetime.combine(week_start, time.min)),
                ("write_date", "<=", datetime.combine(week_end, time.max)),
            ]
        )
        counts.append(count)
        items.append(
            {
                "key": f"w{i + 1}",
                "label": f"W{i + 1}",
                "value": count,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "delta_vs_prev_week": 0,
                "pattern": "neutral",
                "sign": "",
            }
        )
    for idx in range(1, len(items)):
        delta = counts[idx] - counts[idx - 1]
        items[idx]["delta_vs_prev_week"] = delta
        if delta > 0:
            items[idx]["pattern"] = "up"
            items[idx]["sign"] = "+"
        elif delta < 0:
            items[idx]["pattern"] = "down"
            items[idx]["sign"] = "-"
    return {
        "label": "Approved Per Week",
        "sub_string": f"Trailing {weeks} weeks",
        "total": sum(counts),
        "count": str(len(items)),
        "items": items,
    }


def _compute_my_activity(env, task_scope):
    today = fields.Date.context_today(env.user)
    window_from = today - timedelta(days=MY_ACTIVITY_WINDOW_DAYS - 1)
    per_day = _done_per_day(env, task_scope, window_from, today)
    max_count = max(per_day.values()) if per_day else 0

    days = []
    total_tasks = 0
    longest_streak = 0
    current_streak = 0
    active_days = 0
    for offset in range(MY_ACTIVITY_WINDOW_DAYS):
        d = window_from + timedelta(days=offset)
        count = per_day.get(d.isoformat(), 0)
        total_tasks += count
        if count > 0:
            current_streak += 1
            active_days += 1
            if current_streak > longest_streak:
                longest_streak = current_streak
        else:
            current_streak = 0
        days.append(
            {
                "date": d.isoformat(),
                "weekday": d.weekday(),
                "weekday_label": WEEKDAY_LABELS[d.weekday()],
                "count": count,
                "intensity": _intensity(count, max_count),
            }
        )

    prev_window_from = window_from - timedelta(days=MY_ACTIVITY_WINDOW_DAYS)
    prev_window_to = window_from - timedelta(days=1)
    prev_per_day = _done_per_day(env, task_scope, prev_window_from, prev_window_to)
    prev_total = sum(prev_per_day.values())
    delta = total_tasks - prev_total

    avg_per_day = round(total_tasks / MY_ACTIVITY_WINDOW_DAYS, 2)

    return {
        "label": "My Activity",
        "sub_string": f"Last {MY_ACTIVITY_WINDOW_DAYS} days",
        "window": {"start": window_from.isoformat(), "end": today.isoformat()},
        "max_count": max_count,
        "days": days,
        "summary": {
            "total_tasks": total_tasks,
            "total_tasks_delta": delta,
            "avg_per_day": avg_per_day,
            "longest_streak": longest_streak,
            "active_days": active_days,
            "total_days": MY_ACTIVITY_WINDOW_DAYS,
        },
    }


def _compute_tasks_done_chart(env, task_scope):
    today = fields.Date.context_today(env.user)
    window_from = today - timedelta(days=TASKS_DONE_WINDOW_DAYS - 1)
    per_day = _done_per_day(env, task_scope, window_from, today)

    items = []
    total = 0
    for offset in range(TASKS_DONE_WINDOW_DAYS):
        d = window_from + timedelta(days=offset)
        count = per_day.get(d.isoformat(), 0)
        total += count
        items.append(
            {
                "date": d.isoformat(),
                "label": WEEKDAY_LABELS[d.weekday()],
                "value": count,
                "approved": count,
            }
        )

    avg = round(total / TASKS_DONE_WINDOW_DAYS, 2)
    return {
        "label": "Tasks Done",
        "sub_string": f"Last {TASKS_DONE_WINDOW_DAYS} days",
        "window": {"start": window_from.isoformat(), "end": today.isoformat()},
        "total": total,
        "average_per_day": avg,
        "count": str(len(items)),
        "items": items,
    }


def _compute_burned_amount_chart(env, gen_scope):
    Generation = env["skoll.generation"].sudo()
    records = Generation.search(gen_scope, limit=BURNED_TASKS_LIMIT, order="create_date desc")
    items = []
    total_cost = 0.0
    for seq, rec in enumerate(records, start=1):
        cost = float(rec.total_cost or 0.0)
        total_cost += cost
        items.append(
            {
                "seq": seq,
                "code": rec.task_ref or (rec.task_id.task_id if rec.task_id else f"gen-{rec.id}"),
                "cost": round(cost, 4),
                "tokens": int(rec.total_tokens or 0),
                "duration_seconds": float(rec.duration_s or 0.0),
                "model": rec.model_arn or "",
                "completed_at": fields.Datetime.to_string(rec.create_date) if rec.create_date else None,
            }
        )
    avg = (total_cost / len(items)) if items else 0.0
    return {
        "label": "Burned Amount",
        "sub_string": f"Last {len(items)} generations",
        "total": round(total_cost, 4),
        "average_per_task": round(avg, 4),
        "count": str(len(items)),
        "items": items,
    }


def _compute_recent_activity(env, task_scope):
    Task = env["skoll.skoll"].sudo()
    records = Task.search(task_scope, limit=RECENT_ACTIVITY_LIMIT, order="write_date desc, id desc")
    items = []
    for rec in records:
        actor = rec.write_uid or rec.create_uid
        actor_id = actor.id if actor else 0
        actor_name = actor.name if actor else "System"
        action = "updated"
        if rec.qc_status in APPROVED_STATES:
            action = "approved"
        elif rec.qc_status in REWORK_STATES:
            action = "rejected"
        elif rec.qc_status in IN_FLIGHT_STATES:
            action = "started"
        elif rec.qc_status in DRAFT_STATES:
            action = "drafted"
        elif rec.qc_status in FAILED_STATES:
            action = "failed"
        category = ""
        if rec.life_domain_ids:
            category = rec.life_domain_ids[0].name or ""
        items.append(
            {
                "actor_id": actor_id,
                "actor_name": actor_name,
                "action": action,
                "task_code": rec.task_id or "",
                "category": category,
                "timestamp": fields.Datetime.to_string(rec.write_date) if rec.write_date else None,
                "time_ago": _time_ago(rec.write_date),
            }
        )
    return {
        "label": "Recent Activity",
        "count": str(len(items)),
        "items": items,
    }


def _compute_coordination_events(env, task_scope):
    return {
        "label": "TPM Activity — Coordination Events",
        "count": "0",
        "items": [],
    }


class SkollDashboardOverviewController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/dashboard_overview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def dashboard_overview(self, **kwargs):
        env = request.env
        if not _require_skoll_user(env):
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            role_tag, task_domain, _personas = _role_scope(env)
            date_dom = _date_filter_domain(kwargs)
            task_scope = task_domain + date_dom
            gen_scope = []
            for leaf in task_domain + date_dom:
                if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
                    f, op, v = leaf
                    gen_scope.append((f"task_id.{f}", op, v))
                else:
                    gen_scope.append(leaf)
            weeks = _resolve_trend_weeks(kwargs)
            overview = {
                "role": role_tag,
                "kpi": _compute_kpi(env, task_scope, gen_scope),
                "task_progress": _compute_task_progress(env, task_scope),
                "approved_per_week": _compute_approved_per_week(env, task_scope, weeks),
                "recent_activity": _compute_recent_activity(env, task_scope),
                "coordination_events": _compute_coordination_events(env, task_scope),
                "my_activity": _compute_my_activity(env, task_scope),
                "tasks_done_chart": _compute_tasks_done_chart(env, task_scope),
                "burned_amount_chart": _compute_burned_amount_chart(env, gen_scope),
            }
        except Exception as exc:
            _logger.exception("Skoll dashboard_overview failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(message="OK", status=200, data={"overview": overview})
