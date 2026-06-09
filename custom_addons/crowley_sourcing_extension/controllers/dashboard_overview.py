from datetime import date, datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _scope as _role_scope

BUDGET_PARAM = "crowley_sourcing.budget_usd"
IN_FLIGHT_STATES = ("processing", "exporting")
FAILED_STATES = ("error",)
DONE_STATES = ("exported",)
BURN_DAYS = 30
ACCEPTED_DAYS = 14
ACCEPTED_TOP_CATEGORIES = 3
CATEGORY_COLOR_TOKENS = ("primary", "violet", "success", "info", "warn")
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


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _today(env):
    return fields.Date.context_today(env.user)


def _spend(Project, domain):
    rows = Project.read_group(
        domain + [("llm_qc_cost_usd", ">", 0)],
        fields=["llm_qc_cost_usd:sum"],
        groupby=[],
    )
    if rows:
        return round(float(rows[0].get("llm_qc_cost_usd") or 0.0), 2)
    return 0.0


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _time_ago(env, when):
    if not when:
        return ""
    now = fields.Datetime.now()
    seconds = int((now - when).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


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
    done = Project.search_count(gen_scope + [("state", "in", list(DONE_STATES))])
    qc_done = Project.search_count(gen_scope + [("llm_qc_result", "!=", False)])

    spent = _spend(Project, gen_scope)
    budget = _budget_usd(env)
    budget_caption = ""
    if budget:
        budget_caption = f"of ${budget:,.2f} cap · {_pct(spent, budget):.0f}%"

    approved = Project.search_count(
        gen_scope + [("review_status", "=", "approved")]
    )
    rejected = Project.search_count(
        gen_scope + [("review_status", "=", "rejected")]
    )
    reviewed = approved + rejected
    approval_rate = _pct(approved, reviewed)
    pending = Project.search_count(gen_scope + [("review_status", "=", "pending")])
    force_passed = Project.search_count(
        gen_scope + [("llm_qc_force_passed", "=", True)]
    )

    today_start = datetime.combine(_today(env), datetime.min.time())
    done_today = Project.search_count(
        gen_scope
        + [("state", "in", list(DONE_STATES)), ("write_date", ">=", today_start)]
    )

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
            "Total Burn",
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
            "Total Members",
            member_count,
            sub_string=f"{manager_count} managers" if manager_count else "",
        ),
        _kpi_item(
            "tasks_done_qc",
            "Tasks Done & QC Done",
            done,
            sub_string=f"{qc_done} QC'd · {total_tasks} total",
        ),
        _kpi_item(
            "total_qc_done",
            "Total QC Done",
            qc_done,
            sub_string="reviews issued",
        ),
        _kpi_item(
            "force_submit_rate",
            "Force Submit & Rate",
            f"{force_passed} · {_pct(force_passed, reviewed)}%",
            sub_string="force-passes / verdicts",
        ),
        _kpi_item(
            "qc_pending",
            "QC Pending",
            pending,
            sub_string="awaiting review",
        ),
        _kpi_item(
            "total_tasks_done",
            "Total Tasks Done",
            done,
            sub_string="records reached Done",
        ),
        _kpi_item(
            "total_qc_approved",
            "Total QC Approved",
            approved,
            sub_string="approved by QL",
        ),
        _kpi_item(
            "my_qc_pass_ratio",
            "My QC Pass Ratio",
            f"{approval_rate}%",
            sub_string="first-pass quality",
        ),
        _kpi_item(
            "tasks_done_today",
            "Tasks Done Today",
            done_today,
            sub_string="today",
        ),
    ]
    return {"count": len(items), "items": items}


# "Stage Funnel" — the three pipeline stages a sourcing record moves through
# (Draft → Processed → Done), mapped from `state`. These are the funnel bars in
# the design; `conversion_pct` is Done/total and `rejected_rework` counts QC
# rejections (the "✕ N rejected / rework" drop line).
STAGE_FUNNEL = [
    ("draft", "Draft", ("draft",)),
    ("processed", "Processed", ("processing", "exporting", "processed")),
    ("done", "Done", ("exported",)),
]


def _compute_task_progress(env, gen_scope):
    Project = env["video.editor.project"].sudo()
    total = Project.search_count(gen_scope)
    items = []
    done_count = 0
    for key, label, states in STAGE_FUNNEL:
        count = Project.search_count(gen_scope + [("state", "in", list(states))])
        if key == "done":
            done_count = count
        items.append({
            "key": key,
            "label": label,
            "value": count,
            "percentage": _pct(count, total),
        })
    rejected_rework = Project.search_count(
        gen_scope + [("review_status", "=", "rejected")]
    )
    return {
        "label": "Stage Funnel",
        "total": total,
        "count": len(items),
        "items": items,
        "conversion_pct": _pct(done_count, total),
        "rejected_rework": rejected_rework,
    }


def _burn_series(env, gen_scope, days):
    Project = env["video.editor.project"].sudo()
    today = _today(env)
    start = today - timedelta(days=days - 1)
    start_dt = datetime.combine(start, datetime.min.time())
    rows = Project.search_read(
        gen_scope
        + [("llm_qc_cost_usd", ">", 0), ("llm_evaluated_at", ">=", start_dt)],
        fields=["llm_evaluated_at", "llm_qc_cost_usd"],
    )
    bucket = {}
    for row in rows:
        when = row.get("llm_evaluated_at")
        if not when:
            continue
        d = when.date() if isinstance(when, datetime) else when
        bucket[d] = bucket.get(d, 0.0) + (row.get("llm_qc_cost_usd") or 0.0)
    series = []
    for i in range(days):
        d = start + timedelta(days=i)
        series.append({"date": d.isoformat(), "amount": round(bucket.get(d, 0.0), 2)})
    return series


def _compute_budget(env, gen_scope):
    Project = env["video.editor.project"].sudo()
    cap = _budget_usd(env)
    spent = _spend(Project, gen_scope)
    remaining = round(max(cap - spent, 0.0), 2)
    approved = Project.search_count(gen_scope + [("review_status", "=", "approved")])
    avg_cost_per_pair = round(spent / approved, 2) if approved else 0.0

    series = _burn_series(env, gen_scope, BURN_DAYS)
    avg_daily = (sum(d["amount"] for d in series) / BURN_DAYS) if series else 0.0
    exhaustion = ""
    if avg_daily > 0 and remaining > 0:
        days_left = int(remaining / avg_daily)
        exhaustion = (_today(env) + timedelta(days=days_left)).isoformat()

    return {
        "title": "Budget vs Spend",
        "cap": cap,
        "spent": spent,
        "spent_pct": _pct(spent, cap),
        "remaining": remaining,
        "remaining_pct": _pct(remaining, cap),
        "avg_cost_per_accepted_pair": avg_cost_per_pair,
        "projected_exhaustion_date": exhaustion,
    }


def _compute_burn_rate(env, gen_scope):
    series = _burn_series(env, gen_scope, BURN_DAYS)
    today_amount = series[-1]["amount"] if series else 0.0
    last7 = series[-7:]
    avg_7d = round(sum(d["amount"] for d in last7) / len(last7), 2) if last7 else 0.0
    peak = max(series, key=lambda d: d["amount"]) if series else {"date": "", "amount": 0.0}
    return {
        "title": f"Burn Rate ({BURN_DAYS} days)",
        "chart_type": "bar_chart",
        "data": series,
        "today": today_amount,
        "avg_7d": avg_7d,
        "peak": {"date": peak["date"], "amount": peak["amount"]},
        "total": round(sum(d["amount"] for d in series), 2),
    }


def _accepted_category(row, category_labels):
    cat_id = row.get("category_id")
    if cat_id:
        return f"cat_{cat_id[0]}", cat_id[1] or "Uncategorized"
    slug = row.get("category")
    if slug:
        return slug, category_labels.get(slug, slug)
    return "uncategorized", "Uncategorized"


def _compute_accepted_per_day(env, gen_scope):
    # "Records Accepted per Day" — daily count of approved records, stacked
    # by category (top N categories + an "Other" bucket).
    Project = env["video.editor.project"].sudo()
    category_labels = dict(Project._fields["category"].selection)
    today = _today(env)
    start = today - timedelta(days=ACCEPTED_DAYS - 1)
    start_dt = datetime.combine(start, datetime.min.time())
    rows = Project.search_read(
        gen_scope
        + [
            ("review_status", "=", "approved"),
            ("review_decided_at", ">=", start_dt),
        ],
        fields=["review_decided_at", "category", "category_id"],
    )

    per_day = {}
    totals = {}
    labels = {}
    for row in rows:
        when = row.get("review_decided_at")
        if not when:
            continue
        day = when.date() if isinstance(when, datetime) else when
        key, label = _accepted_category(row, category_labels)
        labels[key] = label
        per_day.setdefault(day, {})
        per_day[day][key] = per_day[day].get(key, 0) + 1
        totals[key] = totals.get(key, 0) + 1

    ranked = sorted(totals, key=lambda k: (-totals[k], labels.get(k, k)))
    top = ranked[:ACCEPTED_TOP_CATEGORIES]
    top_set = set(top)

    legend = [
        {
            "key": key,
            "label": labels.get(key, key),
            "color_token": CATEGORY_COLOR_TOKENS[idx % len(CATEGORY_COLOR_TOKENS)],
        }
        for idx, key in enumerate(top)
    ]
    legend.append({"key": "other", "label": "Other", "color_token": "muted"})

    data = []
    for i in range(ACCEPTED_DAYS):
        day = start + timedelta(days=i)
        day_map = per_day.get(day, {})
        segments = [{"key": key, "value": day_map.get(key, 0)} for key in top]
        other_val = sum(c for k, c in day_map.items() if k not in top_set)
        segments.append({"key": "other", "value": other_val})
        data.append({
            "date": day.isoformat(),
            "label": day.strftime("%b %d"),
            "total": sum(seg["value"] for seg in segments),
            "segments": segments,
        })

    return {
        "title": "Records Accepted per Day",
        "chart_type": "stacked_bar",
        "legend": legend,
        "data": data,
        "total_accepted": sum(totals.values()),
    }


def _compute_recent_activity(env, gen_scope):
    Project = env["video.editor.project"].sudo()
    records = Project.search(
        gen_scope, order="write_date desc, id desc", limit=RECENT_ACTIVITY_LIMIT
    )
    items = []
    for rec in records:
        state = rec.state
        review_status = rec.review_status
        if review_status == "approved":
            action = "approved"
        elif review_status == "rejected":
            action = "rejected"
        elif state in DONE_STATES:
            action = "submitted"
        elif state in FAILED_STATES:
            action = "failed"
        elif state in IN_FLIGHT_STATES:
            action = "processing"
        else:
            action = "updated"
        actor = rec.assigned_to
        when = rec.write_date
        items.append({
            "actor_id": actor.id,
            "actor_name": actor.name or "",
            "actor_initials": _initials(actor.name),
            "action": action,
            "task_code": rec.name or "",
            "timestamp": when.isoformat() if when else "",
            "time_ago": _time_ago(env, when),
        })
    return {"label": "Recent Activity", "count": str(len(items)), "items": items}


# Which dashboard view each role sees (drives the role-specific block list).
VIEW_BY_ROLE = {"full": "manager", "pl": "manager", "qr": "ql", "tasker": "tasker"}

# KPI cards shown per view (only these are returned; one card per item).
KPI_KEYS_BY_VIEW = {
    "manager": ("total_burned", "tasks_done_qc", "approval_rate", "team_members"),
    "ql": ("total_qc_done", "approval_rate", "force_submit_rate", "qc_pending"),
    "tasker": (
        "total_tasks_done",
        "total_qc_approved",
        "my_qc_pass_ratio",
        "total_burned",
        "tasks_done_today",
    ),
}

# Which section blocks each view's page actually shows (per the Crowley
# Sourcing overview designs). Every section KEY is always present in the
# response; a section is filled with real data only when it belongs to this
# view's set and is returned blank ({}) otherwise — the key belongs to the
# schema, the data only fills in for the roles whose page displays it.
SECTIONS_BY_VIEW = {
    "manager": (
        "budget",
        "burn_rate",
        "accepted_per_day",
        "task_progress",
        "recent_activity",
    ),
    "ql": ("task_progress", "recent_activity"),
    "tasker": ("accepted_per_day", "recent_activity"),
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

        env = request.env
        role_tag, base_scope, _projects = _role_scope(env)
        gen_scope = base_scope + date_domain
        view = VIEW_BY_ROLE.get(role_tag, "tasker")

        # KPI section — only this view's cards (one item per card). The card
        # set differs per role per the Crowley overview designs.
        kpi_by_key = {
            item["key"]: item for item in _compute_kpi(env, gen_scope)["items"]
        }
        kpi_items = [
            kpi_by_key[key]
            for key in KPI_KEYS_BY_VIEW[view]
            if key in kpi_by_key
        ]

        # Single `overview` wrapper. Both Crowley extensions expose the SAME
        # 12-key schema so one frontend model fits both; a section is filled
        # with real data only when it belongs to this view's page
        # (SECTIONS_BY_VIEW) and is returned blank ({}) otherwise.
        # `approved_per_week`, `coordination_events`, `tasks_done_chart`,
        # `burned_amount_chart` and `my_activity` are crowley_extension-only
        # sections — kept here as always-blank keys for schema parity. KPI
        # items are the role-specific cards; `overview.role` tells the view.
        sections = SECTIONS_BY_VIEW[view]

        def _section(key, builder):
            return builder() if key in sections else {}

        overview = {
            "role": role_tag or "tasker",
            "kpi": {"count": len(kpi_items), "items": kpi_items},
            "budget": _section(
                "budget", lambda: _compute_budget(env, gen_scope)
            ),
            "burn_rate": _section(
                "burn_rate", lambda: _compute_burn_rate(env, gen_scope)
            ),
            "accepted_per_day": _section(
                "accepted_per_day",
                lambda: _compute_accepted_per_day(env, gen_scope),
            ),
            "task_progress": _section(
                "task_progress", lambda: _compute_task_progress(env, gen_scope)
            ),
            "approved_per_week": {},
            "recent_activity": _section(
                "recent_activity",
                lambda: _compute_recent_activity(env, gen_scope),
            ),
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
