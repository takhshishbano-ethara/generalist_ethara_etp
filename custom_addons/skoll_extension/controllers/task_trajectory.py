from __future__ import annotations

import json
import logging

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _user_role_tag
from .task_view_dashboard import (
    STAGE_LABELS,
    _derive_stage,
    _derive_status,
    _prompts,
    _spec_string,
    _user_scope_domain,
)

_logger = logging.getLogger(__name__)

LINK_KEYS = (
    "trajectory_url",
    "viewer_url",
    "playback_url",
    "s3_url",
    "url",
    "link",
)


def _resolve_task(env, params):
    Task = env["skoll.skoll"].sudo()
    raw_id = params.get("id")
    if raw_id:
        try:
            rec_id = int(raw_id)
        except (TypeError, ValueError):
            return None, "Invalid id"
        task = Task.browse(rec_id).exists()
        return (task or None), None
    raw_task_id = params.get("task_id")
    if raw_task_id:
        task = Task.search([("task_id", "=", str(raw_task_id).strip())], limit=1)
        return (task or None), None
    return None, "Provide 'id' or 'task_id'"


def _parse_content(content):
    if not content:
        return None, False
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None, False
    return parsed, True


def _extract_trajectory_link(parsed):
    if not isinstance(parsed, dict):
        return None
    for key in LINK_KEYS:
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _backend_url(env, task):
    base = env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
    base = base.rstrip("/")
    return f"{base}/web#id={task.id}&model=skoll.skoll&view_type=form"


def _serialize_task_meta(task):
    raw_prompt, golden_prompt = _prompts(task)
    assigned_user = None
    for emp in task.employee_ids:
        if emp.user_id:
            assigned_user = emp.user_id
            break
    if task.life_domain_ids:
        category_id = task.life_domain_ids[0].id
        category_name = task.life_domain_ids[0].name or ""
    else:
        category_id = 0
        category_name = ""
    stage = _derive_stage(task.qc_status)
    status = _derive_status(task.qc_status)
    return {
        "id": task.id,
        "seq": task.task_id or "",
        "spec": _spec_string(task),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "persona_id": task.persona_id.id if task.persona_id else 0,
        "persona_name": task.persona_id.name if task.persona_id else "",
        "mode": task.mode or "",
        "assigned_ql_id": assigned_user.id if assigned_user else 0,
        "assigned_ql_name": assigned_user.name if assigned_user else "",
        "category_slug": str(category_id) if category_id else "",
        "category": category_name,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "status": status,
        "state": task.qc_status or "",
        "qc_status": task.qc_status or "",
        "created_at": fields.Datetime.to_string(task.create_date) if task.create_date else None,
        "updated_at": fields.Datetime.to_string(task.write_date) if task.write_date else None,
    }


class SkollTaskTrajectoryController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/task_trajectory",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def task_trajectory(self, **kwargs):
        env = request.env
        tag = _user_role_tag(env)
        if tag is None:
            return return_Response(
                message="Forbidden",
                status=403,
                errors=["User has no Skoll role"],
            )
        try:
            task, err = _resolve_task(env, kwargs)
            if err:
                return return_Response(message="Bad Request", status=400, errors=[err])
            if not task:
                return return_Response(message="Not Found", status=404, errors=["Task not found"])

            scope_domain = _user_scope_domain(env, tag)
            if scope_domain:
                allowed = env["skoll.skoll"].sudo().search_count([("id", "=", task.id)] + scope_domain)
                if not allowed:
                    return return_Response(
                        message="Forbidden",
                        status=403,
                        errors=["Task not in scope for this user"],
                    )

            content = task.content or ""
            parsed, is_valid = _parse_content(content)
            link = _extract_trajectory_link(parsed)

            trajectory = {
                "content": content,
                "content_parsed": parsed if isinstance(parsed, (dict, list)) else None,
                "is_valid_json": is_valid,
                "size_bytes": len(content.encode("utf-8")) if content else 0,
                "spawn_tree": task.spawn_tree or "",
                "trajectory_url": link,
                "backend_url": _backend_url(env, task),
            }
            qc = {
                "status": task.qc_status or "",
                "review": task.qc_result or "",
                "structural_result": task.qc_structural_result or "",
            }
        except Exception as exc:
            _logger.exception("Skoll task_trajectory failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": tag,
                "task": _serialize_task_meta(task),
                "trajectory": trajectory,
                "qc": qc,
            },
        )
