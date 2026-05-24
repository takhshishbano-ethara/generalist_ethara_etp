# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _parse_trajectories(task):
    """Extract per-trajectory token data from the task's JSON trajectory fields."""
    trajectories = []
    field_map = {
        "claude": "claude_trajectory",
        "gpt": "gpt_trajectory",
    }
    for model_key, field_name in field_map.items():
        raw = getattr(task, field_name, None) or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
            entries = parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            continue
        for idx, entry in enumerate(entries):
            t_in = int(entry.get("tokens_in", 0) or 0)
            t_out = int(entry.get("tokens_out", 0) or 0)
            trajectories.append({
                "model": model_key,
                "index": idx + 1,
                "tokens_in": t_in,
                "tokens_out": t_out,
                "tokens_total": t_in + t_out,
                "timestamp": entry.get("timestamp", ""),
            })
    return trajectories


def _get_test_results_for_task(task):
    TestResult = task.env["kensei2.test.result"].sudo()
    results = TestResult.search(
        [("kensei2_id", "=", task.id)],
        order="create_date desc",
        limit=20,
    )
    grouped = {}
    for r in results:
        sb = r.sandbox_id
        model_type = sb.model_type if sb else "unknown"
        if model_type not in grouped:
            grouped[model_type] = []
        grouped[model_type].append({
            "id": r.id,
            "model_used": r.model_used or "",
            "status": r.status or "",
            "tests_total": r.tests_total,
            "tests_passed": r.tests_passed,
            "tests_failed": r.tests_failed,
            "tests_errored": r.tests_errored,
            "trajectory_index": r.trajectory_index or 0,
            "duration_exec_ms": r.duration_execution_ms or 0,
            "duration_gen_ms": r.duration_generation_ms or 0,
            "create_date": str(r.create_date) if r.create_date else "",
        })
    return grouped


def _task_row(task):
    ci = task.claude_input_tokens or 0
    co = task.claude_output_tokens or 0
    gpi = task.gpt_input_tokens or 0
    gpo = task.gpt_output_tokens or 0

    ct = ci + co
    gpt = gpi + gpo

    test_results = _get_test_results_for_task(task)
    tests_total = 0
    tests_passed = 0
    tests_failed = 0
    for model_results in test_results.values():
        for r in model_results:
            tests_total += r.get("tests_total", 0) or 0
            tests_passed += r.get("tests_passed", 0) or 0
            tests_failed += r.get("tests_failed", 0) or 0

    row = {
        "task_id": task.id,
        "task_name": task.task_id or ("Task #%d" % task.id),
        "claude_input": ci,
        "claude_output": co,
        "claude_total": ct,
        "gpt_input": gpi,
        "gpt_output": gpo,
        "gpt_total": gpt,
        "grand_total": ct + gpt,
        "tests_total": tests_total,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "trajectories": _parse_trajectories(task),
        "test_results": test_results,
    }


class Kensei2CostingController(http.Controller):
    @http.route("/kensei2/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        if not request.env.user.has_group("kensei2.group_kensei2_pl"):
            raise AccessError("Only Project Leads can access costing data.")
        today = datetime.now().date()

        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = None

        Task = request.env["kensei2.kensei2"].sudo()
        task_domain = []
        if start:
            task_domain.append(
                ("create_date", ">=", start.strftime("%Y-%m-%d 00:00:00"))
            )
        tasks = Task.search(task_domain)

        emp_map = {}

        for task in tasks:
            employees = task.employee_ids or task.employee_id
            if not employees:
                employees_list = [(0, "Unassigned")]
            else:
                employees_list = [(e.id, e.name) for e in employees]
            for eid, ename in employees_list:
                if eid not in emp_map:
                    emp_map[eid] = {
                        "employee_id": eid,
                        "employee_name": ename,
                        "claude_input": 0,
                        "claude_output": 0,
                        "gpt_input": 0,
                        "gpt_output": 0,
                        "tasks": [],
                    }
                emp_map[eid]["claude_input"] += task.claude_input_tokens or 0
                emp_map[eid]["claude_output"] += task.claude_output_tokens or 0
                emp_map[eid]["gpt_input"] += task.gpt_input_tokens or 0
                emp_map[eid]["gpt_output"] += task.gpt_output_tokens or 0
                emp_map[eid]["tasks"].append(_task_row(task))

        rows = []
        total_keys = [
            "claude_input", "claude_output", "claude_total",
            "gpt_input", "gpt_output", "gpt_total",
            "grand_total",
            "tests_total", "tests_passed", "tests_failed",
        ]
        totals = {k: 0 for k in total_keys}

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            ct = data["claude_input"] + data["claude_output"]
            gpt = data["gpt_input"] + data["gpt_output"]

            tasks_sorted = sorted(
                data["tasks"], key=lambda t: t["grand_total"], reverse=True
            )

            row = {
                "employee_id": data["employee_id"],
                "employee_name": data["employee_name"],
                "claude_input": data["claude_input"],
                "claude_output": data["claude_output"],
                "claude_total": ct,
                "gpt_input": data["gpt_input"],
                "gpt_output": data["gpt_output"],
                "gpt_total": gpt,
                "grand_total": ct + gpt,
                "tests_total": sum(t.get("tests_total", 0) for t in tasks_sorted),
                "tests_passed": sum(t.get("tests_passed", 0) for t in tasks_sorted),
                "tests_failed": sum(t.get("tests_failed", 0) for t in tasks_sorted),
                "tasks": tasks_sorted,
            }
            rows.append(row)
            for k in total_keys:
                totals[k] += row.get(k, 0)

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
