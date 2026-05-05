# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _task_row(task):
    bi = task.bedrock_input_tokens or 0
    bo = task.bedrock_output_tokens or 0
    ci = task.claude_input_tokens or 0
    co = task.claude_output_tokens or 0
    gi = task.glm_input_tokens or 0
    go = task.glm_output_tokens or 0
    oi = task.oneP_input_tokens or 0
    oo = task.oneP_output_tokens or 0
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
        "traj_qc_input": tqi,
        "traj_qc_output": tqo,
        "traj_qc_total": tqt,
        "taskdesc_input": tdi,
        "taskdesc_output": tdo,
        "taskdesc_total": tdt,
        "golden_input": gdi,
        "golden_output": gdo,
        "golden_total": gdt,
        "grand_total": bt + ct + gt + ot + tqt + tdt + gdt,
    }


class KenseiCostingController(http.Controller):
    @http.route("/kensei/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
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
            eid = task.employee_id.id if task.employee_id else 0
            ename = task.employee_id.name if task.employee_id else "Unassigned"
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
                "traj_qc_input": data["traj_qc_input"],
                "traj_qc_output": data["traj_qc_output"],
                "traj_qc_total": tqt,
                "taskdesc_input": data["taskdesc_input"],
                "taskdesc_output": data["taskdesc_output"],
                "taskdesc_total": tdt,
                "golden_input": data["golden_input"],
                "golden_output": data["golden_output"],
                "golden_total": gdt,
                "grand_total": bt + ct + gt + ot + tqt + tdt + gdt,
                "tasks": tasks_sorted,
            }
            rows.append(row)
            for k in total_keys:
                if k == "grand_total":
                    totals[k] += row[k]
                elif k in row:
                    totals[k] += row[k]

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
