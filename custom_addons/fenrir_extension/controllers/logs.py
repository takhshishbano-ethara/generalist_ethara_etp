from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import (
    _create_date_domain,
    _parse_date,
    _scope,
    _user_role_tag,
)
from .task_view_dashboard import _coerce_int, _iso

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# Per-source fetch cap before the in-memory merge; keeps the endpoint
# bounded on busy databases. The response flags when a source was capped.
MERGE_CAP = 1000
SOURCES = ("activity", "message", "tracking")


def _scoped_task_ids(env, scope, single_id=None):
    Task = env["fenrir.task"].sudo()
    domain = list(scope)
    if single_id:
        domain.append(("id", "=", single_id))
    return Task.search(domain).ids


def _activity_entries(env, base_domain, task_ids):
    Activity = env["mail.activity"].sudo()
    domain = base_domain + [
        ("res_model", "=", "fenrir.task"),
        ("res_id", "in", task_ids),
    ]
    records = Activity.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    Task = env["fenrir.task"].sudo()
    task_codes = {t.id: t.code or "" for t in Task.browse(records.mapped("res_id"))}
    entries = []
    for rec in records:
        entries.append({
            "source": "activity",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "level": "info",
            "operation": rec.activity_type_id.name or "",
            "message": rec.summary or rec.note or "",
            "duration_ms": 0,
            "project_id": rec.res_id or 0,
            "project_seq": task_codes.get(rec.res_id, ""),
            "job_id": 0,
        })
    return entries, len(records) >= MERGE_CAP


def _message_entries(env, base_domain, task_ids):
    Message = env["mail.message"].sudo()
    domain = base_domain + [
        ("model", "=", "fenrir.task"),
        ("res_id", "in", task_ids),
    ]
    records = Message.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    Task = env["fenrir.task"].sudo()
    task_codes = {t.id: t.code or "" for t in Task.browse(records.mapped("res_id"))}
    entries = []
    for rec in records:
        entries.append({
            "source": "message",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "subject": rec.subject or "",
            "message": rec.preview or rec.body or "",
            "author": rec.author_id.name or "",
            "message_type": rec.message_type or "",
            "project_id": rec.res_id or 0,
            "project_seq": task_codes.get(rec.res_id, ""),
        })
    return entries, len(records) >= MERGE_CAP


def _tracking_entries(env, base_domain, task_ids):
    Tracking = env["mail.tracking.value"].sudo()
    Message = env["mail.message"].sudo()
    message_ids = Message.search(
        [("model", "=", "fenrir.task"), ("res_id", "in", task_ids)]
    ).ids
    if not message_ids:
        return [], False
    domain = base_domain + [("mail_message_id", "in", message_ids)]
    records = Tracking.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    entries = []
    for rec in records:
        msg = rec.mail_message_id
        entries.append({
            "source": "tracking",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "field_name": rec.field_id.name or "",
            "field_desc": rec.field_id.field_description or "",
            "old_value": rec.old_value_char or str(rec.old_value_integer or "")
            or str(rec.old_value_float or ""),
            "new_value": rec.new_value_char or str(rec.new_value_integer or "")
            or str(rec.new_value_float or ""),
            "author": msg.author_id.name if msg else "",
            "project_id": msg.res_id if msg else 0,
        })
    return entries, len(records) >= MERGE_CAP


class FenrirLogsController(http.Controller):

    @http.route(
        "/api/v1/fenrir_ext/logs",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def fenrir_ext_logs(self, **kwargs):
        """Merged, paginated event feed: activities + messages + tracking values.

        Query params:
        - project_id: restrict to one fenrir.task (its ID)
        - source: comma list of activity,message,tracking (default all)
        - start_date / end_date: YYYY-MM-DD on create_date
        - page / limit
        """
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="You are not allowed to access Fenrir logs.",
                status=403,
            )

        params = request.params or {}

        raw_sources = (params.get("source") or "").strip()
        sources = list(SOURCES)
        if raw_sources:
            requested = [s.strip() for s in raw_sources.split(",") if s.strip()]
            invalid = [s for s in requested if s not in SOURCES]
            if invalid:
                return return_Response(
                    message=(
                        f"Invalid source value(s): {', '.join(invalid)}. "
                        f"Allowed: {', '.join(SOURCES)}."
                    ),
                    status=400,
                )
            sources = requested

        raw_start = (params.get("start_date") or "").strip()
        raw_end = (params.get("end_date") or "").strip()
        start = end = None
        if raw_start:
            start, error = _parse_date(raw_start, "start_date")
            if error is not None:
                return error
        if raw_end:
            end, error = _parse_date(raw_end, "end_date")
            if error is not None:
                return error
        if start and end and start > end:
            return return_Response(
                message="Invalid date range: start_date must be on or before end_date.",
                status=400,
            )
        date_domain = _create_date_domain(start, end)

        _tag, scope, _tasks = _scope(env)
        project_id = _coerce_int(params.get("project_id"), 0)
        if project_id:
            task = env["fenrir.task"].sudo().search(
                [("id", "=", project_id)] + scope, limit=1
            )
            if not task:
                return return_Response(
                    message="Task not found or not in your scope.", status=404
                )
            task_ids = [project_id]
        else:
            task_ids = _scoped_task_ids(env, scope)

        if not task_ids:
            return return_Response(
                message="OK",
                status=200,
                data={
                    "logs": [],
                    "pagination": {
                        "total_records": 0,
                        "page": 1,
                        "limit": DEFAULT_LIMIT,
                        "total_pages": 0,
                    },
                    "sources": sources,
                    "truncated_sources": [],
                },
            )

        entries = []
        truncated_sources = []

        if "activity" in sources:
            activity_entries, capped = _activity_entries(env, date_domain, task_ids)
            entries += activity_entries
            if capped:
                truncated_sources.append("activity")

        if "message" in sources:
            message_entries, capped = _message_entries(env, date_domain, task_ids)
            entries += message_entries
            if capped:
                truncated_sources.append("message")

        if "tracking" in sources:
            tracking_entries, capped = _tracking_entries(env, date_domain, task_ids)
            entries += tracking_entries
            if capped:
                truncated_sources.append("tracking")

        entries.sort(key=lambda e: (e["timestamp"] or "", e["id"]), reverse=True)

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = min(max(1, _coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
        offset = (page - 1) * limit
        total = len(entries)
        total_pages = (total + limit - 1) // limit if total else 0

        data = {
            "logs": entries[offset:offset + limit],
            "pagination": {
                "total_records": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            },
            "sources": sources,
            "truncated_sources": truncated_sources,
        }
        return return_Response(message="OK", status=200, data=data)
