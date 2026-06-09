from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .dashboard_overview import _scope_domain, _user_role_tag

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500

STATUS_SELECTION = (
    ("unstarted", "unstarted"),
    ("submitted", "submitted"),
    ("approved", "approved"),
    ("failed_qc", "failed_qc"),
)

STAGE_SELECTION = (
    ("s1_draft", "S1 Draft"),
    ("s2_submitted", "S2 Submitted"),
    ("s3_qc", "S3 QC"),
    ("failed", "Failed"),
)


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(dt):
    return dt.isoformat() if dt else ""


def _error_response(message, status):
    return return_Response(message=message, status=status, errors=[message])


def _parse_selection(model, field_name, raw, label):
    valid = dict(model._fields[field_name].selection)
    requested = [v.strip() for v in raw.split(",") if v.strip()]
    invalid = [v for v in requested if v not in valid]
    if invalid:
        return None, _error_response(
            f"Invalid {label} value(s): {', '.join(invalid)}.",
            status=400,
        )
    return requested, None


def _status_domain_for(status_slug):
    if status_slug == "unstarted":
        return [("task_status", "in", ("NotSubmitted", "draft"))]
    if status_slug == "submitted":
        return [
            "|",
            ("task_status", "in", ("in_progress", "Submitted")),
            "&",
            ("task_status", "=", "completed"),
            ("qc_status", "=", "pending"),
        ]
    if status_slug == "approved":
        return [("qc_status", "=", "passed")]
    if status_slug == "failed_qc":
        return [("qc_status", "=", "failed")]
    return []


def _or_join(sub_domains):
    sub_domains = [d for d in sub_domains if d]
    if not sub_domains:
        return []
    if len(sub_domains) == 1:
        return sub_domains[0]
    out = ["|"] * (len(sub_domains) - 1)
    for d in sub_domains:
        out += d
    return out


def _stage_domain_for(stage_slug):
    if stage_slug == "s1_draft":
        return [("task_status", "in", ("NotSubmitted", "draft"))]
    if stage_slug == "s2_submitted":
        return [("task_status", "in", ("in_progress", "Submitted"))]
    if stage_slug == "s3_qc":
        return [
            "|",
            ("qc_status", "in", ("passed", "failed")),
            "&",
            ("task_status", "=", "completed"),
            ("qc_status", "=", "pending"),
        ]
    if stage_slug == "failed":
        return [("qc_status", "=", "failed")]
    return []


def _build_task_view_domain(env, params):
    Talos = env["talos.talos"]
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

    raw_task_type = (params.get("task_type") or "").strip()
    if raw_task_type:
        values, error = _parse_selection(
            Talos, "task_type", raw_task_type, "task_type"
        )
        if error is not None:
            return None, error
        if values:
            domain.append(("task_type", "in", values))

    raw_difficulty = (params.get("difficulty") or "").strip()
    if raw_difficulty:
        values, error = _parse_selection(
            Talos, "difficulty", raw_difficulty, "difficulty"
        )
        if error is not None:
            return None, error
        if values:
            domain.append(("difficulty", "in", values))

    raw_persona = (params.get("persona") or "").strip()
    if raw_persona:
        if raw_persona.isdigit():
            domain.append(("persona_id", "=", int(raw_persona)))
        else:
            domain.append(("persona_id.name", "ilike", raw_persona))

    raw_tasker = (params.get("tasker") or "").strip()
    if raw_tasker:
        if raw_tasker.isdigit():
            domain.append(("user_id", "=", int(raw_tasker)))
        else:
            domain.append(("user_id.name", "ilike", raw_tasker))

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

    status_raw = (params.get("status") or "").strip()
    if status_raw:
        allowed = dict(STATUS_SELECTION)
        requested = [s.strip() for s in status_raw.split(",") if s.strip()]
        invalid = [s for s in requested if s not in allowed]
        if invalid:
            return None, _error_response(
                f"Invalid status {invalid}. Allowed: {', '.join(allowed)}.",
                status=400,
            )
        sub_domains = [_status_domain_for(s) for s in requested]
        domain += _or_join(sub_domains)

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|", "|", "|", "|",
            ("task_id", "ilike", search),
            ("seed_prompt", "ilike", search),
            ("initial_prompt", "ilike", search),
            ("user_id.name", "ilike", search),
            ("persona_id.name", "ilike", search),
        ]

    return domain, None


def _derive_stage(task):
    if not task.task_status or task.task_status in ("NotSubmitted", "draft", "not_assigned"):
        return "not_assigned", "not_assigned"
    if task.task_status in ("in_progress", "Submitted"):
        return "s1_in_progress", "S1 In Progress"
    if task.task_status == "completed":
        return "s2_qc", "S2 QC"
    return task.task_status, task.task_status


def _derive_status(task):
    if not task.task_status or task.task_status in ("NotSubmitted", "draft", "not_assigned"):
        return "not_assigned"
    if task.qc_status == "passed":
        return "approved"
    if task.qc_status == "failed":
        return "rejected"
    if task.task_status == "completed":
        return "pending_review"
    return "in_progress"


_COST_FIELDS = (
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


def _task_cost_usd(task):
    total_tokens = 0
    for fname in _COST_FIELDS:
        total_tokens += getattr(task, fname, 0) or 0
    return round(total_tokens / 1000.0, 4)


def _task_token_split(task):
    input_total = 0
    output_total = 0
    for fname in _COST_FIELDS:
        value = getattr(task, fname, 0) or 0
        if fname.endswith("_input_tokens"):
            input_total += value
        elif fname.endswith("_output_tokens"):
            output_total += value
    return input_total, output_total


def _attempts(task):
    sandboxes = task.sandbox_ids if "sandbox_ids" in task._fields else None
    used = len(sandboxes) if sandboxes else 0
    if not used and task.task_status and task.task_status not in (
        "NotSubmitted", "draft", "not_assigned"
    ):
        used = 1
    return used, max(0, 3 - used)


def _prompts(task):
    raw = (task.seed_prompt or "").strip()
    golden = (task.initial_prompt or "").strip()
    if not raw:
        raw = golden
        golden = ""
    elif raw == golden:
        golden = ""
    return raw, golden


def _spec_string(task):
    persona = task.persona_id.name or "-"
    task_type = task.task_type or "-"
    difficulty = task.difficulty or "-"
    return f"{persona} · {task_type} · {difficulty}"


def _category_label(task):
    selection = dict(task._fields["task_type"].selection or [])
    raw = task.task_type or ""
    return selection.get(raw, raw.replace("_", " ").title() if raw else "")


def _serialize_task(task):
    raw_prompt, golden_prompt = _prompts(task)
    stage_slug, stage_label = _derive_stage(task)
    attempts_used, attempts_remaining = _attempts(task)
    input_tokens, output_tokens = _task_token_split(task)
    return {
        "id": task.id,
        "seq": task.task_id or "",
        "spec": _spec_string(task),
        "raw_prompt": raw_prompt,
        "golden_prompt": golden_prompt,
        "is_enriched": bool(golden_prompt),
        "assigned_ql_id": task.user_id.id or False,
        "assigned_ql_name": task.user_id.name or "",
        "category_slug": task.task_type or "",
        "category": _category_label(task),
        "stage": stage_slug,
        "stage_label": stage_label,
        "status": _derive_status(task),
        "state": task.task_status or "",
        "review_state": task.qc_status or "",
        "cost_usd": _task_cost_usd(task),
        "attempts_used": attempts_used,
        "attempts_remaining": attempts_remaining,
        "updated_at": _iso(task.write_date),
        "created_at": _iso(task.create_date),
        "persona_id": task.persona_id.id or 0,
        "persona_name": task.persona_id.name or "",
        "task_type": task.task_type or "",
        "difficulty": task.difficulty or "",
        "trajectory_modifier": task.trajectory_modifier or "",
        "safety_critical": task.safety_critical or "",
        "heart_taxonomy_ids": task.heart_taxonomy.ids,
        "heart_taxonomy_names": task.heart_taxonomy.mapped("name"),
        "golden_status": task.golden_status or "",
        "task_description_status": task.task_description_status or "",
        "auto_process_status": task.auto_process_status or "",
        "claude_status": task.claude_status or "",
        "glm_status": task.glm_status or "",
        "sandbox_count": len(task.sandbox_ids),
        "has_golden_trajectory": bool(
            task.golden_trajectory or task.golden_trajectory_manual
        ),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
    }


class TalosTaskViewDashboardController(http.Controller):

    @http.route(
        "/api/v1/talos_ext/task_view_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def talos_ext_task_view_dashboard(self, **kwargs):
        env = request.env
        role_tag = _user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Talos task view.",
                status=403,
            )
        scope = _scope_domain(env)
        params = request.params or {}

        domain, error = _build_task_view_domain(env, params)
        if error is not None:
            return error
        domain = scope + domain

        page = max(1, _coerce_int(params.get("page"), 1))
        limit = _coerce_int(params.get("limit"), LIST_DEFAULT_LIMIT)
        limit = max(1, min(limit, LIST_MAX_LIMIT))
        offset = (page - 1) * limit

        Talos = env["talos.talos"].sudo()
        total = Talos.search_count(domain)
        records = Talos.search(
            domain,
            limit=limit,
            offset=offset,
            order="write_date desc, id desc",
        )

        tasks = [_serialize_task(t) for t in records]
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag,
                "columns": [
                    {"key": "seq", "label": "Reference", "type": "string"},
                    {"key": "raw_prompt", "label": "Raw Prompt", "type": "string"},
                    {"key": "golden_prompt", "label": "Golden Prompt", "type": "string"},
                    {"key": "assigned_ql_name", "label": "QL", "type": "string"},
                    {"key": "category", "label": "Category", "type": "string"},
                    {"key": "stage_label", "label": "Stage", "type": "string"},
                    {"key": "status", "label": "Status", "type": "string"},
                    {"key": "cost_usd", "label": "Cost", "type": "currency"},
                    {"key": "updated_at", "label": "Updated", "type": "datetime"},
                ],
                "rows": tasks,
                "total_records": total,
                "page": page,
                "limit": limit,
            },
        )
