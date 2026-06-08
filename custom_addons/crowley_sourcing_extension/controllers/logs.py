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
SOURCES = ("processing_log", "job", "notification")


def _project_scope_domain(scope, prefix="project_id"):
    """Rewrite a video.editor.project scope domain onto a related model."""
    return [
        (f"{prefix}.{field}", op, value)
        for field, op, value in scope
    ]


def _processing_log_entries(env, base_domain, scope):
    Log = env["video.editor.processing.log"].sudo()
    domain = base_domain + _project_scope_domain(scope)
    records = Log.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    entries = []
    for rec in records:
        entries.append({
            "source": "processing_log",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "level": rec.level or "",
            "operation": rec.operation or "",
            "message": rec.message or "",
            "duration_ms": rec.duration_ms or 0,
            "project_id": rec.project_id.id or 0,
            "project_seq": rec.project_id.name or "",
            "job_id": rec.job_id.id or 0,
        })
    return entries, len(records) >= MERGE_CAP


def _job_entries(env, base_domain, scope):
    Job = env["video.editor.job"].sudo()
    domain = base_domain + _project_scope_domain(scope)
    records = Job.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    type_labels = dict(Job._fields["job_type"].selection)
    status_labels = dict(Job._fields["status"].selection)
    entries = []
    for rec in records:
        entries.append({
            "source": "job",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "job_type": rec.job_type or "",
            "job_type_label": type_labels.get(rec.job_type, ""),
            "status": rec.status or "",
            "status_label": status_labels.get(rec.status, ""),
            "progress": rec.progress_text or "",
            "started": _iso(rec.started_at),
            "finished": _iso(rec.finished_at),
            "duration_ms": rec.duration_ms or 0,
            "error_message": rec.error_message or "",
            "project_id": rec.project_id.id or 0,
            "project_seq": rec.project_id.name or "",
        })
    return entries, len(records) >= MERGE_CAP


def _notification_entries(env, base_domain, tag):
    Notification = env["kubera.notification"].sudo()
    domain = base_domain + [("res_model", "=", "video.editor.project")]
    # Non-privileged roles only see their own notifications.
    if tag != "full":
        domain.append(("user_id", "=", env.user.id))
    records = Notification.search(domain, order="create_date desc, id desc", limit=MERGE_CAP)
    entries = []
    for rec in records:
        entries.append({
            "source": "notification",
            "timestamp": _iso(rec.create_date),
            "id": rec.id,
            "title": rec.title or "",
            "message": rec.message or "",
            "priority": rec.priority or "",
            "is_read": bool(rec.is_read),
            "user_id": rec.user_id.id or 0,
            "user_name": rec.user_id.name or "",
            "project_id": rec.res_id or 0,
        })
    return entries, len(records) >= MERGE_CAP


class CrowleySourcingLogsController(http.Controller):

    @http.route(
        "/api/v1/crowley_sourcing_ext/logs",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def crowley_sourcing_ext_logs(self, **kwargs):
        """Merged, paginated event feed: processing logs + jobs + notifications.

        Query params:
        - project_id: restrict to one video.editor.project
        - source: comma list of processing_log,job,notification (default all)
        - level: comma list of info,warning,error (processing logs only)
        - start_date / end_date: YYYY-MM-DD on create_date
        - page / limit
        """
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="You are not allowed to access Crowley Sourcing logs.",
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

        _tag, scope, _projects = _scope(env)
        project_id = _coerce_int(params.get("project_id"), 0)
        if project_id:
            project = env["video.editor.project"].sudo().search(
                [("id", "=", project_id)] + scope, limit=1
            )
            if not project:
                return return_Response(
                    message="Project not found or not in your scope.", status=404
                )

        entries = []
        truncated_sources = []

        if "processing_log" in sources:
            domain = list(date_domain)
            if project_id:
                domain.append(("project_id", "=", project_id))
            raw_level = (params.get("level") or "").strip()
            if raw_level:
                Log = env["video.editor.processing.log"]
                valid = dict(Log._fields["level"].selection)
                levels = [v.strip() for v in raw_level.split(",") if v.strip()]
                invalid = [v for v in levels if v not in valid]
                if invalid:
                    return return_Response(
                        message=f"Invalid level value(s): {', '.join(invalid)}.",
                        status=400,
                    )
                domain.append(("level", "in", levels))
            log_entries, capped = _processing_log_entries(env, domain, scope)
            entries += log_entries
            if capped:
                truncated_sources.append("processing_log")

        if "job" in sources:
            domain = list(date_domain)
            if project_id:
                domain.append(("project_id", "=", project_id))
            job_entries, capped = _job_entries(env, domain, scope)
            entries += job_entries
            if capped:
                truncated_sources.append("job")

        if "notification" in sources:
            domain = list(date_domain)
            if project_id:
                domain.append(("res_id", "=", project_id))
            notif_entries, capped = _notification_entries(env, domain, tag)
            entries += notif_entries
            if capped:
                truncated_sources.append("notification")

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
