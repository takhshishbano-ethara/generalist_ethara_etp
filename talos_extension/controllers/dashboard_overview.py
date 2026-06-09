from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

BUDGET_PARAM = "talos.budget_usd"
DEFAULT_TREND_WEEKS = 6
MAX_TREND_WEEKS = 26

IN_FLIGHT_STATUSES = ("NotSubmitted", "in_progress", "draft")
DONE_STATUSES = ("Submitted", "completed")

FULL_ACCESS_GROUPS = (
    "talos.group_talos_admin",
    "etp_user_roles.group_quality_lead",
)
PL_GROUPS = ("etp_user_roles.group_project_lead",)
TASKER_GROUPS = ("etp_user_roles.group_tasker",)


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


def _user_in_any(env, xmlids):
    user = env.user
    for xmlid in xmlids:
        if user.has_group(xmlid):
            return True
    return False


def _user_role_tag(env):
    if _user_in_any(env, FULL_ACCESS_GROUPS):
        return "full"
    if _user_in_any(env, PL_GROUPS):
        return "pl"
    if _user_in_any(env, TASKER_GROUPS):
        return "tasker"
    return None


def _scope_domain(env):
    tag = _user_role_tag(env)
    if tag in ("full", "pl"):
        return []
    if tag == "tasker":
        return [("user_id", "=", env.user.id)]
    return [("id", "=", False)]


def _require_talos_user():
    if _user_role_tag(request.env) is None:
        return return_Response(
            message="You are not allowed to access Talos data.",
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


_TOKEN_FIELDS = (
    "claude_input_tokens", "claude_output_tokens",
    "glm_input_tokens", "glm_output_tokens",
    "oneP_input_tokens", "oneP_output_tokens",
    "onePA_input_tokens", "onePA_output_tokens",
    "onePB_input_tokens", "onePB_output_tokens",
    "onePC_input_tokens", "onePC_output_tokens",
    "onePD_input_tokens", "onePD_output_tokens",
    "bedrock_input_tokens", "bedrock_output_tokens",
    "traj_qc_input_tokens", "traj_qc_output_tokens",
    "taskdesc_input_tokens", "taskdesc_output_tokens",
    "golden_input_tokens", "golden_output_tokens",
    "kimi_eval_input_tokens", "kimi_eval_output_tokens",
)


def _total_tokens(task):
    fields_to_sum = (
        "claude_input_tokens", "claude_output_tokens",
        "glm_input_tokens", "glm_output_tokens",
        "oneP_input_tokens", "oneP_output_tokens",
        "onePA_input_tokens", "onePA_output_tokens",
        "onePB_input_tokens", "onePB_output_tokens",
        "onePC_input_tokens", "onePC_output_tokens",
        "onePD_input_tokens", "onePD_output_tokens",
        "bedrock_input_tokens", "bedrock_output_tokens",
        "traj_qc_input_tokens", "traj_qc_output_tokens",
        "taskdesc_input_tokens", "taskdesc_output_tokens",
        "golden_input_tokens", "golden_output_tokens",
        "kimi_eval_input_tokens", "kimi_eval_output_tokens",
    )
    total = 0
    for fname in fields_to_sum:
        total += getattr(task, fname, 0) or 0
    return total


def _compute_kpi(env, scope):
    Talos = env["talos.talos"].sudo()

    total_tasks = Talos.search_count(scope)
    not_submitted = Talos.search_count(
        scope + [("task_status", "in", list(IN_FLIGHT_STATUSES))]
    )
    submitted = Talos.search_count(
        scope + [("task_status", "in", list(DONE_STATUSES))]
    )

    tasks_with_tokens = Talos.search(scope)
    total_tokens = sum(_total_tokens(t) for t in tasks_with_tokens)
    spent = round(total_tokens / 1000.0, 2)
    budget = _budget_usd(env)
    if budget > 0:
        burned_sub = (
            f"of ${budget:,.2f} budget \u00b7 ${max(budget - spent, 0.0):,.2f} remaining"
        )
        burned_pattern = "down" if spent > budget else ""
    else:
        burned_sub = "Lifetime spend \u00b7 no budget configured"
        burned_pattern = ""

    approved = Talos.search_count(scope + [("qc_status", "=", "passed")])
    rejected = Talos.search_count(scope + [("qc_status", "=", "failed")])
    reviewed = approved + rejected
    approval_rate = _pct(approved, reviewed)

    owner_rows = Talos.read_group(
        scope, fields=["user_id"], groupby=["user_id"], lazy=False
    )
    owner_ids = [row["user_id"][0] for row in owner_rows if row.get("user_id")]
    members = env["res.users"].sudo().browse(owner_ids)
    ql_count = len(
        members.filtered(
            lambda u: u.has_group("etp_user_roles.group_quality_lead")
        )
    )

    items = [
        _kpi_item(
            "total_burned",
            "Total Burned",
            f"{spent:.2f}",
            sub_string=burned_sub,
            pattern=burned_pattern,
            sign="-" if burned_pattern == "down" else "",
        ),
        _kpi_item(
            "active_tasks",
            "Active Tasks",
            f"{not_submitted}/{total_tasks}",
            sub_string=(
                f"{not_submitted} unstarted \u00b7 {submitted} in progress"
            ),
        ),
        _kpi_item(
            "approval_rate",
            "Approval Rate",
            f"{approval_rate}",
            sub_string=f"{approved} approved of {reviewed} reviewed",
        ),
        _kpi_item(
            "team_members",
            "Team Members",
            f"{len(members)}",
            sub_string=(
                f"{ql_count} managers \u00b7 {len(members) - ql_count} users"
            ),
        ),
    ]
    return {"count": str(len(items)), "items": items}


STAGE_BUCKETS = (
    (
        "s1_draft",
        "S1 Draft",
        [("task_status", "in", ("NotSubmitted", "draft", "in_progress", "Submitted"))],
    ),
    (
        "s2_qc_approved",
        "S2 QC'd \u00b7 Approved",
        [("qc_status", "=", "passed")],
    ),
    (
        "s3_delivered",
        "S3 Delivered",
        [("task_status", "=", "completed")],
    ),
    (
        "rejected_failed",
        "Rejected / Failed",
        [("qc_status", "=", "failed")],
    ),
)


ACTIVITY_LIMIT = 8


def _time_ago(dt):
    if not dt:
        return ""
    now = fields.Datetime.now()
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _activity_action(task):
    if task.qc_status == "passed":
        return "approved"
    if task.qc_status == "failed":
        return "rejected"
    if task.task_status == "completed":
        return "completed"
    if task.task_status in ("Submitted", "in_progress"):
        return "in_progress"
    return "updated"


def _compute_recent_activity(env, scope):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(
        scope, limit=ACTIVITY_LIMIT, order="write_date desc, id desc"
    )
    items = []
    for task in records:
        actor = task.write_uid or task.create_uid
        items.append({
            "actor_id": actor.id or False,
            "actor_name": actor.name or "",
            "action": _activity_action(task),
            "task_code": task.task_id or "",
            "category": task.task_type or task.persona_id.name or "",
            "timestamp": task.write_date.isoformat() if task.write_date else "",
            "time_ago": _time_ago(task.write_date),
        })
    return {
        "label": "Recent Activity",
        "count": str(len(items)),
        "items": items,
    }


def _compute_persona_breakdown(env, scope):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(scope)
    records = Talos.search(scope)
    counts = {}
    for task in records:
        name = task.persona_id.name or "unassigned"
        counts[name] = counts.get(name, 0) + 1
    items = [
        {
            "key": name,
            "label": name,
            "value": count,
            "percentage": _pct(count, total),
        }
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return {
        "label": "Tasks by Persona",
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _compute_sandbox_summary(env, scope):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(scope)
    sandboxes = records.mapped("sandbox_ids")
    running = 0
    stopped = 0
    other = 0
    for sb in sandboxes:
        status = getattr(sb, "docker_status", "") or ""
        if status in ("running",):
            running += 1
        elif status in ("exited", "stopped", "removed"):
            stopped += 1
        else:
            other += 1
    return {
        "label": "Sandboxes",
        "total": len(sandboxes),
        "running": running,
        "stopped": stopped,
        "other": other,
    }


def _bucket_summary(env, scope, field, label, allowed):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(scope)
    items = []
    for key in allowed:
        count = Talos.search_count(scope + [(field, "=", key)])
        items.append({
            "key": key,
            "label": key.replace("_", " ").title(),
            "value": count,
            "percentage": _pct(count, total),
        })
    return {
        "label": label,
        "total": total,
        "count": str(len(items)),
        "items": items,
    }


def _compute_golden_status_summary(env, scope):
    return _bucket_summary(
        env, scope, "golden_status",
        "Golden Generation Status",
        ("idle", "generating", "done", "error"),
    )


def _compute_qc_status_summary(env, scope):
    return _bucket_summary(
        env, scope, "qc_status",
        "QC Status",
        ("pending", "passed", "failed"),
    )


def _compute_task_progress(env, scope):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(scope)
    items = []
    for key, label, bucket_domain in STAGE_BUCKETS:
        count = Talos.search_count(scope + bucket_domain)
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


def _compute_approved_per_week(env, scope, weeks):
    Talos = env["talos.talos"].sudo()
    today = fields.Datetime.now().date()
    current_week_start = _week_start(today)

    window_start = current_week_start - timedelta(weeks=weeks)
    window_start_dt = datetime.combine(window_start, datetime.min.time())

    approvals = Talos.search_read(
        scope
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

        scope = _scope_domain(env) + date_domain
        role_tag = _user_role_tag(env) or ""

        return return_Response(
            message="OK",
            status=200,
            data={
                "overview": {
                    "role": role_tag,
                    "kpi": _compute_kpi(env, scope),
                    "task_progress": _compute_task_progress(env, scope),
                    "approved_per_week": _compute_approved_per_week(
                        env, _scope_domain(env), weeks
                    ),
                    "recent_activity": _compute_recent_activity(env, scope),
                    "coordination_events": {},
                    "tasks_done_chart": {},
                    "burned_amount_chart": {},
                    "my_activity": {},
                    "persona_breakdown": _compute_persona_breakdown(env, scope),
                    "sandbox_summary": _compute_sandbox_summary(env, scope),
                    "golden_status_summary": _compute_golden_status_summary(env, scope),
                    "qc_status_summary": _compute_qc_status_summary(env, scope),
                },
            },
        )
