# -*- coding: utf-8 -*-
"""
Dashboard & Analytics API endpoints.

GET /api/v1/dashboard/stats           — Overview stats
GET /api/v1/dashboard/employee-stats  — Per-employee completion stats
GET /api/v1/dashboard/token-usage     — Token usage summary
GET /api/v1/dashboard/qc-summary      — QC pass/fail breakdown
GET /api/v1/users                     — List users
GET /api/v1/employees                 — List employees with task counts
"""
import logging

from odoo import http
from odoo.http import request

from .helpers import (
    json_error,
    json_success,
    jwt_required,
    paginate_params,
)

_logger = logging.getLogger(__name__)


class DashboardController(http.Controller):
    """REST endpoints for dashboard analytics and user management."""

    # ------------------------------------------------------------------
    # D1: Overview Stats
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/dashboard/stats",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["quality_lead", "admin"])
    def dashboard_stats(self, **kw):
        """Return overview statistics for all tasks."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        Model = env["preference.ranking"].sudo()

        total = Model.search_count([])
        submitted = Model.search_count([("task_status", "=", "Submitted")])
        not_submitted = Model.search_count([("task_status", "=", "NotSubmitted")])
        processed = Model.search_count([("is_processed", "=", True)])
        ratable = Model.search_count([("is_ratable", "=", True)])
        eval_done = Model.search_count([("is_eval_done", "=", True)])
        qc_passed = Model.search_count([("qc_task_status", "=", "pass")])
        qc_failed = Model.search_count([("qc_task_status", "=", "fail")])
        qc_pending = Model.search_count([
            ("is_processed", "=", True),
            ("qc_task_status", "=", False),
        ])

        return json_success(data={
            "total_tasks": total,
            "submitted": submitted,
            "not_submitted": not_submitted,
            "processed": processed,
            "ratable": ratable,
            "eval_done": eval_done,
            "qc_passed": qc_passed,
            "qc_failed": qc_failed,
            "qc_pending": qc_pending,
            "by_status": {
                "Submitted": submitted,
                "NotSubmitted": not_submitted,
            },
        })

    # ------------------------------------------------------------------
    # D2: Employee Stats
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/dashboard/employee-stats",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["quality_lead", "admin"])
    def employee_stats(self, **kw):
        """Return per-employee task completion stats."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        # Use raw SQL for efficiency
        env.cr.execute("""
            SELECT
                e.id AS employee_id,
                e.name AS employee_name,
                COUNT(pr.id) AS total_tasks,
                COUNT(CASE WHEN pr.task_status = 'Submitted' THEN 1 END) AS submitted,
                COUNT(CASE WHEN pr.task_status = 'NotSubmitted' THEN 1 END) AS not_submitted,
                COUNT(CASE WHEN pr.is_processed = true THEN 1 END) AS processed,
                COUNT(CASE WHEN pr.qc_task_status = 'pass' THEN 1 END) AS qc_passed,
                COUNT(CASE WHEN pr.qc_task_status = 'fail' THEN 1 END) AS qc_failed
            FROM preference_ranking pr
            LEFT JOIN hr_employee e ON pr.employee_id = e.id
            GROUP BY e.id, e.name
            ORDER BY total_tasks DESC
        """)
        rows = env.cr.dictfetchall()

        return json_success(data=[{
            "employee_id": r["employee_id"],
            "employee_name": r["employee_name"] or "Unassigned",
            "total_tasks": r["total_tasks"],
            "submitted": r["submitted"],
            "not_submitted": r["not_submitted"],
            "processed": r["processed"],
            "qc_passed": r["qc_passed"],
            "qc_failed": r["qc_failed"],
        } for r in rows])

    # ------------------------------------------------------------------
    # D3: Token Usage
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/dashboard/token-usage",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["admin"])
    def token_usage(self, **kw):
        """Return aggregated token usage stats by model and type."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env

        # By model
        env.cr.execute("""
            SELECT
                t.ai_model_type,
                SUM(tl.input_token) AS input_tokens,
                SUM(tl.output_token) AS output_tokens,
                SUM(tl.cache_token) AS cached_tokens,
                SUM(tl.cost) AS total_cost
            FROM preference_ranking_token t
            JOIN preference_ranking_token_line tl ON tl.token_id = t.id
            GROUP BY t.ai_model_type
        """)
        by_model_rows = env.cr.dictfetchall()

        by_model = {}
        total_cost = 0.0
        for r in by_model_rows:
            model = r["ai_model_type"] or "unknown"
            cost = float(r["total_cost"] or 0)
            total_cost += cost
            by_model[model] = {
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cached_tokens": int(r["cached_tokens"] or 0),
                "cost_usd": round(cost, 4),
            }

        # By type
        env.cr.execute("""
            SELECT
                t.type,
                SUM(tl.cost) AS total_cost,
                COUNT(DISTINCT t.id) AS count
            FROM preference_ranking_token t
            JOIN preference_ranking_token_line tl ON tl.token_id = t.id
            GROUP BY t.type
        """)
        by_type_rows = env.cr.dictfetchall()

        by_type = {}
        for r in by_type_rows:
            by_type[r["type"] or "unknown"] = {
                "total_cost": round(float(r["total_cost"] or 0), 4),
                "count": r["count"],
            }

        return json_success(data={
            "total_cost_usd": round(total_cost, 4),
            "by_model": by_model,
            "by_type": by_type,
        })

    # ------------------------------------------------------------------
    # D4: QC Summary
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/dashboard/qc-summary",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["quality_lead", "admin"])
    def qc_summary(self, **kw):
        """Return QC pass/fail breakdown with average scores."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env

        env.cr.execute("""
            SELECT
                qc_task_status,
                COUNT(*) AS count,
                AVG(qc_score) AS avg_score
            FROM preference_ranking
            WHERE qc_task_status IS NOT NULL AND qc_task_status != ''
            GROUP BY qc_task_status
        """)
        rows = env.cr.dictfetchall()

        summary = {}
        for r in rows:
            summary[r["qc_task_status"]] = {
                "count": r["count"],
                "avg_score": round(float(r["avg_score"] or 0), 2),
            }

        total_qc = sum(s["count"] for s in summary.values())

        return json_success(data={
            "total_qc_reviewed": total_qc,
            "breakdown": summary,
        })

    # ------------------------------------------------------------------
    # U1: List Users
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/users",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["admin"])
    def list_users(self, **params):
        """List users for admin assignment views."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        page, limit, offset = paginate_params(params)

        users = env["res.users"].sudo().search(
            [("active", "=", True)],
            limit=limit,
            offset=offset,
            order="name asc",
        )
        total = env["res.users"].sudo().search_count([("active", "=", True)])

        data = []
        for u in users:
            from .helpers import _resolve_role
            data.append({
                "id": u.id,
                "name": u.name,
                "login": u.login,
                "role": _resolve_role(u),
            })

        return json_success(
            data=data,
            pagination={
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit if limit else 1,
            },
        )

    # ------------------------------------------------------------------
    # U2: List Employees
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/employees",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["quality_lead", "admin"])
    def list_employees(self, **params):
        """List employees with task counts."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        page, limit, offset = paginate_params(params)

        employees = env["hr.employee"].sudo().search(
            [], limit=limit, offset=offset, order="name asc"
        )
        total = env["hr.employee"].sudo().search_count([])

        data = []
        Model = env["preference.ranking"].sudo()
        for emp in employees:
            task_count = Model.search_count([("employee_id", "=", emp.id)])
            submitted_count = Model.search_count([
                ("employee_id", "=", emp.id),
                ("task_status", "=", "Submitted"),
            ])
            data.append({
                "id": emp.id,
                "name": emp.name,
                "user_id": emp.user_id.id if emp.user_id else None,
                "total_tasks": task_count,
                "submitted_tasks": submitted_count,
            })

        return json_success(
            data=data,
            pagination={
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit if limit else 1,
            },
        )
