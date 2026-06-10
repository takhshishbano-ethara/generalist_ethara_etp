from datetime import datetime, timedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard_overview import _scope_domain, _user_role_tag

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date_label(prefix, d):
    return f"{prefix}-{d.day}-{d.strftime('%B-%Y')}"


def _time_ago(now, when):
    if not when:
        return ""
    delta = now - when
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


def _task_action(task):
    if task.qc_status == "passed":
        return "approved"
    if task.qc_status == "failed":
        return "rejected"
    if task.task_status == "completed":
        return "completed"
    if task.task_status in ("in_progress", "Submitted"):
        return "in_progress"
    return "updated"


def _serialize_task_log(task, now):
    actor = task.write_uid or task.create_uid
    return {
        "type": "task",
        "id": task.id,
        "actor_id": actor.id or False,
        "actor_name": actor.name or "",
        "action": _task_action(task),
        "task_code": task.task_id or "",
        "task_internal_id": task.id,
        "category": task.task_type or "",
        "persona": task.persona_id.name or "",
        "difficulty": task.difficulty or "",
        "state": task.task_status or "",
        "review_state": task.qc_status or "",
        "golden_status": task.golden_status or "",
        "timestamp": task.write_date.isoformat() if task.write_date else "",
        "time_ago": _time_ago(now, task.write_date),
    }


def _serialize_turn_log(turn, now):
    actor = turn.write_uid or turn.create_uid
    return {
        "type": "turn",
        "id": turn.id,
        "actor_id": actor.id or False,
        "actor_name": actor.name or "",
        "action": turn.turn_status or "updated",
        "task_code": turn.talos_id.task_id or "",
        "task_internal_id": turn.talos_id.id or 0,
        "turn_number": turn.turn_number or 0,
        "model": turn.model_name or "",
        "qc_severity": turn.qc_severity or "",
        "feedback": turn.feedback or "",
        "tool_names": turn.tool_names or "",
        "timestamp": turn.write_date.isoformat() if turn.write_date else "",
        "time_ago": _time_ago(now, turn.write_date),
    }


def _bucket_for_date(now_date, target_date):
    if target_date == now_date:
        return _date_label("Today", now_date)
    if target_date == now_date + timedelta(days=1):
        return _date_label("Tomorrow", target_date)
    return "older"


def _collect_logs(env):
    scope = _scope_domain(env)
    Talos = env["talos.talos"].sudo()
    Turn = env["talos.turn"].sudo() if "talos.turn" in env else None

    now = fields.Datetime.now()
    now_date = now.date()

    items = []

    task_records = Talos.search(scope, order="write_date desc, id desc", limit=MAX_LIMIT)
    for task in task_records:
        items.append(_serialize_task_log(task, now))

    if Turn is not None:
        turn_scope = []
        if scope and scope[0][0] == "user_id":
            turn_scope = [("talos_id.user_id", "=", env.user.id)]
        turn_records = Turn.search(
            turn_scope, order="write_date desc, id desc", limit=MAX_LIMIT
        )
        for turn in turn_records:
            items.append(_serialize_turn_log(turn, now))

    items.sort(key=lambda i: i.get("timestamp") or "", reverse=True)

    today_key = _date_label("Today", now_date)
    tomorrow_key = _date_label("Tomorrow", now_date + timedelta(days=1))
    record = {today_key: [], tomorrow_key: [], "older": []}

    for entry in items:
        ts = entry.get("timestamp") or ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                bucket = _bucket_for_date(now_date, dt.date())
            except ValueError:
                bucket = "older"
        else:
            bucket = "older"
        record.setdefault(bucket, []).append(entry)

    return {
        "today_key": today_key,
        "tomorrow_key": tomorrow_key,
        "record": record,
    }


class TalosLogsController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/logs",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def talos_ext_logs(self, **kwargs):
        env = request.env
        if _user_role_tag(env) is None:
            return return_Response(
                message="You are not allowed to access Talos logs.",
                status=403,
            )

        result = _collect_logs(env)
        today_key = result["today_key"]
        tomorrow_key = result["tomorrow_key"]
        record = result["record"]

        today_count = len(record.get(today_key, []))
        tomorrow_count = len(record.get(tomorrow_key, []))
        older_count = len(record.get("older", []))

        return return_Response(
            message={
                "status_code": 200,
                "message": "Success",
                "record": record,
                "today_count": today_count,
                "tomorrow_count": tomorrow_count,
                "older_count": older_count,
            },
            status=200,
        )
