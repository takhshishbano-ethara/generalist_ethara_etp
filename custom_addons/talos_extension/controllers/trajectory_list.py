from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import _user_role_tag, _total_tokens  # noqa: F401
from .task_view_dashboard import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    STAGE_SELECTION,
    STATUS_SELECTION,
    _coerce_int,
    _error_response,
    _get_category_maps,
    _iso,
    _or_join,
    _prompts,
    _resolve_category_param,
    _spec_string,
    _stage_domain_for,
    _status_domain_for,
    _user_scope_domain,
)


MAX_TASKS_SCAN = 5000

TRAJECTORY_VARIANTS = (
    {
        "key": "claude",
        "label": "Claude 4.7",
        "field": "claude_trajectory",
        "input_tokens": "claude_input_tokens",
        "output_tokens": "claude_output_tokens",
        "status_field": "claude_status",
    },
    {
        "key": "glm",
        "label": "GLM 5",
        "field": "glm_trajectory",
        "input_tokens": "glm_input_tokens",
        "output_tokens": "glm_output_tokens",
        "status_field": "glm_status",
    },
    {
        "key": "onePA",
        "label": "1PA",
        "field": "onePA_trajectory",
        "input_tokens": "onePA_input_tokens",
        "output_tokens": "onePA_output_tokens",
        "status_field": None,
    },
    {
        "key": "onePB",
        "label": "1PB",
        "field": "onePB_trajectory",
        "input_tokens": "onePB_input_tokens",
        "output_tokens": "onePB_output_tokens",
        "status_field": None,
    },
    {
        "key": "onePC",
        "label": "1PC",
        "field": "onePC_trajectory",
        "input_tokens": "onePC_input_tokens",
        "output_tokens": "onePC_output_tokens",
        "status_field": None,
    },
    {
        "key": "onePD",
        "label": "1PD",
        "field": "onePD_trajectory",
        "input_tokens": "onePD_input_tokens",
        "output_tokens": "onePD_output_tokens",
        "status_field": None,
    },
    {
        "key": "golden",
        "label": "Golden (AI)",
        "field": "golden_trajectory",
        "input_tokens": "golden_input_tokens",
        "output_tokens": "golden_output_tokens",
        "status_field": "golden_status",
    },
    {
        "key": "golden_manual",
        "label": "Golden (Manual)",
        "field": "golden_trajectory_manual",
        "input_tokens": None,
        "output_tokens": None,
        "status_field": None,
    },
)

VARIANT_KEYS = tuple(v["key"] for v in TRAJECTORY_VARIANTS)


def _build_trajectory_domain(env, params):
    domain = []

    raw_start = (params.get("start_date") or "").strip()
    if raw_start:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid start_date '{raw_start}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )

    raw_end = (params.get("end_date") or "").strip()
    if raw_end:
        try:
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return None, _error_response(
                f"Invalid end_date '{raw_end}'. Expected YYYY-MM-DD.",
                status=400,
            )
        domain.append(
            (
                "create_date",
                "<",
                datetime.combine(end, datetime.min.time()) + timedelta(days=1),
            )
        )

    raw_cat = (params.get("category") or "").strip()
    if raw_cat:
        slugs, err = _resolve_category_param(env, raw_cat)
        if err:
            return None, _error_response(err, status=400)
        if slugs:
            domain.append(("task_type", "in", slugs))

    raw_task = (params.get("task_id") or "").strip()
    if raw_task:
        if raw_task.isdigit():
            domain.append(("id", "=", int(raw_task)))
        else:
            domain.append(("task_id", "=", raw_task))

    raw_ql = (params.get("ql") or "").strip()
    if raw_ql:
        if raw_ql.isdigit():
            domain.append(("user_id", "=", int(raw_ql)))
        else:
            domain.append(("user_id.name", "ilike", raw_ql))

    stage_raw = (params.get("stage") or "").strip()
    if stage_raw:
        allowed = dict(STAGE_SELECTION)
        requested = [s.strip() for s in stage_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid stage {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_stage_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    task_status_raw = (params.get("task_status_filter") or "").strip()
    if task_status_raw:
        allowed = dict(STATUS_SELECTION)
        requested = [s.strip() for s in task_status_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid task_status_filter {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_status_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|",
            ("task_id", "ilike", search),
            ("seed_prompt", "ilike", search),
            ("initial_prompt", "ilike", search),
            ("user_id.name", "ilike", search),
        ]

    return domain, None


def _parse_model_filter(raw):
    raw = (raw or "").strip()
    if not raw:
        return None, None
    requested = [m.strip() for m in raw.split(",") if m.strip()]
    invalid = [m for m in requested if m not in VARIANT_KEYS]
    if invalid:
        return None, (
            f"Invalid model {invalid}. Allowed: {', '.join(VARIANT_KEYS)}."
        )
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


def _derive_variant_status(task, variant, has_text):
    if variant["status_field"]:
        val = getattr(task, variant["status_field"], None) or ""
        if val:
            return val
    return "available" if has_text else "missing"


def _serialize_trajectory(task, variant, slug_to_label):
    text = getattr(task, variant["field"], None) or ""
    has_text = bool(text and text.strip())
    traj_len = len(text)
    input_tokens = (
        int(getattr(task, variant["input_tokens"], 0) or 0)
        if variant["input_tokens"]
        else 0
    )
    output_tokens = (
        int(getattr(task, variant["output_tokens"], 0) or 0)
        if variant["output_tokens"]
        else 0
    )
    total_tokens = input_tokens + output_tokens
    status = _derive_variant_status(task, variant, has_text)
    raw_prompt, golden_prompt = _prompts(task)

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
        "spec": _spec_string(task),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "category_slug": task.task_type or "",
        "category": slug_to_label.get(task.task_type, ""),
        "task_status": task.task_status or "",
        "qc_status": task.qc_status or "",
        "golden_status": task.golden_status or "",
        "assigned_user_id": task.user_id.id if task.user_id else False,
        "assigned_user_name": task.user_id.name if task.user_id else "",
        "created_at": _iso(task.create_date),
        "updated_at": _iso(task.write_date),
    }


class TalosTrajectoryListController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/trajectory_list",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def talos_ext_trajectory_list(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Talos trajectories.",
                status=403,
            )

        scope_domain = _user_scope_domain(env, role_tag)
        params = request.params or {}

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
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Talos = env["talos.talos"].sudo()
        tasks = Talos.search(
            full_domain,
            limit=MAX_TASKS_SCAN,
            order="write_date desc, id desc",
        )

        slug_to_label, _label_to_slug = _get_category_maps(env)

        active_variants = [
            v for v in TRAJECTORY_VARIANTS
            if model_filter is None or v["key"] in model_filter
        ]

        rows = []
        for task in tasks:
            for variant in active_variants:
                row = _serialize_trajectory(task, variant, slug_to_label)
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
