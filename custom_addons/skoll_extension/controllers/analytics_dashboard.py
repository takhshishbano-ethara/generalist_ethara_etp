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

_logger = logging.getLogger(__name__)


COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")
PASS_RATE_GOOD = 90.0
UNASSIGNED_QL = 0
UNASSIGNED_QL_LABEL = "Unassigned"
UNCATEGORIZED_KEY = "uncategorized"
UNCATEGORIZED_LABEL = "Uncategorized"
DONE_STATE = "passed"
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}

APPROVED_STATES = ("passed",)
REWORK_STATES = ("failed",)
DECIDED_STATES = APPROVED_STATES + REWORK_STATES
IN_PROGRESS_STATES = ()
ERROR_STATES = ("failed",)


def _user_role_tag(env):
    user = env.user
    if user.has_group("etp_user_roles.group_project_lead"):
        return "full"
    if user.has_group("etp_user_roles.group_quality_lead"):
        return "ql"
    if user.has_group("etp_user_roles.group_tasker"):
        return "tasker"
    return None


def _scope(env):
    tag = _user_role_tag(env)
    Persona = env["skoll.persona"].sudo()
    if tag in ("full", "ql"):
        return tag, [], Persona.search([])
    if tag == "tasker":
        uid = env.user.id
        domain = [("employee_id.user_id", "=", uid)]
        tasks = env["skoll.skoll"].sudo().search(domain)
        personas = tasks.persona_id
        return tag, domain, personas
    return None, [("id", "=", 0)], Persona.browse()


def _gen_scope(task_domain):
    out = []
    for leaf in task_domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
            field, op, val = leaf
            out.append((f"task_id.{field}", op, val))
        else:
            out.append(leaf)
    return out


def _money(value):
    try:
        return f"${float(value or 0.0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _pct1(part, whole):
    try:
        whole = float(whole or 0)
        if whole <= 0:
            return 0.0
        return round(float(part or 0) * 100.0 / whole, 1)
    except (TypeError, ValueError):
        return 0.0


def _initials(name):
    if not name:
        return "?"
    parts = [p for p in str(name).strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _color(idx):
    return COLOR_TOKENS[idx % len(COLOR_TOKENS)]


def _parse_date(raw):
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _create_date_domain(start, end):
    dom = []
    if start:
        dom.append(("create_date", ">=", datetime.combine(start, time.min)))
    if end:
        dom.append(("create_date", "<=", datetime.combine(end, time.max)))
    return dom


def _resolve_range(params):
    raw_start = _parse_date(params.get("start_date"))
    raw_end = _parse_date(params.get("end_date"))
    preset = (params.get("range") or "").strip().lower()
    today = fields.Date.context_today(request.env.user)

    if raw_start and raw_end and raw_start > raw_end:
        raw_start, raw_end = raw_end, raw_start

    if raw_start or raw_end:
        return {
            "start": raw_start,
            "end": raw_end,
            "label": _range_label({"start": raw_start, "end": raw_end, "preset": None}),
            "preset": None,
        }

    days = RANGE_DAYS.get(preset)
    if days:
        start = today - timedelta(days=days - 1)
        return {
            "start": start,
            "end": today,
            "label": f"Last {days} days",
            "preset": preset,
        }

    return {"start": None, "end": None, "label": "All time", "preset": None}


def _range_domain(rng):
    return _create_date_domain(rng.get("start"), rng.get("end"))


def _range_label(rng):
    start = rng.get("start")
    end = rng.get("end")
    if rng.get("preset") and RANGE_DAYS.get(rng["preset"]):
        return f"Last {RANGE_DAYS[rng['preset']]} days"
    if start and end:
        return f"{start.isoformat()} → {end.isoformat()}"
    if start:
        return f"Since {start.isoformat()}"
    if end:
        return f"Until {end.isoformat()}"
    return "All time"


def _resolve_context(env, params):
    tag, task_domain, personas = _scope(env)
    rng = _resolve_range(params)
    date_dom = _range_domain(rng)
    task_scope = task_domain + date_dom
    gen_scope = _gen_scope(task_domain) + _gen_scope(date_dom)

    Task = env["skoll.skoll"].sudo()
    Generation = env["skoll.generation"].sudo()

    tasks = Task.search(task_scope)

    return {
        "tag": tag,
        "task_domain": task_domain,
        "task_scope": task_scope,
        "gen_scope": gen_scope,
        "rng": rng,
        "personas": personas,
        "tasks": tasks,
        "Task": Task,
        "Generation": Generation,
    }


def _build_kpi_v2(env, ctx):
    Generation = ctx["Generation"]
    Task = ctx["Task"]
    tasks = ctx["tasks"]
    gen_scope = ctx["gen_scope"]

    total_generations = Generation.search_count(gen_scope)
    cost_groups = Generation.read_group(gen_scope, ["total_cost:sum"], [])
    total_spend = float((cost_groups[0]["total_cost"] if cost_groups else 0.0) or 0.0)

    task_count = len(tasks)
    avg_per_task = (total_spend / task_count) if task_count else 0.0

    approved_count = Task.search_count(
        ctx["task_scope"] + [("qc_status", "in", list(APPROVED_STATES))]
    )
    rework_count = Task.search_count(
        ctx["task_scope"] + [("qc_status", "in", list(REWORK_STATES))]
    )
    reviewed_count = approved_count + rework_count
    pass_rate = _pct1(approved_count, reviewed_count) if reviewed_count else 0.0

    tokens_groups = Generation.read_group(gen_scope, ["total_tokens:sum"], [])
    total_tokens = int((tokens_groups[0]["total_tokens"] if tokens_groups else 0) or 0)
    avg_tokens = int(round(total_tokens / task_count)) if task_count else 0

    duration_groups = Generation.read_group(
        gen_scope, ["duration_s:sum"], []
    )
    total_duration = float(
        (duration_groups[0]["duration_s"] if duration_groups else 0.0) or 0.0
    )
    avg_wall_seconds = (total_duration / total_generations) if total_generations else 0.0
    avg_wall_minutes = avg_wall_seconds / 60.0

    items = [
        {
            "key": "total_spend",
            "label": "Total Spend",
            "value": _money(total_spend),
            "sub_string": (
                f"Avg {_money(avg_per_task)}/task · {total_generations} generations"
            ),
            "pattern": "neutral",
            "sign": "",
        },
        {
            "key": "qc_pass_rate",
            "label": "QC Pass Rate",
            "value": f"{pass_rate}%",
            "sub_string": f"{approved_count}/{reviewed_count} approved",
            "pattern": "up" if pass_rate >= PASS_RATE_GOOD else "down" if reviewed_count else "neutral",
            "sign": "+" if pass_rate >= PASS_RATE_GOOD else "-" if reviewed_count else "",
        },
        {
            "key": "avg_tokens_per_task",
            "label": "Avg Tokens / Task",
            "value": f"{avg_tokens:,}",
            "sub_string": f"{total_tokens:,} total tokens",
            "pattern": "neutral",
            "sign": "",
        },
        {
            "key": "avg_wall_time",
            "label": "Avg Wall Time",
            "value": f"{avg_wall_seconds:.1f}s",
            "sub_string": f"{avg_wall_minutes:.1f} min per generation",
            "pattern": "neutral",
            "sign": "",
        },
    ]
    return {"count": str(len(items)), "items": items}


def _build_spend_by_category(env, ctx):
    Generation = ctx["Generation"]
    rng = ctx["rng"]

    gen_rows = Generation.search_read(
        ctx["gen_scope"],
        ["task_id", "total_cost"],
    )

    task_ids = {row["task_id"][0] for row in gen_rows if row.get("task_id")}
    tasks = env["skoll.skoll"].sudo().browse(list(task_ids))
    task_to_label = {}
    task_to_key = {}
    for task in tasks:
        if task.life_domain_ids:
            tag = task.life_domain_ids[0]
            task_to_label[task.id] = tag.name or UNCATEGORIZED_LABEL
            task_to_key[task.id] = f"life_domain_{tag.id}"
        else:
            task_to_label[task.id] = UNCATEGORIZED_LABEL
            task_to_key[task.id] = UNCATEGORIZED_KEY

    buckets = defaultdict(lambda: {"label": UNCATEGORIZED_LABEL, "amount": 0.0})
    total = 0.0
    for row in gen_rows:
        tid = row["task_id"][0] if row.get("task_id") else None
        cost = float(row.get("total_cost") or 0.0)
        if tid is None:
            key = UNCATEGORIZED_KEY
            label = UNCATEGORIZED_LABEL
        else:
            key = task_to_key.get(tid, UNCATEGORIZED_KEY)
            label = task_to_label.get(tid, UNCATEGORIZED_LABEL)
        buckets[key]["label"] = label
        buckets[key]["amount"] += cost
        total += cost

    items = []
    for idx, (key, payload) in enumerate(
        sorted(buckets.items(), key=lambda kv: kv[1]["amount"], reverse=True)
    ):
        amount = payload["amount"]
        percentage = _pct1(amount, total)
        items.append(
            {
                "key": key,
                "label": payload["label"],
                "value": f"{_money(amount)} ({percentage}%)",
                "amount": round(amount, 2),
                "percentage": percentage,
                "color_token": _color(idx),
            }
        )

    return {
        "title": "Spend by Category",
        "sub_title": _range_label(rng),
        "type": "horizontal_bar",
        "total": round(total, 2),
        "items": items,
    }


def _build_pass_rate_by_ql(env, ctx):
    Task = ctx["Task"]
    rng = ctx["rng"]
    tasks = ctx["tasks"]

    buckets = defaultdict(
        lambda: {
            "label": UNASSIGNED_QL_LABEL,
            "pass_count": 0,
            "rework_count": 0,
            "reviewed_count": 0,
            "total": 0,
            "taskers": set(),
        }
    )
    for task in tasks:
        if task.persona_id:
            key = task.persona_id.id
            label = task.persona_id.name or UNASSIGNED_QL_LABEL
        else:
            key = UNASSIGNED_QL
            label = UNASSIGNED_QL_LABEL
        bucket = buckets[key]
        bucket["label"] = label
        bucket["total"] += 1
        if task.qc_status in APPROVED_STATES:
            bucket["pass_count"] += 1
            bucket["reviewed_count"] += 1
        elif task.qc_status in REWORK_STATES:
            bucket["rework_count"] += 1
            bucket["reviewed_count"] += 1
        if task.employee_id and task.employee_id.user_id:
            bucket["taskers"].add(task.employee_id.user_id.id)

    items = []
    for idx, (key, payload) in enumerate(
        sorted(buckets.items(), key=lambda kv: kv[1]["reviewed_count"], reverse=True)
    ):
        reviewed = payload["reviewed_count"]
        pass_rate = _pct1(payload["pass_count"], reviewed) if reviewed else 0.0
        items.append(
            {
                "key": key,
                "label": payload["label"],
                "initials": _initials(payload["label"]),
                "value": f"{pass_rate}% · {payload['total']} tasks",
                "pass_rate": pass_rate,
                "tasks": payload["total"],
                "reviewed": reviewed,
                "taskers": len(payload["taskers"]),
                "color_token": _color(idx),
            }
        )

    return {
        "title": "QC Pass Rate by QL",
        "sub_title": "",
        "type": "horizontal_bar",
        "items": items,
    }


def _build_daily_burn_rate(env, ctx):
    Generation = ctx["Generation"]
    rng = ctx["rng"]

    gen_rows = Generation.search_read(
        ctx["gen_scope"],
        ["create_date", "total_cost", "persona_id"],
    )

    persona_totals = defaultdict(float)
    daily = defaultdict(lambda: defaultdict(float))
    total = 0.0
    for row in gen_rows:
        cd = row.get("create_date")
        if not cd:
            continue
        if isinstance(cd, str):
            try:
                cd = fields.Datetime.from_string(cd)
            except Exception:
                continue
        day = cd.date().isoformat()
        cost = float(row.get("total_cost") or 0.0)
        if row.get("persona_id"):
            pid, pname = row["persona_id"]
            seg_key = f"persona_{pid}"
            seg_label = pname or UNASSIGNED_QL_LABEL
        else:
            seg_key = "persona_unassigned"
            seg_label = UNASSIGNED_QL_LABEL
        persona_totals[(seg_key, seg_label)] += cost
        daily[day][seg_key] += cost
        total += cost

    legend = []
    for idx, ((seg_key, seg_label), amount) in enumerate(
        sorted(persona_totals.items(), key=lambda kv: kv[1], reverse=True)
    ):
        legend.append(
            {
                "key": seg_key,
                "label": seg_label,
                "value": _money(amount),
                "amount": round(amount, 2),
                "color_token": _color(idx),
            }
        )

    data = []
    for day in sorted(daily.keys()):
        segments = [
            {"key": entry["key"], "value": round(daily[day].get(entry["key"], 0.0), 2)}
            for entry in legend
        ]
        data.append(
            {
                "date": day,
                "total": round(sum(s["value"] for s in segments), 2),
                "segments": segments,
            }
        )

    return {
        "title": "Daily Burn Rate",
        "sub_title": "",
        "type": "stacked_bar",
        "headline": _money(total),
        "headline_caption": _range_label(rng),
        "legend": legend,
        "data": data,
    }


def _build_analytics(env, ctx):
    return {
        "kpi": _build_kpi_v2(env, ctx),
        "spend_by_category": _build_spend_by_category(env, ctx),
        "qc_pass_rate_by_ql": _build_pass_rate_by_ql(env, ctx),
        "daily_burn_rate": _build_daily_burn_rate(env, ctx),
    }


class SkollAnalyticsDashboardController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def analytics_dashboard(self, **kwargs):
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            ctx = _resolve_context(env, kwargs)
            data = _build_analytics(env, ctx)
        except Exception as exc:
            _logger.exception("Skoll analytics_dashboard failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(message="OK", status=200, data=data)
