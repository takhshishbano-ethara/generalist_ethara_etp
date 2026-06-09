from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard_overview import _scope_domain, _user_role_tag, _total_tokens

RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")
PASS_RATE_GOOD = 90.0
UNASSIGNED_QL = 0
UNASSIGNED_QL_LABEL = "Unassigned"

DONE_STATUSES = ("Submitted", "completed")
INPUT_FIELDS = (
    "claude_input_tokens", "glm_input_tokens",
    "oneP_input_tokens", "onePA_input_tokens", "onePB_input_tokens",
    "onePC_input_tokens", "onePD_input_tokens",
    "bedrock_input_tokens", "traj_qc_input_tokens",
    "taskdesc_input_tokens", "golden_input_tokens", "kimi_eval_input_tokens",
)
OUTPUT_FIELDS = tuple(f.replace("_input_tokens", "_output_tokens") for f in INPUT_FIELDS)
MODEL_GROUPS = (
    ("claude", ("claude_input_tokens", "claude_output_tokens")),
    ("glm", ("glm_input_tokens", "glm_output_tokens")),
    ("oneP", ("oneP_input_tokens", "oneP_output_tokens")),
    ("onePA", ("onePA_input_tokens", "onePA_output_tokens")),
    ("onePB", ("onePB_input_tokens", "onePB_output_tokens")),
    ("onePC", ("onePC_input_tokens", "onePC_output_tokens")),
    ("onePD", ("onePD_input_tokens", "onePD_output_tokens")),
    ("bedrock", ("bedrock_input_tokens", "bedrock_output_tokens")),
    ("traj_qc", ("traj_qc_input_tokens", "traj_qc_output_tokens")),
    ("taskdesc", ("taskdesc_input_tokens", "taskdesc_output_tokens")),
    ("golden", ("golden_input_tokens", "golden_output_tokens")),
    ("kimi_eval", ("kimi_eval_input_tokens", "kimi_eval_output_tokens")),
)


def _money(value):
    return f"${value or 0.0:,.2f}"


def _pct1(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 1)


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _color(index):
    return COLOR_TOKENS[index % len(COLOR_TOKENS)]


def _task_cost_usd(task):
    return round(_total_tokens(task) / 1000.0, 4)


def _resolve_range(params):
    today = fields.Datetime.now().date()

    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()
    if raw_start or raw_end:
        try:
            start = (
                datetime.strptime(raw_start, "%Y-%m-%d").date()
                if raw_start else today - timedelta(days=29)
            )
            end = (
                datetime.strptime(raw_end, "%Y-%m-%d").date()
                if raw_end else today
            )
        except ValueError:
            return None, return_Response(
                message="Invalid start_date/end_date. Expected YYYY-MM-DD.",
                status=400,
            )
        if start > end:
            return None, return_Response(
                message="start_date must be on or before end_date.",
                status=400,
            )
        return {"key": "custom", "start": start, "end": end}, None

    range_key = (params.get("range") or DEFAULT_RANGE).strip().lower()
    if range_key not in RANGE_DAYS:
        return None, return_Response(
            message=f"Invalid range '{range_key}'. Allowed: {', '.join(RANGE_DAYS)}.",
            status=400,
        )
    days = RANGE_DAYS[range_key]
    return {
        "key": range_key,
        "start": today - timedelta(days=days - 1),
        "end": today,
    }, None


def _range_domain(rng):
    start_dt = datetime.combine(rng["start"], datetime.min.time())
    end_dt = datetime.combine(rng["end"], datetime.min.time()) + timedelta(days=1)
    return [("create_date", ">=", start_dt), ("create_date", "<", end_dt)]


def _range_label(rng):
    if rng["key"] == "custom":
        return f"{rng['start'].isoformat()} → {rng['end'].isoformat()}"
    return {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days"}[rng["key"]]


def _resolve_context(env, params):
    rng, error = _resolve_range(params)
    if error is not None:
        return None, error
    scope = _scope_domain(env) + _range_domain(rng)
    return {"rng": rng, "scope": scope}, None


def _build_kpi(env, ctx):
    Talos = env["talos.talos"].sudo()
    scope = ctx["scope"]

    records = Talos.search(scope)
    total_tasks = len(records)

    total_tokens = sum(_total_tokens(t) for t in records)
    total_spend = round(total_tokens / 1000.0, 4)
    avg_per_task = (total_spend / total_tasks) if total_tasks else 0.0

    approved = Talos.search_count(scope + [("qc_status", "=", "passed")])
    rejected = Talos.search_count(scope + [("qc_status", "=", "failed")])
    reviewed = approved + rejected
    pass_rate = _pct1(approved, reviewed)

    avg_tokens = round(total_tokens / total_tasks) if total_tasks else 0

    durations = []
    for rec in records:
        if rec.write_date and rec.create_date and rec.task_status == "completed":
            durations.append((rec.write_date - rec.create_date).total_seconds())
    avg_wall = (sum(durations) / len(durations)) if durations else 0.0

    items = [
        {
            "key": "total_spend",
            "label": "Total Spend",
            "value": _money(total_spend),
            "sub_string": (
                f"Avg {_money(avg_per_task)}/task · {total_tasks} generations"
            ),
            "pattern": "",
            "sign": "",
        },
        {
            "key": "qc_pass_rate",
            "label": "QC Pass Rate",
            "value": f"{pass_rate}%",
            "sub_string": (
                f"{approved} of {reviewed} reviewed" if reviewed else "No reviews yet"
            ),
            "pattern": "badge" if reviewed else "",
            "sign": "+" if pass_rate >= PASS_RATE_GOOD else "-" if reviewed else "",
        },
        {
            "key": "avg_tokens_per_task",
            "label": "Avg Tokens / Task",
            "value": avg_tokens,
            "sub_string": "per generation",
            "pattern": "",
            "sign": "",
        },
        {
            "key": "avg_wall_time",
            "label": "Avg Wall Time",
            "value": f"{round(avg_wall)}s",
            "sub_string": f"{round(avg_wall / 60.0, 1)} min per generation",
            "pattern": "",
            "sign": "",
        },
    ]
    return {"count": len(items), "items": items}


def _build_spend_by_category(env, ctx):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(ctx["scope"])
    amount_by_cat = {}
    for rec in records:
        key = rec.task_type or "unassigned"
        amount_by_cat[key] = amount_by_cat.get(key, 0.0) + _task_cost_usd(rec)
    total = round(sum(amount_by_cat.values()), 4)

    selection = list(Talos._fields["task_type"].selection or [])
    seen = {key for key, _ in selection}
    for extra_key in amount_by_cat:
        if extra_key not in seen:
            selection.append(
                (extra_key, extra_key.replace("_", " ").title() if extra_key else "Unassigned")
            )

    ordered = sorted(selection, key=lambda kv: (-amount_by_cat.get(kv[0], 0.0), kv[1]))
    items = []
    for idx, (key, label) in enumerate(ordered):
        amount = round(amount_by_cat.get(key, 0.0), 4)
        percentage = _pct1(amount, total)
        items.append({
            "key": key,
            "label": label,
            "value": f"{_money(amount)} ({percentage:.0f}%)",
            "amount": amount,
            "percentage": percentage,
            "color_token": _color(idx),
        })

    return {
        "title": "Spend by Category",
        "sub_title": _range_label(ctx["rng"]),
        "type": "horizontal_bar",
        "total": total,
        "items": items,
    }


def _build_qc_pass_rate_by_ql(env, ctx):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(ctx["scope"])
    by_user = {}
    for rec in records:
        uid = rec.user_id.id or UNASSIGNED_QL
        name = rec.user_id.name or UNASSIGNED_QL_LABEL
        bucket = by_user.setdefault(uid, {"name": name, "tasks": 0, "approved": 0, "reviewed": 0})
        bucket["tasks"] += 1
        if rec.qc_status == "passed":
            bucket["approved"] += 1
            bucket["reviewed"] += 1
        elif rec.qc_status == "failed":
            bucket["reviewed"] += 1

    if UNASSIGNED_QL not in by_user:
        by_user[UNASSIGNED_QL] = {
            "name": UNASSIGNED_QL_LABEL, "tasks": 0, "approved": 0, "reviewed": 0,
        }

    items = []
    for uid, info in by_user.items():
        rate = _pct1(info["approved"], info["reviewed"])
        items.append({
            "key": uid,
            "label": info["name"],
            "initials": _initials(info["name"]),
            "value": f"{rate}% · {info['tasks']} tasks",
            "pass_rate": rate,
            "tasks": info["tasks"],
            "reviewed": info["reviewed"],
            "taskers": 1,
            "color_token": "success" if rate >= PASS_RATE_GOOD else "warn",
        })

    items.sort(key=lambda i: (-i["pass_rate"], -i["tasks"], i["label"]))
    return {
        "title": "QC Pass Rate by QL",
        "sub_title": "",
        "type": "horizontal_bar",
        "items": items,
    }


def _build_daily_burn_rate(env, ctx):
    Talos = env["talos.talos"].sudo()
    rng = ctx["rng"]

    records = Talos.search(ctx["scope"])
    per_day = {}
    per_ql_total = {}
    ql_ids = set()
    ql_name = {UNASSIGNED_QL: UNASSIGNED_QL_LABEL}

    for rec in records:
        ql_id = rec.user_id.id or UNASSIGNED_QL
        ql_name[ql_id] = rec.user_id.name or UNASSIGNED_QL_LABEL
        ql_ids.add(ql_id)
        cost = _task_cost_usd(rec)
        day = rec.create_date.date() if rec.create_date else None
        if not day:
            continue
        per_day.setdefault(day, {})
        per_day[day][ql_id] = per_day[day].get(ql_id, 0.0) + cost
        per_ql_total[ql_id] = per_ql_total.get(ql_id, 0.0) + cost

    color_by_ql = {ql_id: _color(idx) for idx, ql_id in enumerate(sorted(ql_ids))}
    data = []
    cursor = rng["start"]
    grand_total = 0.0
    while cursor <= rng["end"]:
        day_map = per_day.get(cursor, {})
        day_total = round(sum(day_map.values()), 4)
        grand_total += day_total
        data.append({
            "date": cursor.isoformat(),
            "total": day_total,
            "segments": [
                {"key": ql_id, "value": round(day_map.get(ql_id, 0.0), 4)}
                for ql_id in sorted(ql_ids)
                if day_map.get(ql_id)
            ],
        })
        cursor += timedelta(days=1)

    legend = [
        {
            "key": ql_id,
            "label": ql_name.get(ql_id, UNASSIGNED_QL_LABEL),
            "value": _money(per_ql_total.get(ql_id, 0.0)),
            "amount": round(per_ql_total.get(ql_id, 0.0), 4),
            "color_token": color_by_ql.get(ql_id, "primary"),
        }
        for ql_id in sorted(ql_ids, key=lambda q: -per_ql_total.get(q, 0.0))
    ]

    return {
        "title": "Daily Burn Rate",
        "sub_title": "",
        "type": "stacked_bar",
        "headline": _money(grand_total),
        "headline_caption": _range_label(rng),
        "legend": legend,
        "data": data,
    }


def _build_persona_burn_rate(env, ctx):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(ctx["scope"])
    per_persona = {}
    for rec in records:
        key = rec.persona_id.name or "unassigned"
        per_persona[key] = per_persona.get(key, 0.0) + _task_cost_usd(rec)
    total = round(sum(per_persona.values()), 4)
    items = []
    for idx, (key, amount) in enumerate(
        sorted(per_persona.items(), key=lambda kv: -kv[1])
    ):
        percentage = _pct1(amount, total)
        items.append({
            "key": key,
            "label": key,
            "value": f"{_money(amount)} ({percentage:.0f}%)",
            "amount": round(amount, 4),
            "percentage": percentage,
            "color_token": _color(idx),
        })
    return {
        "title": "Spend by Persona",
        "sub_title": _range_label(ctx["rng"]),
        "type": "horizontal_bar",
        "total": total,
        "items": items,
    }


def _build_model_token_usage(env, ctx):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(ctx["scope"])
    items = []
    grand_total = 0
    for idx, (model_key, fields_pair) in enumerate(MODEL_GROUPS):
        in_field, out_field = fields_pair
        in_total = sum(getattr(r, in_field, 0) or 0 for r in records)
        out_total = sum(getattr(r, out_field, 0) or 0 for r in records)
        tokens = in_total + out_total
        grand_total += tokens
        items.append({
            "key": model_key,
            "label": model_key,
            "input_tokens": in_total,
            "output_tokens": out_total,
            "total_tokens": tokens,
            "color_token": _color(idx),
        })
    for item in items:
        item["percentage"] = _pct1(item["total_tokens"], grand_total)
    items.sort(key=lambda i: -i["total_tokens"])
    return {
        "title": "Token Usage by Model",
        "sub_title": _range_label(ctx["rng"]),
        "type": "horizontal_bar",
        "total_tokens": grand_total,
        "items": items,
    }


def _build_golden_status_breakdown(env, ctx):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(ctx["scope"])
    states = ("idle", "generating", "done", "error")
    items = []
    for idx, key in enumerate(states):
        count = Talos.search_count(ctx["scope"] + [("golden_status", "=", key)])
        items.append({
            "key": key,
            "label": key.title(),
            "value": count,
            "percentage": _pct1(count, total),
            "color_token": _color(idx),
        })
    return {
        "title": "Golden Generation Breakdown",
        "sub_title": _range_label(ctx["rng"]),
        "type": "horizontal_bar",
        "total": total,
        "items": items,
    }


class TalosAnalyticsDashboardController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos analytics.",
                status=403,
            )
        ctx, error = _resolve_context(env, request.params or {})
        if error is not None:
            return error

        return return_Response(
            message="OK",
            status=200,
            data={
                "kpi": _build_kpi(env, ctx),
                "spend_by_category": _build_spend_by_category(env, ctx),
                "qc_pass_rate_by_ql": _build_qc_pass_rate_by_ql(env, ctx),
                "daily_burn_rate": _build_daily_burn_rate(env, ctx),
                "persona_burn_rate": _build_persona_burn_rate(env, ctx),
                "model_token_usage": _build_model_token_usage(env, ctx),
                "golden_status_breakdown": _build_golden_status_breakdown(env, ctx),
            },
        )
