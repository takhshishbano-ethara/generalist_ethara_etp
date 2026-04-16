# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class TalosCostingController(http.Controller):
    @http.route("/talos/costing/data", type="json", auth="user")
    def costing_data(self, period="week", **kw):
        today = datetime.now().date()

        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = None

        # Read all token data from task-level fields (survives turn deletion)
        Task = request.env["talos.talos"].sudo()
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
                }
            emp_map[eid]["bedrock_input"] += task.bedrock_input_tokens or 0
            emp_map[eid]["bedrock_output"] += task.bedrock_output_tokens or 0
            emp_map[eid]["claude_input"] += task.claude_input_tokens or 0
            emp_map[eid]["claude_output"] += task.claude_output_tokens or 0
            emp_map[eid]["glm_input"] += task.glm_input_tokens or 0
            emp_map[eid]["glm_output"] += task.glm_output_tokens or 0

        rows = []
        totals = {
            "bedrock_input": 0,
            "bedrock_output": 0,
            "bedrock_total": 0,
            "claude_input": 0,
            "claude_output": 0,
            "claude_total": 0,
            "glm_input": 0,
            "glm_output": 0,
            "glm_total": 0,
            "grand_total": 0,
        }

        for data in sorted(emp_map.values(), key=lambda r: r["employee_name"]):
            bt = data["bedrock_input"] + data["bedrock_output"]
            ct = data["claude_input"] + data["claude_output"]
            gt = data["glm_input"] + data["glm_output"]
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
                "grand_total": bt + ct + gt,
            }
            rows.append(row)
            for k in (
                "bedrock_input",
                "bedrock_output",
                "bedrock_total",
                "claude_input",
                "claude_output",
                "claude_total",
                "glm_input",
                "glm_output",
                "glm_total",
            ):
                totals[k] += row[k]
            totals["grand_total"] += row["grand_total"]

        return {
            "period": period,
            "start_date": start.isoformat() if start else None,
            "end_date": today.isoformat(),
            "rows": rows,
            "totals": totals,
        }
