from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _user_role_tag
from .task_view_dashboard import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    STAGE_SELECTION,
    STAGE_TO_QC_STATUS,
    STATUS_SELECTION,
    STATUS_TO_QC_STATUS,
    _prompts,
    _resolve_category_param,
    _search_domain,
    _spec_string,
    _user_scope_domain,
)

_logger = logging.getLogger(__name__)

MAX_TASKS_SCAN = 5000

TRAJECTORY_VARIANTS = (
    {
        "key": "golden",
        "label": "Golden Trajectory",
        "field": "content",
        "call_types": ("generate", "improve"),
    },
    {
        "key": "qc_review",
        "label": "QC Review",
        "field": "qc_result",
        "call_types": ("qc",),
    },
    {
        "key": "qc_structural",
        "label": "QC Structural",
        "field": "qc_structural_result",
        "call_types": (),
    },
)
VARIANT_KEYS = tuple(v["key"] for v in TRAJECTORY_VARIANTS)


def _error_response(message, status=400):
    return return_Response(message=message, status=status, errors=[message])


def _coerce_int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _date_filter(params):
    dom = []
    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return [], f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD."
        dom.append(("create_date", ">=", datetime.combine(start, time.min)))
    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        try:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return [], f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD."
        dom.append(("create_date", "<=", datetime.combine(end, time.max)))
    return dom, None


def _stage_domain_validated(raw):
    if not raw:
        return [], None
    allowed = dict(STAGE_SELECTION)
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in requested if s not in allowed]
    if invalid:
        return [], f"Invalid stage {invalid}. Allowed: {', '.join(allowed)}."
    statuses = set()
    for s in requested:
        statuses.update(STAGE_TO_QC_STATUS.get(s, ()))
    if not statuses:
        return [], None
    return [("qc_status", "in", list(statuses))], None


def _status_domain_validated(raw):
    if not raw:
        return [], None
    allowed = dict(STATUS_SELECTION)
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in requested if s not in allowed]
    if invalid:
        return [], f"Invalid task_status_filter {invalid}. Allowed: {', '.join(allowed)}."
    qc = set()
    for s in requested:
        qc.update(STATUS_TO_QC_STATUS.get(s, ()))
    if not qc:
        return [], None
    return [("qc_status", "in", list(qc))], None


def _task_id_filter(raw):
    if not raw:
        return []
    raw = str(raw).strip()
    if not raw:
        return []
    if raw.isdigit():
        return [("id", "=", int(raw))]
    return [("task_id", "=", raw)]


def _ql_filter(raw):
    if not raw:
        return []
    raw = str(raw).strip()
    if not raw:
        return []
    if raw.isdigit():
        return [("employee_ids.user_id", "=", int(raw))]
    return [("employee_ids.user_id.name", "ilike", raw)]


def _build_trajectory_domain(env, params):
    domain = []

    dd, err = _date_filter(params)
    if err:
        return None, _error_response(err, status=400)
    domain += dd

    raw_cat = (params.get("category") or "").strip()
    if raw_cat:
        domain += _resolve_category_param(env, raw_cat)

    domain += _task_id_filter(params.get("task_id"))
    domain += _ql_filter(params.get("ql"))

    stage_dom, err = _stage_domain_validated((params.get("stage") or "").strip())
    if err:
        return None, _error_response(err, status=400)
    domain += stage_dom

    status_dom, err = _status_domain_validated(
        (params.get("task_status_filter") or "").strip()
    )
    if err:
        return None, _error_response(err, status=400)
    domain += status_dom

    domain += _search_domain(params.get("search"))
    return domain, None


def _parse_model_filter(raw):
    raw = (raw or "").strip()
    if not raw:
        return None, None
    requested = [m.strip() for m in raw.split(",") if m.strip()]
    invalid = [m for m in requested if m not in VARIANT_KEYS]
    if invalid:
        return None, f"Invalid model {invalid}. Allowed: {', '.join(VARIANT_KEYS)}."
    return set(requested), None


def _parse_has_trajectory(raw):
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if v in ("1", "true", "yes", "y"):
        return True
    if v in ("0", "false", "no", "n"):
        return False
    return None


def _derive_variant_status(has_text, latest_gen_status):
    if latest_gen_status:
        return latest_gen_status
    return "available" if has_text else "missing"


def _aggregate_generations(env, task_ids):
    sums = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "total_cost": 0.0}
    )
    latest = {}
    if not task_ids:
        return sums, latest
    gens = env["skoll.generation"].sudo().search(
        [("task_id", "in", list(task_ids))],
        order="create_date desc, id desc",
    )
    for g in gens:
        key = (g.task_id.id, g.call_type)
        sums[key]["input_tokens"] += int(g.input_tokens or 0)
        sums[key]["output_tokens"] += int(g.output_tokens or 0)
        sums[key]["total_tokens"] += int(g.total_tokens or 0)
        sums[key]["total_cost"] += float(g.total_cost or 0.0)
        if key not in latest:
            latest[key] = g.status or ""
    return sums, latest


def _serialize_trajectory(task, variant, gen_sums, gen_latest):
    text = getattr(task, variant["field"], None) or ""
    has_text = bool(text and text.strip())
    traj_len = len(text)

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    total_cost = 0.0
    latest_status = ""
    for ct in variant["call_types"]:
        agg = gen_sums.get((task.id, ct))
        if agg:
            input_tokens += agg["input_tokens"]
            output_tokens += agg["output_tokens"]
            total_tokens += agg["total_tokens"]
            total_cost += agg["total_cost"]
        if not latest_status:
            latest_status = gen_latest.get((task.id, ct), "")

    status = _derive_variant_status(has_text, latest_status)
    raw_prompt, golden_prompt = _prompts(task)

    assigned_user = None
    for emp in task.employee_ids:
        if emp.user_id:
            assigned_user = emp.user_id
            break

    if task.life_domain_ids:
        cat_id = task.life_domain_ids[0].id
        cat_name = task.life_domain_ids[0].name or ""
    else:
        cat_id = 0
        cat_name = ""

    return {
        "id": f"{task.id}_{variant['key']}",
        "task_id": task.id,
        "task_ref": task.task_id or "",
        "model": variant["key"],
        "model_label": variant["label"],
        "status": status,
        "has_trajectory": has_text,
        "trajectory_length": traj_len,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 4),
        "spec": _spec_string(task),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "category_slug": str(cat_id) if cat_id else "",
        "category": cat_name,
        "task_status": task.qc_status or "",
        "qc_status": task.qc_status or "",
        "assigned_user_id": assigned_user.id if assigned_user else False,
        "assigned_user_name": assigned_user.name if assigned_user else "",
        "created_at": fields.Datetime.to_string(task.create_date) if task.create_date else None,
        "updated_at": fields.Datetime.to_string(task.write_date) if task.write_date else None,
    }


class SkollTrajectoryListController(http.Controller):
    @http.route(
        "/api/v1/skoll_ext/trajectory_list",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def skoll_ext_trajectory_list(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Skoll trajectories.",
                status=403,
                errors=["User has no Skoll role"],
            )

        scope_domain = _user_scope_domain(env, role_tag)
        params = request.params or {}

        try:
            domain, error = _build_trajectory_domain(env, params)
            if error is not None:
                return error
            full_domain = scope_domain + domain

            model_filter, err = _parse_model_filter(params.get("model"))
            if err:
                return _error_response(err, status=400)

            has_trajectory_filter = _parse_has_trajectory(params.get("has_trajectory"))

            variant_status_raw = (params.get("variant_status") or "").strip()
            variant_status_filter = None
            if variant_status_raw:
                variant_status_filter = {
                    s.strip() for s in variant_status_raw.split(",") if s.strip()
                }

            page = max(1, _coerce_int(params.get("page"), 1))
            limit = _coerce_int(params.get("limit"), DEFAULT_LIMIT)
            limit = max(1, min(limit, MAX_LIMIT))
            offset = (page - 1) * limit

            Task = env["skoll.skoll"].sudo()
            tasks = Task.search(
                full_domain,
                limit=MAX_TASKS_SCAN,
                order="write_date desc, id desc",
            )

            gen_sums, gen_latest = _aggregate_generations(env, tasks.ids)

            active_variants = [
                v for v in TRAJECTORY_VARIANTS
                if model_filter is None or v["key"] in model_filter
            ]

            rows = []
            for task in tasks:
                for variant in active_variants:
                    row = _serialize_trajectory(task, variant, gen_sums, gen_latest)
                    if (
                        has_trajectory_filter is not None
                        and row["has_trajectory"] != has_trajectory_filter
                    ):
                        continue
                    if (
                        variant_status_filter
                        and row["status"] not in variant_status_filter
                    ):
                        continue
                    rows.append(row)

            total = len(rows)
            page_rows = rows[offset:offset + limit]
        except Exception as exc:
            _logger.exception("Skoll trajectory_list failed")
            return return_Response(
                message="Internal Server Error",
                status=500,
                errors=[str(exc)],
            )

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "columns": [
                    {"key": "task_ref", "label": "Task", "type": "string"},
                    {"key": "model_label", "label": "Model", "type": "string"},
                    {"key": "status", "label": "Status", "type": "string"},
                    {"key": "has_trajectory", "label": "Has Trajectory", "type": "boolean"},
                    {"key": "trajectory_length", "label": "Length", "type": "integer"},
                    {"key": "input_tokens", "label": "Input Tokens", "type": "integer"},
                    {"key": "output_tokens", "label": "Output Tokens", "type": "integer"},
                    {"key": "total_tokens", "label": "Total Tokens", "type": "integer"},
                    {"key": "assigned_user_name", "label": "Owner", "type": "string"},
                    {"key": "category", "label": "Category", "type": "string"},
                    {"key": "updated_at", "label": "Updated", "type": "datetime"},
                ],
                "rows": page_rows,
                "total_records": total,
                "page": page,
                "limit": limit,
                "available_models": [
                    {"key": v["key"], "label": v["label"]}
                    for v in TRAJECTORY_VARIANTS
                ],
                "scanned_tasks": len(tasks),
                "max_tasks_scan": MAX_TASKS_SCAN,
            },
        )
