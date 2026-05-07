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
        "glm": "glm_trajectory",
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
    TestResult = task.env["kensei.test.result"].sudo()
    results = TestResult.search(
        [("kensei_id", "=", task.id)],
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
    bi = task.bedrock_input_tokens or 0
    bo = task.bedrock_output_tokens or 0
    ci = task.claude_input_tokens or 0
    co = task.claude_output_tokens or 0
    gi = task.glm_input_tokens or 0
    go = task.glm_output_tokens or 0
    oi = task.oneP_input_tokens or 0
    oo = task.oneP_output_tokens or 0
    gpi = task.gpt_input_tokens or 0
    gpo = task.gpt_output_tokens or 0
    tqi = task.traj_qc_input_tokens or 0
    tqo = task.traj_qc_output_tokens or 0
    tdi = task.taskdesc_input_tokens or 0
    tdo = task.taskdesc_output_tokens or 0
    gdi = task.golden_input_tokens or 0
    gdo = task.golden_output_tokens or 0

    bt = bi + bo
    ct = ci + co
    gt = gi + go
    ot = oi + oo
    gpt = gpi + gpo
    tqt = tqi + tqo
    tdt = tdi + tdo
    gdt = gdi + gdo

    return {
        "task_id": task.id,
        "task_name": task.task_id or ("Task #%d" % task.id),
        "bedrock_input": bi,
        "bedrock_output": bo,
        "bedrock_total": bt,
        "claude_input": ci,
        "claude_output": co,
        "claude_total": ct,
        "glm_input": gi,
        "glm_output": go,
        "glm_total": gt,
        "oneP_input": oi,
        "oneP_output": oo,
        "oneP_total": ot,
        "gpt_input": gpi,
        "gpt_output": gpo,
        "gpt_total": gpt,
        "traj_qc_input": tqi,
        "traj_qc_output": tqo,
        "traj_qc_total": tqt,
        "taskdesc_input": tdi,
        "taskdesc_output": tdo,
        "taskdesc_total": tdt,
        "golden_input": gdi,
        "golden_output": gdo,
        "golden_total": gdt,
        "grand_total": bt + ct + gt + ot + gpt + tqt + tdt + gdt,
        "trajectories": _parse_trajectories(task),
        "test_results": _get_test_results_for_task(task),
    }
    test_data = row["test_results"]
    tests_total = 0
    tests_passed = 0
    tests_failed = 0
    for model_results in test_data.values():
        for r in model_results:
            tests_total += r.get("tests_total", 0)
            tests_passed += r.get("tests_passed", 0)
            tests_failed += r.get("tests_failed", 0)
    row["tests_total"] = tests_total
    row["tests_passed"] = tests_passed
    row["tests_failed"] = tests_failed
    return row


class KenseiCostingController(http.Controller):
    @http.route("/kensei/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        if not request.env.user.has_group("kensei.group_kensei_pl"):
            raise AccessError("Only Project Leads can access costing data.")
        today = datetime.now().date()

        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = None

        Task = request.env["kensei.kensei"].sudo()
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
                        "bedrock_input": 0,
                        "bedrock_output": 0,
                        "claude_input": 0,
                        "claude_output": 0,
                        "glm_input": 0,
                        "glm_output": 0,
                        "oneP_input": 0,
                        "oneP_output": 0,
                        "gpt_input": 0,
                        "gpt_output": 0,
                        "traj_qc_input": 0,
                        "traj_qc_output": 0,
                        "taskdesc_input": 0,
                        "taskdesc_output": 0,
                        "golden_input": 0,
                        "golden_output": 0,
                        "tasks": [],
                    }
                emp_map[eid]["bedrock_input"] += task.bedrock_input_tokens or 0
                emp_map[eid]["bedrock_output"] += task.bedrock_output_tokens or 0
                emp_map[eid]["claude_input"] += task.claude_input_tokens or 0
                emp_map[eid]["claude_output"] += task.claude_output_tokens or 0
                emp_map[eid]["glm_input"] += task.glm_input_tokens or 0
                emp_map[eid]["glm_output"] += task.glm_output_tokens or 0
                emp_map[eid]["oneP_input"] += task.oneP_input_tokens or 0
                emp_map[eid]["oneP_output"] += task.oneP_output_tokens or 0
                emp_map[eid]["gpt_input"] += task.gpt_input_tokens or 0
                emp_map[eid]["gpt_output"] += task.gpt_output_tokens or 0
                emp_map[eid]["traj_qc_input"] += task.traj_qc_input_tokens or 0
                emp_map[eid]["traj_qc_output"] += task.traj_qc_output_tokens or 0
                emp_map[eid]["taskdesc_input"] += task.taskdesc_input_tokens or 0
                emp_map[eid]["taskdesc_output"] += task.taskdesc_output_tokens or 0
                emp_map[eid]["golden_input"] += task.golden_input_tokens or 0
                emp_map[eid]["golden_output"] += task.golden_output_tokens or 0
                emp_map[eid]["tasks"].append(_task_row(task))

        rows = []
        total_keys = [
            "bedrock_input", "bedrock_output", "bedrock_total",
            "claude_input", "claude_output", "claude_total",
            "glm_input", "glm_output", "glm_total",
            "oneP_input", "oneP_output", "oneP_total",
            "gpt_input", "gpt_output", "gpt_total",
            "traj_qc_input", "traj_qc_output", "traj_qc_total",
            "taskdesc_input", "taskdesc_output", "taskdesc_total",
            "golden_input", "golden_output", "golden_total",
            "grand_total",
        ]
        totals = {k: 0 for k in total_keys}

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            bt = data["bedrock_input"] + data["bedrock_output"]
            ct = data["claude_input"] + data["claude_output"]
            gt = data["glm_input"] + data["glm_output"]
            ot = data["oneP_input"] + data["oneP_output"]
            gpt = data["gpt_input"] + data["gpt_output"]
            tqt = data["traj_qc_input"] + data["traj_qc_output"]
            tdt = data["taskdesc_input"] + data["taskdesc_output"]
            gdt = data["golden_input"] + data["golden_output"]

            tasks_sorted = sorted(
                data["tasks"], key=lambda t: t["grand_total"], reverse=True
            )

            row = {
                "employee_id": data["employee_id"],
                "employee_name": data["employee_name"],
                "bedrock_input": data["bedrock_input"],
                "bedrock_output": data["bedrock_output"],
                "bedrock_total": bt,
                "claude_input": data["claude_input"],
                "claude_output": data["claude_output"],
                "claude_total": ct,
                "glm_input": data["glm_input"],
                "glm_output": data["glm_output"],
                "glm_total": gt,
                "oneP_input": data["oneP_input"],
                "oneP_output": data["oneP_output"],
                "oneP_total": ot,
                "gpt_input": data["gpt_input"],
                "gpt_output": data["gpt_output"],
                "gpt_total": gpt,
                "traj_qc_input": data["traj_qc_input"],
                "traj_qc_output": data["traj_qc_output"],
                "traj_qc_total": tqt,
                "taskdesc_input": data["taskdesc_input"],
                "taskdesc_output": data["taskdesc_output"],
                "taskdesc_total": tdt,
                "golden_input": data["golden_input"],
                "golden_output": data["golden_output"],
                "golden_total": gdt,
                "grand_total": bt + ct + gt + ot + gpt + tqt + tdt + gdt,
                "tests_total": sum(t.get("tests_total", 0) for t in tasks_sorted),
                "tests_passed": sum(t.get("tests_passed", 0) for t in tasks_sorted),
                "tests_failed": sum(t.get("tests_failed", 0) for t in tasks_sorted),
                "tasks": tasks_sorted,
            }
            rows.append(row)
            for k in total_keys:
                if k == "grand_total":
                    totals[k] += row[k]
                elif k in row:
                    totals[k] += row[k]
            totals.setdefault("tests_total", 0)
            totals.setdefault("tests_passed", 0)
            totals.setdefault("tests_failed", 0)
            totals["tests_total"] += row["tests_total"]
            totals["tests_passed"] += row["tests_passed"]
            totals["tests_failed"] += row["tests_failed"]

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
