# -*- coding: utf-8 -*-
"""
Vindex Portal Controller — serves the modern web portal for task annotation.

Routes:
    GET  /vindex/tasks              — Task list page
    GET  /vindex/tasks/<id>         — Task detail / annotation page
    POST /vindex/tasks/<id>/save    — Save human-editable fields (JSON)
    POST /vindex/tasks/<id>/evaluate — Trigger evaluate_task (JSON)
    POST /vindex/tasks/<id>/submit  — Submit task (JSON)
"""

import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

# Dimensions used across all scoring sections
DIMS = [
    "truthfulness",
    "instruction_following",
    "writing_quality",
    "verbosity",
    "prompt_correctness",
    "overall_quality",
]

# Fields that taskers are allowed to write through the portal
HUMAN_EDITABLE_FIELDS = set()

# Section 1: Core A/B
for _d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"{_d}_a")
    HUMAN_EDITABLE_FIELDS.add(f"{_d}_b")
HUMAN_EDITABLE_FIELDS.update(
    [
        "ab_preference",
        "ab_comment",
        "rejection_reason",
    ]
)

# Section 2: Ophelia / Opalite
for _d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"ophelia_{_d}_a")
    HUMAN_EDITABLE_FIELDS.add(f"opalite_{_d}_b")
HUMAN_EDITABLE_FIELDS.update(
    [
        "enhance_ab_preference",
        "enhance_ab_comment",
    ]
)

# Section 3: GPT SxS
for _d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"gpt_{_d}_a")
HUMAN_EDITABLE_FIELDS.update(
    [
        "gpts_ab_preference",
        "gpts_ab_comment",
    ]
)

# Section 4: Gemini SxS
for _d in DIMS:
    HUMAN_EDITABLE_FIELDS.add(f"gemini_{_d}_b")
HUMAN_EDITABLE_FIELDS.update(
    [
        "geminis_ab_preference",
        "geminis_ab_comment",
    ]
)

# Section 5: Rubrics
for _n in range(1, 6):
    HUMAN_EDITABLE_FIELDS.add(f"rubric{_n}_name")
    HUMAN_EDITABLE_FIELDS.add(f"rubric{_n}_description")
    for _model in ("ophelia", "opalite", "gpt", "gemini"):
        HUMAN_EDITABLE_FIELDS.add(f"{_model}_rubric{_n}_rating")

# Section 6: External model comparisons
HUMAN_EDITABLE_FIELDS.update(
    [
        "gpt_preference",
        "gpt_comment",
        "gemini_preference",
        "gemini_comment",
    ]
)

# Section 7: Enhanced prompt
HUMAN_EDITABLE_FIELDS.add("enhance_prompt")


def _check_vindex_access(user):
    """Verify that the user has at least tasker-level Vindex access.
    Returns the role string or raises AccessError."""
    if user.has_group("preference_ranking.group_vindex_admin"):
        return "admin"
    if user.has_group("preference_ranking.group_vindex_ql"):
        return "quality_lead"
    if user.has_group("preference_ranking.group_vindex_tasker"):
        return "tasker"
    raise AccessError("You do not have access to Vindex.")


def _json_response(data, status=200):
    """Return a JSON HTTP response."""
    body = json.dumps(data, default=str)
    return http.Response(body, content_type="application/json", status=status)


def _build_scores_section(record, prefix, suffix):
    """Build a structured scores dict for a response section."""
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
    return {
        "preference": getattr(record, pref_field, None) or None,
        "store_preference": getattr(record, f"store_{pref_field}", None) or None,
        "comment": getattr(record, comment_field, None) or "",
        "store_comment": getattr(record, f"store_{comment_field}", None) or "",
        "error_preference": bool(getattr(record, f"error_{pref_field}", False)),
        "error_comment": bool(getattr(record, f"error_{comment_field}", False)),
        "reason_preference": getattr(record, f"reason1_{pref_field}", None) or "",
        "reason_comment": getattr(record, f"reason1_{comment_field}", None) or "",
    }


def _build_rubrics(record):
    """Build the rubrics array (5 rubrics x 4 models)."""
    rubrics = []
    for n in range(1, 6):
        rubric = {
            "index": n,
            "name": getattr(record, f"rubric{n}_name", None) or "",
            "store_name": getattr(record, f"store_rubric{n}_name", None) or "",
            "description": getattr(record, f"rubric{n}_description", None) or "",
            "store_description": getattr(record, f"store_rubric{n}_description", None)
            or "",
            "error_name": bool(getattr(record, f"error_rubric{n}_name", False)),
            "error_description": bool(
                getattr(record, f"error_rubric{n}_description", False)
            ),
            "reason_name": getattr(record, f"reason1_rubric{n}_name", None) or "",
            "reason_description": getattr(
                record, f"reason1_rubric{n}_description", None
            )
            or "",
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


def _serialize_task_detail(record):
    """Full task serialization for the detail page."""
    return {
        "id": record.id,
        "task_id": record.task_id or "",
        "task_status": record.task_status or "NotSubmitted",
        "is_processed": bool(record.is_processed),
        "is_ratable": bool(record.is_ratable),
        "is_eval_done": bool(record.is_eval_done),
        "is_randomized": bool(record.is_randomized),
        "client_prompt": record.client_prompt or "",
        "enhance_prompt": record.enhance_prompt or "",
        "client_response_a": record.client_response_a or "",
        "client_response_b": record.client_response_b or "",
        "ophelia_response_a": record.ophelia_response_a or "",
        "opalite_response_b": record.opalite_response_b or "",
        "gpt_response": record.gpt_response or "",
        "gemini_response": record.gemini_response or "",
        "scores": {
            "response_a": _build_scores_section(record, "", "_a"),
            "response_b": _build_scores_section(record, "", "_b"),
            "ophelia_a": _build_scores_section(record, "ophelia_", "_a"),
            "opalite_b": _build_scores_section(record, "opalite_", "_b"),
            "gpt_sxs": _build_scores_section(record, "gpt_", "_a"),
            "gemini_sxs": _build_scores_section(record, "gemini_", "_b"),
        },
        "comparisons": {
            "ab": _build_comparison(record, "ab_preference", "ab_comment"),
            "enhance_ab": _build_comparison(
                record, "enhance_ab_preference", "enhance_ab_comment"
            ),
            "gpts_ab": _build_comparison(
                record, "gpts_ab_preference", "gpts_ab_comment"
            ),
            "geminis_ab": _build_comparison(
                record, "geminis_ab_preference", "geminis_ab_comment"
            ),
            "gpt_external": _build_comparison(record, "gpt_preference", "gpt_comment"),
            "gemini_external": _build_comparison(
                record, "gemini_preference", "gemini_comment"
            ),
        },
        "rubrics": _build_rubrics(record),
        "rejection": {
            "prompt_rejection_reason": record.prompt_rejection_reason or None,
            "rejection_reason": record.rejection_reason or None,
        },
        "qc": {
            "qc_task_status": record.qc_task_status or None,
            "qc_score": record.qc_score or 0,
        },
        "employee_name": record.employee_id.name if record.employee_id else "",
        "employee_id": record.employee_id.id if record.employee_id else None,
    }


class VindexPortalController(http.Controller):
    """Portal routes for the Vindex annotation interface."""

    # ------------------------------------------------------------------
    # Page: Task List
    # ------------------------------------------------------------------
    @http.route(
        "/vindex/tasks",
        type="http",
        auth="user",
        website=True,
    )
    def portal_task_list(self, page=1, status=None, search=None, **kw):
        """Render the task list page."""
        user = request.env.user
        role = _check_vindex_access(user)

        Model = request.env["preference.ranking"].sudo()
        domain = []

        # Taskers only see their own tasks
        if role == "tasker":
            domain.append(("user_id", "=", user.id))

        if status in ("Submitted", "NotSubmitted"):
            domain.append(("task_status", "=", status))

        if search:
            domain += [
                "|",
                ("task_id", "ilike", search),
                ("client_prompt", "ilike", search),
            ]

        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1

        limit = 20
        offset = (page - 1) * limit
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order="id desc")

        total_pages = max(1, (total + limit - 1) // limit)

        tasks = []
        for r in records:
            prompt = r.client_prompt or ""
            tasks.append(
                {
                    "id": r.id,
                    "task_id": r.task_id or "",
                    "task_status": r.task_status or "NotSubmitted",
                    "is_processed": bool(r.is_processed),
                    "is_ratable": bool(r.is_ratable),
                    "is_eval_done": bool(r.is_eval_done),
                    "qc_task_status": r.qc_task_status or "",
                    "qc_score": r.qc_score or 0,
                    "client_prompt_preview": prompt[:200]
                    + ("..." if len(prompt) > 200 else ""),
                }
            )

        values = {
            "tasks": tasks,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "status_filter": status or "",
            "search_filter": search or "",
            "role": role,
            "page_name": "vindex_tasks",
        }
        return request.render("preference_ranking.portal_task_list", values)

    # ------------------------------------------------------------------
    # Page: Task Detail / Annotation
    # ------------------------------------------------------------------
    @http.route(
        "/vindex/tasks/<int:task_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_task_detail(self, task_id, **kw):
        """Render the full annotation interface for a single task."""
        user = request.env.user
        role = _check_vindex_access(user)

        record = request.env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return request.redirect("/vindex/tasks")

        # Taskers can only view their own tasks
        if role == "tasker" and record.user_id and record.user_id.id != user.id:
            return request.redirect("/vindex/tasks")

        task_data = _serialize_task_detail(record)

        values = {
            "task": task_data,
            "task_json": json.dumps(task_data, default=str),
            "role": role,
            "page_name": "vindex_task_detail",
        }
        return request.render("preference_ranking.portal_task_detail", values)

    # ------------------------------------------------------------------
    # API: Save fields
    # ------------------------------------------------------------------
    @http.route(
        "/vindex/tasks/<int:task_id>/save",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def portal_save_task(self, task_id, **kw):
        """Save human-editable fields. Accepts JSON body."""
        user = request.env.user
        role = _check_vindex_access(user)

        record = request.env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return _json_response({"success": False, "message": "Task not found"}, 404)

        if role == "tasker" and record.user_id and record.user_id.id != user.id:
            return _json_response({"success": False, "message": "Access denied"}, 403)

        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (json.JSONDecodeError, Exception):
            return _json_response({"success": False, "message": "Invalid JSON"}, 400)

        vals = {k: v for k, v in body.items() if k in HUMAN_EDITABLE_FIELDS}
        if not vals:
            return _json_response({"success": False, "message": "No valid fields"}, 400)

        try:
            record.write(vals)
            return _json_response({"success": True, "updated": list(vals.keys())})
        except Exception as e:
            _logger.error("Portal save failed for task %s: %s", task_id, e)
            return _json_response({"success": False, "message": str(e)}, 500)

    # ------------------------------------------------------------------
    # API: Evaluate scores
    # ------------------------------------------------------------------
    @http.route(
        "/vindex/tasks/<int:task_id>/evaluate",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def portal_evaluate_task(self, task_id, **kw):
        """Trigger evaluate_task() to compare human vs LLM scores."""
        user = request.env.user
        role = _check_vindex_access(user)

        record = request.env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return _json_response({"success": False, "message": "Task not found"}, 404)

        if role == "tasker" and record.user_id and record.user_id.id != user.id:
            return _json_response({"success": False, "message": "Access denied"}, 403)

        try:
            record.evaluate_task()

            error_count = 0
            for field_name in record._fields:
                if field_name.startswith("error_") and getattr(
                    record, field_name, False
                ):
                    error_count += 1

            # Re-serialize the task for the frontend to update
            task_data = _serialize_task_detail(record)

            return _json_response(
                {
                    "success": True,
                    "errors_found": error_count,
                    "task": task_data,
                }
            )
        except Exception as e:
            _logger.error("Portal evaluate failed for task %s: %s", task_id, e)
            return _json_response({"success": False, "message": str(e)}, 500)

    # ------------------------------------------------------------------
    # API: Submit task
    # ------------------------------------------------------------------
    @http.route(
        "/vindex/tasks/<int:task_id>/submit",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def portal_submit_task(self, task_id, **kw):
        """Submit a completed task."""
        user = request.env.user
        role = _check_vindex_access(user)

        record = request.env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return _json_response({"success": False, "message": "Task not found"}, 404)

        if role == "tasker" and record.user_id and record.user_id.id != user.id:
            return _json_response({"success": False, "message": "Access denied"}, 403)

        try:
            record.submit_task()
            return _json_response(
                {
                    "success": True,
                    "task_status": record.task_status,
                }
            )
        except Exception as e:
            _logger.error("Portal submit failed for task %s: %s", task_id, e)
            return _json_response({"success": False, "message": str(e)}, 400)
