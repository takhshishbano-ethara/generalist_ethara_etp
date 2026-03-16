# -*- coding: utf-8 -*-
"""
Task Management API endpoints.

GET    /api/v1/tasks           — List tasks (paginated, filtered)
GET    /api/v1/tasks/<id>      — Get single task detail
PATCH  /api/v1/tasks/<id>      — Update task fields (human ratings)
POST   /api/v1/tasks/<id>/submit — Submit task
POST   /api/v1/tasks/create    — Create task(s) manually
"""
import logging

from odoo import http
from odoo.http import request

from .helpers import (
    json_error,
    json_success,
    jwt_required,
    paginate_params,
    parse_json_body,
)

_logger = logging.getLogger(__name__)

# Dimensions used across all sections
DIMS = [
    "truthfulness",
    "instruction_following",
    "writing_quality",
    "verbosity",
    "prompt_correctness",
    "overall_quality",
]

# Fields that taskers are allowed to write through the API
HUMAN_EDITABLE_FIELDS = set()

# Section 1: Core A/B
for d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"{d}_a")
    HUMAN_EDITABLE_FIELDS.add(f"{d}_b")
HUMAN_EDITABLE_FIELDS.update([
    "ab_preference", "ab_comment", "rejection_reason",
])

# Section 2: Ophelia / Opalite
for d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"ophelia_{d}_a")
    HUMAN_EDITABLE_FIELDS.add(f"opalite_{d}_b")
HUMAN_EDITABLE_FIELDS.update([
    "enhance_ab_preference", "enhance_ab_comment",
])

# Section 3: GPT SxS
for d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"gpt_{d}_a")
HUMAN_EDITABLE_FIELDS.update([
    "gpts_ab_preference", "gpts_ab_comment",
])

# Section 4: Gemini SxS
for d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"gemini_{d}_b")
HUMAN_EDITABLE_FIELDS.update([
    "geminis_ab_preference", "geminis_ab_comment",
])

# Section 5: Rubrics
for n in range(1, 6):
    HUMAN_EDITABLE_FIELDS.add(f"rubric{n}_name")
    HUMAN_EDITABLE_FIELDS.add(f"rubric{n}_description")
    for model in ("ophelia", "opalite", "gpt", "gemini"):
        HUMAN_EDITABLE_FIELDS.add(f"{model}_rubric{n}_rating")

# Section 6: External model comparisons
HUMAN_EDITABLE_FIELDS.update([
    "gpt_preference", "gpt_comment",
    "gemini_preference", "gemini_comment",
])

# Section 7: Enhanced prompt
HUMAN_EDITABLE_FIELDS.add("enhance_prompt")


def _build_scores_section(record, prefix, suffix):
    """Build a structured scores dict for a response section.

    Args:
        record: preference.ranking browse record
        prefix: e.g. '' (core), 'ophelia_', 'gpt_', 'gemini_'
        suffix: e.g. '_a', '_b'
    """
    section = {}
    for d in DIMS:
        human_field = f"{prefix}{d}{suffix}"
        store_field = f"store_{prefix}{d}{suffix}"
        error_field = f"error_{prefix}{d}{suffix}"
        reason_field = f"reason1_{prefix}{d}{suffix}"
        section[d] = {
            "human": getattr(record, human_field, None) or None,
            "llm": getattr(record, store_field, None) or None,
            "error": bool(getattr(record, error_field, False)),
            "reason": getattr(record, reason_field, None) or "",
        }
    return section


def _build_comparison(record, pref_field, comment_field):
    """Build a comparison dict."""
    error_pref_field = f"error_{pref_field}" if hasattr(record, f"error_{pref_field}") else None
    error_comm_field = f"error_{comment_field}" if hasattr(record, f"error_{comment_field}") else None
    reason_pref_field = f"reason1_{pref_field}" if hasattr(record, f"reason1_{pref_field}") else None
    reason_comm_field = f"reason1_{comment_field}" if hasattr(record, f"reason1_{comment_field}") else None

    return {
        "preference": getattr(record, pref_field, None) or None,
        "store_preference": getattr(record, f"store_{pref_field}", None) or None,
        "comment": getattr(record, comment_field, None) or "",
        "store_comment": getattr(record, f"store_{comment_field}", None) or "",
        "error_preference": bool(getattr(record, error_pref_field, False)) if error_pref_field else False,
        "error_comment": bool(getattr(record, error_comm_field, False)) if error_comm_field else False,
        "reason_preference": getattr(record, reason_pref_field, None) or "" if reason_pref_field else "",
        "reason_comment": getattr(record, reason_comm_field, None) or "" if reason_comm_field else "",
    }


def _build_rubrics(record):
    """Build the rubrics array (5 rubrics × 4 models)."""
    rubrics = []
    for n in range(1, 6):
        rubric = {
            "index": n,
            "name": getattr(record, f"rubric{n}_name", None) or "",
            "store_name": getattr(record, f"store_rubric{n}_name", None) or "",
            "description": getattr(record, f"rubric{n}_description", None) or "",
            "store_description": getattr(record, f"store_rubric{n}_description", None) or "",
            "error_name": bool(getattr(record, f"error_rubric{n}_name", False)),
            "error_description": bool(getattr(record, f"error_rubric{n}_description", False)),
            "reason_name": getattr(record, f"reason1_rubric{n}_name", None) or "",
            "reason_description": getattr(record, f"reason1_rubric{n}_description", None) or "",
            "ratings": {},
        }
        for model in ("ophelia", "opalite", "gpt", "gemini"):
            field = f"{model}_rubric{n}_rating"
            store_field = f"store_{model}_rubric{n}_rating"
            error_field = f"error_{model}_rubric{n}_rating"
            reason_field = f"reason1_{model}_rubric{n}_rating"
            rubric["ratings"][model] = {
                "score": getattr(record, field, None) or None,
                "store_score": getattr(record, store_field, None) or None,
                "error": bool(getattr(record, error_field, False)),
                "reason": getattr(record, reason_field, None) or "",
            }
        rubrics.append(rubric)
    return rubrics


def _task_to_list_dict(record):
    """Serialize a task record for the list view (lightweight)."""
    prompt = getattr(record, "client_prompt", "") or ""
    return {
        "id": record.id,
        "task_id": record.task_id or "",
        "task_status": record.task_status or "NotSubmitted",
        "is_processed": bool(record.is_processed),
        "is_ratable": bool(record.is_ratable),
        "is_eval_done": bool(record.is_eval_done),
        "qc_task_status": record.qc_task_status or None,
        "qc_score": record.qc_score or 0,
        "employee_name": record.employee_id.name if record.employee_id else "",
        "client_prompt_preview": prompt[:200] + ("..." if len(prompt) > 200 else ""),
        "is_randomized": bool(record.is_randomized),
        "created_at": record.create_date.isoformat() if record.create_date else None,
    }


def _task_to_detail_dict(record):
    """Serialize a task record for the detail view (full payload)."""
    return {
        "id": record.id,
        "task_id": record.task_id or "",
        "task_status": record.task_status or "NotSubmitted",
        "is_processed": bool(record.is_processed),
        "is_ratable": bool(record.is_ratable),
        "is_eval_done": bool(record.is_eval_done),
        "is_randomized": bool(record.is_randomized),

        # Prompts
        "client_prompt": record.client_prompt or "",
        "enhance_prompt": record.enhance_prompt or "",

        # Client responses
        "client_response_a": record.client_response_a or "",
        "client_response_b": record.client_response_b or "",

        # LLM-generated responses
        "ophelia_response_a": record.ophelia_response_a or "",
        "opalite_response_b": record.opalite_response_b or "",
        "gpt_response": record.gpt_response or "",
        "gemini_response": record.gemini_response or "",

        # Scores by section
        "scores": {
            "response_a": _build_scores_section(record, "", "_a"),
            "response_b": _build_scores_section(record, "", "_b"),
            "ophelia_a": _build_scores_section(record, "ophelia_", "_a"),
            "opalite_b": _build_scores_section(record, "opalite_", "_b"),
            "gpt_sxs": _build_scores_section(record, "gpt_", "_a"),
            "gemini_sxs": _build_scores_section(record, "gemini_", "_b"),
        },

        # Comparisons
        "comparisons": {
            "ab": _build_comparison(record, "ab_preference", "ab_comment"),
            "enhance_ab": _build_comparison(record, "enhance_ab_preference", "enhance_ab_comment"),
            "gpts_ab": _build_comparison(record, "gpts_ab_preference", "gpts_ab_comment"),
            "geminis_ab": _build_comparison(record, "geminis_ab_preference", "geminis_ab_comment"),
            "gpt_external": _build_comparison(record, "gpt_preference", "gpt_comment"),
            "gemini_external": _build_comparison(record, "gemini_preference", "gemini_comment"),
        },

        # Rubrics
        "rubrics": _build_rubrics(record),

        # Rejection
        "rejection": {
            "prompt_rejection_reason": record.prompt_rejection_reason or None,
            "rejection_reason": record.rejection_reason or None,
        },

        # QC
        "qc": {
            "qc_task_status": record.qc_task_status or None,
            "qc_score": record.qc_score or 0,
        },

        # Metadata
        "employee_name": record.employee_id.name if record.employee_id else "",
        "employee_id": record.employee_id.id if record.employee_id else None,
        "created_at": record.create_date.isoformat() if record.create_date else None,
        "updated_at": record.write_date.isoformat() if record.write_date else None,
    }


class TaskController(http.Controller):
    """REST endpoints for task CRUD operations."""

    # ------------------------------------------------------------------
    # T1: List Tasks
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def list_tasks(self, **params):
        """List tasks with pagination and filtering."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        page, limit, offset = paginate_params(params)

        # Build domain
        domain = []

        # Role-based filtering: taskers only see own tasks
        if request.jwt_payload.get("role") == "tasker":
            domain.append(("user_id", "=", request.jwt_uid))
        elif params.get("employee_id"):
            try:
                domain.append(("employee_id", "=", int(params["employee_id"])))
            except (ValueError, TypeError):
                pass

        # Status filter
        if params.get("status") in ("Submitted", "NotSubmitted"):
            domain.append(("task_status", "=", params["status"]))

        # Boolean filters
        for bf in ("is_processed", "is_ratable", "is_eval_done"):
            val = params.get(bf)
            if val is not None and val != "":
                domain.append((bf, "=", val.lower() in ("true", "1", "yes")))

        # QC status filter
        if params.get("qc_status") in ("pass", "fail"):
            domain.append(("qc_task_status", "=", params["qc_status"]))

        # Search filter
        search = params.get("search", "").strip()
        if search:
            domain.append("|")
            domain.append(("task_id", "ilike", search))
            domain.append(("client_prompt", "ilike", search))

        # Sort
        sort_field = params.get("sort", "id")
        order_dir = params.get("order", "desc").lower()
        if order_dir not in ("asc", "desc"):
            order_dir = "desc"
        # Whitelist sortable fields
        sortable = {"id", "task_id", "task_status", "create_date", "write_date", "qc_score"}
        if sort_field not in sortable:
            sort_field = "id"
        order = f"{sort_field} {order_dir}"

        Model = env["preference.ranking"].sudo()
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order=order)

        tasks = [_task_to_list_dict(r) for r in records]

        return json_success(
            data=tasks,
            pagination={
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit if limit else 1,
            },
        )

    # ------------------------------------------------------------------
    # T2: Get Task Detail
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def get_task(self, task_id, **params):
        """Get full task detail by ID."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)

        if not record.exists():
            return json_error("Task not found", 404)

        # Taskers can only see their own tasks
        if request.jwt_payload.get("role") == "tasker":
            if record.user_id and record.user_id.id != request.jwt_uid:
                return json_error("Task not found", 404)

        return json_success(data=_task_to_detail_dict(record))

    # ------------------------------------------------------------------
    # T3: Create Tasks
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/create",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["admin"])
    def create_tasks(self, **kw):
        """Create one or more tasks manually.

        Body: {"tasks": [{"task_id": "...", "client_prompt": "...", ...}]}
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        body = parse_json_body()
        tasks_data = body.get("tasks", [])

        if not tasks_data:
            return json_error("tasks array is required", 400)

        env = request.jwt_env
        created_ids = []

        for task in tasks_data:
            vals = {
                "task_id": task.get("task_id", ""),
                "client_prompt": task.get("client_prompt", ""),
                "client_response_a": task.get("client_response_a", ""),
                "client_response_b": task.get("client_response_b", ""),
                "is_randomized": task.get("is_randomized", False),
            }
            if task.get("employee_id"):
                vals["employee_id"] = task["employee_id"]
            rec = env["preference.ranking"].sudo().create(vals)
            created_ids.append(rec.id)

        return json_success(
            data={"created_ids": created_ids, "count": len(created_ids)},
            message=f"Created {len(created_ids)} task(s)",
            status=201,
        )

    # ------------------------------------------------------------------
    # T4: Update Task (Partial — human ratings)
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>",
        type="http",
        auth="none",
        methods=["PATCH", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def update_task(self, task_id, **kw):
        """Update human-editable fields on a task.

        Body: {"truthfulness_a": "4", "ab_comment": "...", ...}
        Only fields in HUMAN_EDITABLE_FIELDS are accepted.
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)

        if not record.exists():
            return json_error("Task not found", 404)

        # Taskers can only update their own tasks
        if request.jwt_payload.get("role") == "tasker":
            if record.user_id and record.user_id.id != request.jwt_uid:
                return json_error("Task not found", 404)

        body = parse_json_body()
        if not body:
            return json_error("Request body is required", 400)

        # Filter to allowed fields only
        vals = {}
        rejected_fields = []
        for key, value in body.items():
            if key in HUMAN_EDITABLE_FIELDS:
                vals[key] = value
            else:
                rejected_fields.append(key)

        if not vals:
            return json_error(
                "No valid fields to update",
                400,
                errors={"rejected_fields": rejected_fields},
            )

        try:
            record.write(vals)
        except Exception as e:
            _logger.error("Failed to update task %s: %s", task_id, e)
            return json_error(f"Update failed: {e}", 500)

        result = {"updated_fields": list(vals.keys())}
        if rejected_fields:
            result["rejected_fields"] = rejected_fields

        return json_success(data=result, message="Task updated")

    # ------------------------------------------------------------------
    # T5: Submit Task
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/submit",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def submit_task(self, task_id, **kw):
        """Submit a completed task (calls model's submit_task method)."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)

        if not record.exists():
            return json_error("Task not found", 404)

        # Taskers can only submit their own tasks
        if request.jwt_payload.get("role") == "tasker":
            if record.user_id and record.user_id.id != request.jwt_uid:
                return json_error("Task not found", 404)

        try:
            record.submit_task()
        except Exception as e:
            return json_error(str(e), 400)

        return json_success(
            data={"task_status": record.task_status},
            message="Task submitted successfully",
        )
