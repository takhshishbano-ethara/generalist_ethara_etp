import calendar
from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

COMPLETED_STATES = ("Submitted",)
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEATMAP_INTENSITY_LEVELS = 4
LEADERBOARD_WINDOW_DAYS = 30
TIMELINE_WINDOW_DAYS = 30

CACHE_TTL = 60
CACHE_MAX_ENTRIES = 256
_CACHE = {}

FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)

PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)

QR_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

TASKER_ROLE_XMLIDS = (
    "api_auth_gateway.role_tasker_technical",
    "api_auth_gateway.role_tasker_stem",
    "api_auth_gateway.role_tasker_non_stem",
)

TALOS_USER_ROLE_XMLIDS = (
    FULL_ACCESS_ROLE_XMLIDS
    + PL_ROLE_XMLIDS
    + QR_ROLE_XMLIDS
    + TASKER_ROLE_XMLIDS
)

TOKEN_FIELDS = (
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


def _total_tokens(rec):
    return sum(int(getattr(rec, f, 0) or 0) for f in TOKEN_FIELDS)


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _diff_pct(current, previous):
    if not previous:
        return 100.0 if current else 0.0
    return round(((current - previous) / previous) * 100.0, 2)


def _fmt_duration(seconds):
    total = int(round(seconds or 0))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _user_has_role(env, xmlids):
    role = env.user.user_role
    if not role:
        return False
    return role.id in _get_role_ids(env, xmlids)


def _user_role_tag(env):
    if _user_has_role(env, FULL_ACCESS_ROLE_XMLIDS):
        return "full"
    if _user_has_role(env, PL_ROLE_XMLIDS):
        return "pl"
    if _user_has_role(env, QR_ROLE_XMLIDS):
        return "qr"
    if _user_has_role(env, TASKER_ROLE_XMLIDS):
        return "tasker"
    return None


def _scope(env):
    tag = _user_role_tag(env)
    user = env.user
    Project = env["project.project"].sudo() if "project.project" in env else None
    empty_projects = Project.browse() if Project is not None else None
    if tag in ("full", "pl", "qr"):
        return tag, [], empty_projects
    if tag == "tasker":
        return "tasker", [("user_id", "=", user.id)], empty_projects
    return None, [("id", "=", 0)], empty_projects


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _create_date_domain(start_date, end_date):
    domain = []
    if start_date:
        domain.append(
            ("create_date", ">=", datetime.combine(start_date, datetime.min.time()))
        )
    if end_date:
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end_date, datetime.min.time()) + timedelta(days=1),
            )
        )
    return domain


def _resolve_dashboard_filters(params):
    start = end = month_start = month_end = None

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        start, error = _parse_date(raw_start, "start_date")
        if error is not None:
            return None, error

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        end, error = _parse_date(raw_end, "end_date")
        if error is not None:
            return None, error

    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )

    raw_month = (params.get("month") or "").strip()
    if raw_month:
        try:
            parsed = datetime.strptime(raw_month, "%Y-%m").date()
        except ValueError:
            return None, return_Response(
                message=f"Invalid month '{raw_month}'. Expected YYYY-MM.",
                status=400,
            )
        month_start = parsed.replace(day=1)
        month_end = parsed.replace(
            day=calendar.monthrange(parsed.year, parsed.month)[1]
        )

    return {
        "start": start,
        "end": end,
        "month_start": month_start,
        "month_end": month_end,
    }, None


def _period_windows(start, end):
    today = fields.Datetime.now().date()
    current_end = end or today
    current_start = start or (current_end - timedelta(days=6))
    length = (current_end - current_start).days + 1
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    return current_start, current_end, previous_start, previous_end


def _completed_day_counts(env, scope, win_start, win_end):
    start_dt = datetime.combine(win_start, datetime.min.time())
    end_dt = datetime.combine(win_end, datetime.min.time()) + timedelta(days=1)
    records = env["talos.talos"].sudo().search(
        scope
        + [
            ("task_status", "in", list(COMPLETED_STATES)),
            ("write_date", ">=", start_dt),
            ("write_date", "<", end_dt),
        ]
    )
    counts = {}
    for rec in records:
        when = rec.write_date
        if not when:
            continue
        day = when.date()
        counts[day] = counts.get(day, 0) + 1
    return counts


def _intensity(count, max_count):
    if not count or not max_count:
        return 0
    level = (count * HEATMAP_INTENSITY_LEVELS + max_count - 1) // max_count
    return min(level, HEATMAP_INTENSITY_LEVELS)


def _heatmap_window(filters):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        window_start = filters["start"] or window_end.replace(day=1)
        return window_start, window_end
    return today.replace(day=1), today


def _timeline_window(filters):
    if filters["month_start"]:
        return filters["month_start"], filters["month_end"]
    today = fields.Datetime.now().date()
    if filters["start"] or filters["end"]:
        window_end = filters["end"] or today
        window_start = filters["start"] or (
            window_end - timedelta(days=TIMELINE_WINDOW_DAYS - 1)
        )
        return window_start, window_end
    return today - timedelta(days=TIMELINE_WINDOW_DAYS - 1), today


def _build_total_task(env, scope, filters):
    Talos = env["talos.talos"].sudo()
    total = Talos.search_count(
        scope + _create_date_domain(filters["start"], filters["end"])
    )
    cur_start, cur_end, prev_start, prev_end = _period_windows(
        filters["start"], filters["end"]
    )
    current = Talos.search_count(scope + _create_date_domain(cur_start, cur_end))
    previous = Talos.search_count(scope + _create_date_domain(prev_start, prev_end))
    if current > previous:
        trend = "increase"
    elif current < previous:
        trend = "decrease"
    else:
        trend = "no_change"
    return {
        "total_task_count": total,
        "current_period_count": current,
        "previous_period_count": previous,
        "difference_percentage": _diff_pct(current, previous),
        "trend": trend,
        "current_period": {
            "start": cur_start.isoformat(),
            "end": cur_end.isoformat(),
        },
        "previous_period": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        },
    }


def _sum_tokens_for_domain(env, domain):
    records = env["talos.talos"].sudo().search(domain)
    counted = 0
    total = 0
    for rec in records:
        tokens = _total_tokens(rec)
        if tokens > 0:
            counted += 1
            total += tokens
    return counted, total


def _build_avg_cost(env, scope, filters):
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    counted, total = _sum_tokens_for_domain(env, domain)
    average = (total / counted) if counted else 0.0
    return {
        "tasks_with_cost": counted,
        "average_cost": round(average, 6),
        "total_cost": round(total, 6),
    }


def _build_avg_duration(env, scope, filters):
    return {
        "tasks_measured": 0,
        "average_duration_seconds": 0.0,
        "average_duration_minutes": 0.0,
        "average_duration_display": _fmt_duration(0),
    }


def _build_failed_task(env, scope, filters):
    Talos = env["talos.talos"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    total = Talos.search_count(base)
    failed = Talos.search_count(
        base + ["|", ("qc_status", "=", "failed"), ("golden_status", "=", "error")]
    )
    return {
        "failed_task_count": failed,
        "total_task_count": total,
        "failure_percentage": _pct(failed, total),
    }


def _build_team_members(env, projects):
    Users = env["res.users"].sudo()
    role_breakdown = []
    member_ids = set()
    role_buckets = (
        ("team_lead", PL_ROLE_XMLIDS),
        ("qc_reviewer", QR_ROLE_XMLIDS),
        ("tasker", TASKER_ROLE_XMLIDS),
    )
    for role_key, xmlids in role_buckets:
        role_ids = _get_role_ids(env, xmlids)
        users = (
            Users.search([("user_role", "in", role_ids)])
            if role_ids
            else Users.browse()
        )
        role_breakdown.append({"role": role_key, "count": len(users)})
        member_ids.update(users.ids)
    return {
        "total_team_members": len(member_ids),
        "role_breakdown": role_breakdown,
    }


def _build_heatmap(env, scope, filters):
    window_start, window_end = _heatmap_window(filters)
    counts = _completed_day_counts(env, scope, window_start, window_end)
    max_count = max(counts.values()) if counts else 0
    days = []
    cursor = window_start
    while cursor <= window_end:
        count = counts.get(cursor, 0)
        days.append({
            "date": cursor.isoformat(),
            "weekday": cursor.weekday(),
            "weekday_label": WEEKDAY_LABELS[cursor.weekday()],
            "count": count,
            "intensity": _intensity(count, max_count),
        })
        cursor += timedelta(days=1)
    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "max_count": max_count,
        "total_completed": sum(counts.values()),
        "days": days,
    }


def _build_review_state(env, scope, filters):
    Talos = env["talos.talos"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Talos._read_group(domain, ["qc_status"], ["__count"])
    counts = {}
    no_state = 0
    for key, count in groups:
        count = count or 0
        if key:
            counts[key] = count
        else:
            no_state += count
    total = sum(counts.values()) + no_state
    distribution = [
        {
            "verdict_key": key,
            "verdict_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Talos._fields["qc_status"].selection
    ]
    return {
        "total_task_count": total,
        "no_verdict_count": no_state,
        "distribution": distribution,
    }


def _build_qc_leaderboard(env, projects, filters):
    Talos = env["talos.talos"].sudo()
    today = fields.Datetime.now().date()
    window_end = filters["end"] or today
    window_start = filters["start"] or (
        window_end - timedelta(days=LEADERBOARD_WINDOW_DAYS - 1)
    )
    date_domain = _create_date_domain(window_start, window_end)

    Users = env["res.users"].sudo()
    qr_role_ids = _get_role_ids(env, QR_ROLE_XMLIDS)
    tasker_role_ids = _get_role_ids(env, TASKER_ROLE_XMLIDS)
    tasker_users = (
        Users.search([("user_role", "in", tasker_role_ids)])
        if tasker_role_ids
        else Users.browse()
    )
    qr_users = (
        Users.search([("user_role", "in", qr_role_ids)])
        if qr_role_ids
        else Users.browse()
    )
    tasker_user_ids = tasker_users.ids

    base_domain = [("user_id", "in", tasker_user_ids)] + date_domain
    total_by_user = {}
    completed_by_user = {}
    if tasker_user_ids:
        for user, count in Talos._read_group(
            base_domain, ["user_id"], ["__count"]
        ):
            if user:
                total_by_user[user.id] = count
        for user, count in Talos._read_group(
            base_domain + [("task_status", "in", list(COMPLETED_STATES))],
            ["user_id"],
            ["__count"],
        ):
            if user:
                completed_by_user[user.id] = count

    leaderboard = []
    if qr_users:
        for qc_user in qr_users:
            leaderboard.append({
                "qc_id": qc_user.id,
                "qc_name": qc_user.name or "",
                "total_taskers": len(tasker_user_ids),
                "tasks_completed": sum(completed_by_user.values()),
                "tasks_total": sum(total_by_user.values()),
                "completion_percentage": _pct(
                    sum(completed_by_user.values()), sum(total_by_user.values())
                ),
            })
    else:
        leaderboard.append({
            "qc_id": 0,
            "qc_name": "Unassigned",
            "total_taskers": len(tasker_user_ids),
            "tasks_completed": sum(completed_by_user.values()),
            "tasks_total": sum(total_by_user.values()),
            "completion_percentage": _pct(
                sum(completed_by_user.values()), sum(total_by_user.values())
            ),
        })

    leaderboard.sort(
        key=lambda row: (
            -row["tasks_completed"],
            -row["completion_percentage"],
            row["qc_name"],
        )
    )
    for rank, row in enumerate(leaderboard, start=1):
        row["rank"] = rank

    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "leaderboard": leaderboard,
    }


def _build_timeline(env, scope, filters):
    window_start, window_end = _timeline_window(filters)
    counts = _completed_day_counts(env, scope, window_start, window_end)
    trend = []
    cursor = window_start
    while cursor <= window_end:
        trend.append({
            "label": cursor.isoformat(),
            "count": counts.get(cursor, 0),
        })
        cursor += timedelta(days=1)
    return {
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "total_completed": sum(counts.values()),
        "trend": trend,
    }


def _cache_key(env, filters):
    return (
        env.cr.dbname,
        env.user.id,
        filters["start"].isoformat() if filters["start"] else "",
        filters["end"].isoformat() if filters["end"] else "",
        filters["month_start"].isoformat() if filters["month_start"] else "",
    )


def _cache_get(key):
    entry = _CACHE.get(key)
    if not entry:
        return None
    cached_at, payload = entry
    if (datetime.now() - cached_at).total_seconds() > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key, payload):
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        _CACHE.clear()
    _CACHE[key] = (datetime.now(), payload)


def _build_aht_overview_aligned(env, scope, filters):
    target = round(filters.get("target_aht", 0) or 0, 2)
    return {
        "average_handling_time_minutes": 0.0,
        "target_aht_minutes": target,
        "difference_minutes": round(0.0 - target, 2),
        "performance_indicator": "no_data",
        "tasks_measured": 0,
    }


def _build_video_overview_aligned(env, scope, filters):
    Talos = env["talos.talos"].sudo()
    base = scope + _create_date_domain(filters["start"], filters["end"])
    return {
        "tasks_with_video": Talos.search_count(
            base + [("golden_status", "=", "done")]
        ),
        "rejected_at_validation": Talos.search_count(
            base + [("qc_status", "=", "failed")]
        ),
    }


def _build_team_overview_aligned(env, projects):
    Users = env["res.users"].sudo()

    def _users_by_roles(xmlids):
        role_ids = _get_role_ids(env, xmlids)
        return (
            Users.search([("user_role", "in", role_ids)])
            if role_ids
            else Users.browse()
        )

    tpm_users = _users_by_roles(("api_auth_gateway.role_tpm_technical",))
    pl_users = _users_by_roles(PL_ROLE_XMLIDS)
    qr_users = _users_by_roles(QR_ROLE_XMLIDS)
    tasker_users = _users_by_roles(TASKER_ROLE_XMLIDS)
    members = tpm_users | pl_users | qr_users | tasker_users
    return {
        "total_team_size": len(members),
        "role_counts": {
            "tpm": len(tpm_users),
            "pl": len(pl_users),
            "qr": len(qr_users),
            "tasker": len(tasker_users),
            "aire": 0,
            "swe": 0,
        },
    }


def _build_status_chart_aligned(env, scope, filters):
    Talos = env["talos.talos"].sudo()
    domain = scope + _create_date_domain(filters["start"], filters["end"])
    groups = Talos._read_group(domain, ["task_status"], ["__count"])
    counts = {}
    for key, count in groups:
        if key:
            counts[key] = count or 0
    total = sum(counts.values())
    chart = [
        {
            "status_key": key,
            "status_name": label,
            "count": counts.get(key, 0),
            "percentage": _pct(counts.get(key, 0), total),
        }
        for key, label in Talos._fields["task_status"].selection
    ]
    return {"total_task_count": total, "status_chart": chart}


DONE_STATE = "Submitted"
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
COLOR_TOKENS = ("primary", "success", "info", "warn", "danger", "muted")
PASS_RATE_GOOD = 90.0
UNASSIGNED_QL = 0
UNASSIGNED_QL_LABEL = "Unassigned"


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


def _resolve_range(params):
    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()

    start = end = None
    try:
        if raw_start:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        if raw_end:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
    except ValueError:
        return None, return_Response(
            message="Invalid start_date/end_date. Expected YYYY-MM-DD.",
            status=400,
        )

    if start and end and start > end:
        return None, return_Response(
            message="start_date must be on or before end_date.",
            status=400,
        )

    if not start and not end:
        return {"key": "all"}, None

    rng = {"key": "custom"}
    if start:
        rng["start"] = start
    if end:
        rng["end"] = end
    return rng, None


def _range_domain(rng):
    domain = []
    if "start" in rng:
        start_dt = datetime.combine(rng["start"], datetime.min.time())
        domain.append(("create_date", ">=", start_dt))
    if "end" in rng:
        end_dt = datetime.combine(rng["end"], datetime.min.time()) + timedelta(days=1)
        domain.append(("create_date", "<", end_dt))
    return domain


def _range_label(rng):
    key = rng.get("key")
    if key == "all":
        return "All time"
    if key == "custom":
        start = rng["start"].isoformat() if "start" in rng else "…"
        end = rng["end"].isoformat() if "end" in rng else "…"
        return f"{start} → {end}"
    return {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days"}[key]


def _visible_taskers(env, project):
    tag = _user_role_tag(env)
    Users = env["res.users"].sudo()
    tasker_role_ids = _get_role_ids(env, TASKER_ROLE_XMLIDS)
    if not tasker_role_ids:
        return Users.browse(), tag
    if tag in ("full", "pl", "qr"):
        return Users.search([("user_role", "in", tasker_role_ids)]), tag
    if tag == "tasker":
        return env.user, tag
    return Users.browse(), None


def _build_ql_maps(taskers):
    user_ids = []
    ql_of_user = {}
    ql_name = {UNASSIGNED_QL: UNASSIGNED_QL_LABEL}
    ql_taskers = {}
    for user in taskers:
        uid = user.id
        if not uid:
            continue
        ql_id = UNASSIGNED_QL
        ql_name[ql_id] = UNASSIGNED_QL_LABEL
        ql_of_user[uid] = ql_id
        ql_taskers.setdefault(ql_id, set()).add(uid)
        user_ids.append(uid)
    return {
        "user_ids": list(dict.fromkeys(user_ids)),
        "ql_of_user": ql_of_user,
        "ql_name": ql_name,
        "ql_taskers": ql_taskers,
    }


def _resolve_context(env, params):
    tag = _user_role_tag(env)
    if tag is None:
        return None, return_Response(
            message="You are not allowed to access Talos analytics.",
            status=403,
        )

    taskers, role = _visible_taskers(env, None)

    rng, error = _resolve_range(params)
    if error is not None:
        return None, error

    ctx = {"project": None, "role": role, "rng": rng, "taskers": taskers}
    ctx.update(_build_ql_maps(taskers))
    base_scope = []
    if tag == "tasker":
        base_scope = [("user_id", "=", env.user.id)]
    elif ctx["user_ids"]:
        base_scope = [("user_id", "in", ctx["user_ids"])]
    ctx["scope"] = base_scope + _range_domain(rng)
    return ctx, None


def _counts_by_user(Talos, scope, extra=None):
    rows = Talos.formatted_read_group(scope + (extra or []), ["user_id"], ["__count"])
    out = {}
    for r in rows:
        user = r["user_id"]
        if user:
            out[user[0]] = r["__count"]
    return out


def _kpi(key, label, value, sub_string="", pattern="", sign=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "sub_string": sub_string,
        "pattern": pattern,
        "sign": sign,
    }


def _attempt_scope(scope):
    out = []
    for leaf in scope:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
            field, op, value = leaf
            out.append((f"talos_id.{field}", op, value))
        else:
            out.append(leaf)
    return out


def _sum_turn_tokens(env, attempt_scope):
    turns = env["talos.turn"].sudo().search(attempt_scope)
    return sum(_total_tokens(t) for t in turns), len(turns)


def _build_kpi_v2(env, ctx):
    Talos = env["talos.talos"].sudo()
    scope = ctx["scope"]
    attempt_scope = _attempt_scope(scope)

    tasks_count = Talos.search_count(scope)

    total_spend, _turn_count = _sum_turn_tokens(env, attempt_scope)
    avg_per_task = (total_spend / tasks_count) if tasks_count else 0.0

    approved = Talos.search_count(scope + [("qc_status", "=", "passed")])
    reviewed = approved + Talos.search_count(scope + [("qc_status", "=", "failed")])
    pass_rate = _pct1(approved, reviewed)

    records = Talos.search(scope)
    token_totals = [_total_tokens(r) for r in records]
    non_zero_tokens = [v for v in token_totals if v > 0]
    avg_tokens = round(sum(non_zero_tokens) / len(non_zero_tokens)) if non_zero_tokens else 0

    avg_wall = 0.0

    items = [
        _kpi(
            "total_spend",
            "Total Spend",
            _money(total_spend),
            sub_string=f"Avg {_money(avg_per_task)}/task · {tasks_count} tasks",
        ),
        _kpi(
            "qc_pass_rate",
            "QC Pass Rate",
            f"{pass_rate}%",
            sub_string=(
                f"{approved} of {reviewed} reviewed" if reviewed else "No reviews yet"
            ),
            pattern="badge" if reviewed else "",
            sign="+" if pass_rate >= PASS_RATE_GOOD else "-" if reviewed else "",
        ),
        _kpi(
            "avg_tokens_per_task",
            "Avg Tokens / Task",
            avg_tokens,
            sub_string="per task",
        ),
        _kpi(
            "avg_wall_time",
            "Avg Wall Time",
            f"{round(avg_wall)}s",
            sub_string=f"{round(avg_wall / 60.0, 1)} min per task",
        ),
    ]
    return {"count": len(items), "items": items}


def _build_spend_by_category(env, ctx):
    Talos = env["talos.talos"].sudo()
    records = Talos.search(ctx["scope"] + [("task_type", "!=", False)])
    amount_by_cat = {}
    for rec in records:
        key = rec.task_type
        if not key:
            continue
        amount_by_cat[key] = amount_by_cat.get(key, 0.0) + float(_total_tokens(rec))
    amount_by_cat = {k: round(v, 4) for k, v in amount_by_cat.items()}
    total = round(sum(amount_by_cat.values()), 4)

    selection = list(Talos._fields["task_type"].selection)
    ordered = sorted(selection, key=lambda kv: (-amount_by_cat.get(kv[0], 0.0), kv[1]))

    items = []
    for idx, (key, label) in enumerate(ordered):
        amount = amount_by_cat.get(key, 0.0)
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


def _build_pass_rate_by_ql(env, ctx):
    Talos = env["talos.talos"].sudo()
    scope = ctx["scope"]
    ql_taskers = ctx["ql_taskers"]
    ql_name = ctx["ql_name"]

    totals = _counts_by_user(Talos, scope)
    approved = _counts_by_user(Talos, scope, [("qc_status", "=", "passed")])
    reviewed = _counts_by_user(
        Talos, scope, [("qc_status", "in", ["passed", "failed"])]
    )

    items = []
    for ql_id, uids in ql_taskers.items():
        tasks = sum(totals.get(u, 0) for u in uids)
        appr = sum(approved.get(u, 0) for u in uids)
        rev = sum(reviewed.get(u, 0) for u in uids)
        rate = _pct1(appr, rev)
        name = ql_name.get(ql_id, UNASSIGNED_QL_LABEL)
        items.append({
            "key": ql_id,
            "label": name,
            "initials": _initials(name),
            "value": f"{rate}% · {tasks} tasks",
            "pass_rate": rate,
            "tasks": tasks,
            "reviewed": rev,
            "taskers": len(uids),
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
    ql_of_user = ctx["ql_of_user"]
    ql_name = ctx["ql_name"]
    records = Talos.search(ctx["scope"])

    per_day = {}
    per_ql_total = {}
    ql_ids = set()
    for rec in records:
        day = rec.create_date.date() if rec.create_date else None
        if not day:
            continue
        ql_id = ql_of_user.get(rec.user_id.id, UNASSIGNED_QL)
        ql_ids.add(ql_id)
        cost = float(_total_tokens(rec))
        per_day.setdefault(day, {})
        per_day[day][ql_id] = per_day[day].get(ql_id, 0.0) + cost
        per_ql_total[ql_id] = per_ql_total.get(ql_id, 0.0) + cost

    color_by_ql = {ql_id: _color(idx) for idx, ql_id in enumerate(sorted(ql_ids))}

    if per_day:
        start = rng["start"] if "start" in rng else min(per_day.keys())
        end = rng["end"] if "end" in rng else max(per_day.keys())
    else:
        today = fields.Datetime.now().date()
        start = rng.get("start", today)
        end = rng.get("end", today)

    data = []
    cursor = start
    grand_total = 0.0
    while cursor <= end:
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
            "color_token": color_by_ql[ql_id],
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


def _build_analytics(env, ctx):
    return {
        "kpi": _build_kpi_v2(env, ctx),
        "spend_by_category": _build_spend_by_category(env, ctx),
        "qc_pass_rate_by_ql": _build_pass_rate_by_ql(env, ctx),
        "daily_burn_rate": _build_daily_burn_rate(env, ctx),
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
        ctx, error = _resolve_context(env, request.params or {})
        if error is not None:
            return error
        return return_Response(
            message="OK", status=200, data=_build_analytics(env, ctx)
        )
